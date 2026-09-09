#!/usr/bin/env python3
"""
m4b_for_ipod.py - batch-convert .m4b audiobooks to iPod-friendly AAC-LC mono.

Re-encodes every .m4b under a folder with Apple's AudioToolbox AAC encoder
(aac_at), folds to mono, and low-passes to strip the synthesised high
frequencies that make HE-AAC audiobooks chirp. Chapters, metadata and cover
art are carried across.

COVER ART NOTE
  An .m4b output selects ffmpeg's `ipod` muxer, which is stricter than `mp4`.
  Many audiobooks store their cover as an ordinary video track rather than one
  flagged `attached_pic`, and stream-copying that into the ipod muxer fails
  with "Tag mp4v incompatible with output codec id". Setting -disposition
  doesn't help: the muxer's codec-tag check runs first. So the cover is
  extracted to a still JPEG in a separate pass, then attached. Doing the
  extraction inline with -frames:v 1 truncates the whole output to one second,
  which is why it is a separate step.

MODES
  mirrored (default)      Write converted copies into a separate tree.
  --in-place              Overwrite originals, after verifying each encode.
  --restore-covers-from   Repair already-converted files that lost their cover,
                          by grafting it from a pristine copy. Audio is stream-
                          copied, so there is no second generation of loss.

Requires ffmpeg and ffprobe on PATH. aac_at is macOS-only; elsewhere the
script falls back to libfdk_aac or ffmpeg's native aac encoder automatically.

MIT licensed. See the LICENSE file distributed alongside this script.

Examples:
    ./m4b_for_ipod.py /Volumes/EXTERNAL/Audiobooks --in-place --dry-run
    ./m4b_for_ipod.py /Volumes/EXTERNAL/Audiobooks --in-place --jobs 2
    ./m4b_for_ipod.py /Volumes/EXTERNAL/Audiobooks \
        --restore-covers-from /Volumes/NAS/Audiobooks
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

PRINT_LOCK = threading.Lock()
PART_SUFFIX = ".part.m4b"
PROBE_TIMEOUT = 60
COVER_TIMEOUT = 120
IS_WINDOWS = os.name == "nt"

# Win32 refuses paths over 259 chars unless long-path support is switched on,
# and ffmpeg.exe is not reliably long-path aware even when Python is.
WIN_PATH_LIMIT = 259

# Encoders in descending order of quality. aac_at is Apple's AudioToolbox
# encoder (macOS only); libfdk_aac is the best option elsewhere but is omitted
# from most distributed builds for licensing reasons; the native aac encoder is
# the universally available fallback.
ENCODER_PREFERENCE = ["aac_at", "libfdk_aac", "aac"]

ANSI_OK = True
_LAST_STATUS_LEN = 0

SKIP_DIRS = {
    ".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems",
    ".DocumentRevisions-V100", "System Volume Information", "$RECYCLE.BIN",
}

ACTIVE: dict[str, tuple[float, float | None]] = {}
ACTIVE_LOCK = threading.Lock()


def setup_console() -> None:
    """Force UTF-8 on stdout/stderr. Without this, a book title containing an
    accent or a dash raises UnicodeEncodeError on a cp1252 Windows console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def enable_vt_mode() -> bool:
    """Switch on ANSI escape handling for the Windows console. Returns False if
    escapes are unavailable, in which case status lines fall back to plain \\r
    plus padding, which every terminal understands."""
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
        if handle in (0, -1):
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _erase() -> str:
    if ANSI_OK:
        return "\r\033[K"
    return "\r" + " " * _LAST_STATUS_LEN + "\r"


def log(msg: str = "") -> None:
    global _LAST_STATUS_LEN
    with PRINT_LOCK:
        sys.stderr.write(_erase())
        sys.stderr.flush()
        _LAST_STATUS_LEN = 0
        print(msg, flush=True)


def status(msg: str) -> None:
    global _LAST_STATUS_LEN
    if not sys.stderr.isatty():
        return
    # a status line wider than the terminal wraps and leaves debris behind,
    # which matters most on the 80-column default of a fresh Windows console
    width = max(40, shutil.get_terminal_size(fallback=(80, 24)).columns - 1)
    msg = msg[:width]
    with PRINT_LOCK:
        sys.stderr.write(_erase() + msg)
        sys.stderr.flush()
        _LAST_STATUS_LEN = len(msg)


def clear_status() -> None:
    global _LAST_STATUS_LEN
    if sys.stderr.isatty():
        with PRINT_LOCK:
            sys.stderr.write(_erase())
            sys.stderr.flush()
            _LAST_STATUS_LEN = 0


def fmt_hms(seconds: float) -> str:
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


@dataclass
class Probe:
    codec: str
    profile: str
    sample_rate: int
    channels: int
    bitrate_kbps: int | None
    duration: float | None
    chapters: int
    has_cover: bool


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"error: {tool} not found on PATH")


def available_encoders() -> set[str]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                         capture_output=True, text=True, check=False).stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return names


def choose_encoders(available: set[str], native_only: bool) -> list[str]:
    """Best-to-worst encoder fallback chain, filtered to what this build has."""
    if native_only:
        return ["aac"]
    chain = [e for e in ENCODER_PREFERENCE if e in available]
    return chain or ["aac"]


def atomic_replace(tmp: Path, dst: Path, attempts: int = 6) -> None:
    """os.replace is atomic on Windows too, but it fails with PermissionError
    while any process holds the destination open. Antivirus scanners, the
    Search indexer and media players all do this transiently, so retry."""
    for i in range(attempts):
        try:
            tmp.replace(dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.4 * (i + 1))


def path_too_long(path: Path) -> str | None:
    """Return a reason string if Win32 will reject this path, else None.
    Checked up front so the run reports it clearly instead of failing partway
    through with an opaque ffmpeg error."""
    if not IS_WINDOWS:
        return None
    # the temp file is the longest name we will actually open
    longest = len(str(path.with_name(path.stem + PART_SUFFIX)))
    if longest > WIN_PATH_LIMIT:
        return (f"path is {longest} chars, over the Windows {WIN_PATH_LIMIT} limit; "
                f"enable long paths or move the library closer to the drive root")
    return None


def find_m4b(root: Path) -> tuple[list[Path], list[str]]:
    found: list[Path] = []
    warnings: list[str] = []
    scanned = 0

    def on_error(err: OSError) -> None:
        warnings.append(f"could not read {err.filename}: {err.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        scanned += 1
        if scanned % 25 == 0:
            status(f"scanning... {scanned} folders, {len(found)} audiobooks found")
        for name in filenames:
            if name.startswith("._") or name.endswith(PART_SUFFIX):
                continue
            if name.lower().endswith(".m4b"):
                found.append(Path(dirpath) / name)

    clear_status()
    return sorted(found), warnings


def probe(path: Path) -> Probe | None:
    cmd = [
        "ffprobe", "-v", "error", "-of", "json", "-show_entries",
        "stream=codec_type,codec_name,profile,sample_rate,channels,bit_rate"
        ":format=duration:chapter=id",
        str(path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             check=False, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None

    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        return None

    raw_bitrate = audio.get("bit_rate")
    raw_duration = data.get("format", {}).get("duration")
    return Probe(
        codec=audio.get("codec_name", "?"),
        profile=audio.get("profile", "?"),
        sample_rate=int(audio.get("sample_rate") or 44100),
        channels=int(audio.get("channels") or 2),
        bitrate_kbps=round(int(raw_bitrate) / 1000) if raw_bitrate else None,
        duration=float(raw_duration) if raw_duration else None,
        chapters=len(data.get("chapters", [])),
        has_cover=any(s.get("codec_type") == "video" for s in streams),
    )


def probe_all(files: list[Path], workers: int) -> dict[Path, Probe | None]:
    results: dict[Path, Probe | None] = {}
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe, f): f for f in files}
        for n, future in enumerate(as_completed(futures), 1):
            path = futures[future]
            results[path] = future.result()
            elapsed = time.monotonic() - started
            eta = (len(files) - n) / (n / elapsed) if elapsed > 0 and n else 0
            status(f"reading file info... {n}/{len(files)} (~{eta:.0f}s left) "
                   f"{path.name[:40]}")
    clear_status()
    return results


def extract_cover(src: Path, out_jpg: Path, cover_max: int) -> tuple[bool, str]:
    """Pull the first video stream out as a single still JPEG.

    -frames:v 1 is safe here because this output has no audio stream to
    truncate. Doing it in the main encode would cut the audiobook to 1 second.
    """
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
           "-i", str(src), "-map", "0:v:0", "-frames:v", "1",
           "-c:v", "mjpeg", "-q:v", "2"]
    if cover_max:
        cmd += ["-vf", f"scale='min({cover_max},iw)':-2"]
    cmd.append(str(out_jpg))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             check=False, timeout=COVER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "cover extraction timed out"
    if res.returncode != 0 or not out_jpg.exists() or out_jpg.stat().st_size == 0:
        lines = res.stderr.strip().splitlines()
        return False, lines[-1] if lines else "cover extraction failed"
    return True, ""


def pick_settings(info: Probe, args) -> tuple[int, int | None]:
    nyquist = info.sample_rate / 2
    cutoff = args.cutoff if args.cutoff < nyquist * 0.9 else None
    bitrate = args.bitrate or (64 if info.sample_rate >= 32000 else 48)
    return bitrate, cutoff


def already_converted(info: Probe, args) -> bool:
    if info.codec != "aac" or info.channels != 1:
        return False
    if "HE" in (info.profile or "").upper():
        return False
    target, _ = pick_settings(info, args)
    if info.bitrate_kbps and info.bitrate_kbps > target * 1.15:
        return False
    return True


def build_cmd(src: Path, dst: Path, encoder: str, bitrate: int,
              cutoff: int | None, cover: Path | None) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
           "-progress", "pipe:1", "-nostats", "-i", str(src)]
    if cover:
        cmd += ["-i", str(cover)]
    cmd += ["-map", "0:a:0"]
    if cover:
        # the cover input is already a single mjpeg frame, so copy it straight
        # in; no -frames:v needed and none wanted
        cmd += ["-map", "1:v:0", "-c:v", "copy", "-disposition:v", "attached_pic"]
    cmd += ["-c:a", encoder, "-profile:a", "aac_low", "-b:a", f"{bitrate}k", "-ac", "1"]
    if cutoff:
        cmd += ["-af", f"lowpass=f={cutoff}"]
    cmd += ["-map_metadata", "0", "-map_chapters", "0", str(dst)]
    return cmd


def run_encode(cmd: list[str], key: str, total: float | None) -> tuple[int, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    err: list[str] = []
    drain = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    drain.start()
    with ACTIVE_LOCK:
        ACTIVE[key] = (0.0, total)
    try:
        for line in proc.stdout:
            field, _, value = line.strip().partition("=")
            # out_time_ms is really microseconds; long-standing ffmpeg quirk
            if field in ("out_time_us", "out_time_ms") and value.isdigit():
                with ACTIVE_LOCK:
                    ACTIVE[key] = (int(value) / 1_000_000, total)
    finally:
        proc.wait()
        drain.join(timeout=5)
        with ACTIVE_LOCK:
            ACTIVE.pop(key, None)
    return proc.returncode, "".join(c for c in err if c)


def render_progress(stop: threading.Event, total_jobs: int,
                    counter: dict, started: float) -> None:
    while not stop.wait(0.5):
        with ACTIVE_LOCK:
            items = sorted(ACTIVE.items())
        parts = []
        for name, (done, total) in items[:3]:
            parts.append(f"{name[:22]} {min(99, int(100 * done / total))}%"
                         if total else f"{name[:22]} {fmt_hms(done)}")
        if len(items) > 3:
            parts.append(f"+{len(items) - 3} more")
        body = "  |  ".join(parts) if parts else "starting..."
        status(f"[{counter['done']}/{total_jobs}] {body}   "
               f"{fmt_hms(time.monotonic() - started)} elapsed")


def verify(src_info: Probe, out_path: Path) -> tuple[bool, str]:
    """Gate before anything is allowed to replace a real file. This is what
    catches a truncated encode, so keep it strict about duration."""
    out = probe(out_path)
    if out is None:
        return False, "output could not be probed"
    if out.channels != 1:
        return False, f"output is {out.channels}ch, expected mono"
    if src_info.duration and out.duration:
        tolerance = max(2.0, src_info.duration * 0.002)
        drift = abs(src_info.duration - out.duration)
        if drift > tolerance:
            return False, (f"duration mismatch: {src_info.duration:.1f}s -> "
                           f"{out.duration:.1f}s (drift {drift:.1f}s)")
    if src_info.chapters and out.chapters != src_info.chapters:
        return False, f"chapters lost: {src_info.chapters} -> {out.chapters}"
    return True, ""


def convert(src: Path, dst: Path, info: Probe, args, encoders: set[str],
            backup_root: Path | None, src_root: Path) -> tuple[bool, str]:
    bitrate, cutoff = pick_settings(info, args)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = src.with_name(src.stem + PART_SUFFIX)

    cover_path: Path | None = None
    cover_note = ""
    workdir = None
    if info.has_cover:
        workdir = tempfile.mkdtemp(prefix="m4bcover-")
        candidate = Path(workdir) / "cover.jpg"
        got, why = extract_cover(src, candidate, args.cover_max)
        if got:
            cover_path = candidate
        else:
            cover_note = f" [cover dropped: {why[:70]}]"

    candidates = choose_encoders(encoders, args.native_aac)
    attempts = []
    for enc in candidates:
        if cover_path:
            attempts.append((enc, cover_path))
        attempts.append((enc, None))

    last_err = "unknown error"
    try:
        for encoder, cover in attempts:
            code, stderr_text = run_encode(
                build_cmd(src, tmp, encoder, bitrate, cutoff, cover),
                src.name, info.duration,
            )
            if code != 0 or not tmp.exists() or tmp.stat().st_size == 0:
                lines = stderr_text.strip().splitlines()
                last_err = lines[-1] if lines else "ffmpeg failed with no message"
                if cover:
                    cover_note = f" [cover dropped: {last_err[:70]}]"
                tmp.unlink(missing_ok=True)
                continue

            good, why = verify(info, tmp)
            if not good:
                last_err = why
                if cover:
                    cover_note = f" [cover dropped: {why[:70]}]"
                tmp.unlink(missing_ok=True)
                continue

            saved = 100 * (1 - tmp.stat().st_size / max(1, src.stat().st_size))
            detail = f"{encoder} {bitrate}k mono"
            detail += f" lp{cutoff}" if cutoff else " (no low-pass)"
            detail += f", -{saved:.0f}%"
            detail += " [cover kept]" if cover else cover_note

            if backup_root is not None:
                backup = backup_root / src.relative_to(src_root)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(backup))
                detail += " [original archived]"

            atomic_replace(tmp, dst)
            return True, detail
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)

    return False, last_err


def restore_cover(target: Path, origin: Path, args) -> tuple[bool, str]:
    """Graft a cover from a pristine copy onto an already-converted file.
    Audio is stream-copied, so the AAC packets come through untouched."""
    workdir = tempfile.mkdtemp(prefix="m4bcover-")
    tmp = target.with_name(target.stem + PART_SUFFIX)
    try:
        cover = Path(workdir) / "cover.jpg"
        got, why = extract_cover(origin, cover, args.cover_max)
        if not got:
            return False, f"no cover in source: {why[:70]}"

        before = probe(target)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
               "-i", str(target), "-i", str(cover),
               "-map", "0:a:0", "-map", "1:v:0",
               "-c:a", "copy", "-c:v", "copy", "-disposition:v", "attached_pic",
               "-map_metadata", "0", "-map_chapters", "0", str(tmp)]
        res = subprocess.run(cmd, capture_output=True, text=True,
                             check=False, timeout=COVER_TIMEOUT)
        if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            lines = res.stderr.strip().splitlines()
            return False, lines[-1] if lines else "remux failed"

        if before:
            good, why = verify(before, tmp)
            if not good:
                return False, why
        after = probe(tmp)
        if not after or not after.has_cover:
            return False, "cover missing from result"

        atomic_replace(tmp, target)
        return True, "cover restored (audio stream-copied)"
    except subprocess.TimeoutExpired:
        return False, "timed out"
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(workdir, ignore_errors=True)


def run_restore(src_root: Path, origin_root: Path, args) -> int:
    files, _ = find_m4b(src_root)
    origins, _ = find_m4b(origin_root)
    by_name: dict[str, Path] = {}
    for o in origins:
        by_name.setdefault(o.name, o)

    log(f"{len(files)} file(s) here, {len(origins)} in the reference copy\n")

    probes = probe_all(files, workers=max(2, args.jobs))
    todo = []
    for f in files:
        info = probes.get(f)
        if info is None or info.has_cover:
            continue
        rel = f.relative_to(src_root)
        origin = origin_root / rel
        if not origin.is_file():
            origin = by_name.get(f.name)
        if origin is None:
            log(f"SKIP  {rel}  (no match in reference copy)")
            continue
        todo.append((f, origin))

    log(f"{len(todo)} file(s) missing a cover and matched to a source\n")
    if args.dry_run:
        for f, origin in todo:
            log(f"{f.relative_to(src_root)}  <-  {origin}")
        return 0

    ok = bad = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(restore_cover, f, o, args): f for f, o in todo}
        for n, future in enumerate(as_completed(futures), 1):
            rel = futures[future].relative_to(src_root)
            good, detail = future.result()
            ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
            log(f"[{n}/{len(todo)}] {'ok  ' if good else 'FAIL'}  {rel}  ->  {detail}")

    log(f"\ndone: {ok} restored, {bad} failed")
    return 1 if bad else 0


def main() -> int:
    global ANSI_OK
    setup_console()
    ANSI_OK = enable_vt_mode()

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path, nargs="?")
    parser.add_argument("--in-place", action="store_true")
    parser.add_argument("--restore-covers-from", type=Path, default=None, metavar="DIR",
                        help="repair covers on already-converted files using this "
                             "pristine copy; audio is not re-encoded")
    parser.add_argument("--backup-dir", type=Path, default=None, metavar="DIR")
    parser.add_argument("--bitrate", type=int, default=None, metavar="K")
    parser.add_argument("--cutoff", type=int, default=11000, metavar="HZ")
    parser.add_argument("--cover-max", type=int, default=0, metavar="PX",
                        help="shrink cover art wider than this (0 = leave alone)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--native-aac", action="store_true")
    args = parser.parse_args()

    require_tools()

    src_root = args.source.expanduser().resolve()
    if not src_root.is_dir():
        return fail(f"error: {src_root} is not a directory")

    if args.restore_covers_from is not None:
        origin_root = args.restore_covers_from.expanduser().resolve()
        if not origin_root.is_dir():
            return fail(f"error: {origin_root} is not a directory")
        log(f"restoring covers in {src_root}\n           from {origin_root}\n")
        return run_restore(src_root, origin_root, args)

    if args.in_place:
        if args.dest is not None:
            return fail("error: don't pass a destination with --in-place")
        dst_root = src_root
    else:
        if args.dest is None:
            return fail("error: need a destination folder, or pass --in-place")
        dst_root = args.dest.expanduser().resolve()
        if src_root == dst_root or src_root in dst_root.parents or dst_root in src_root.parents:
            return fail("error: source and destination must not be nested "
                        "(use --in-place to overwrite originals)")

    backup_root = None
    if args.backup_dir is not None:
        if not args.in_place:
            return fail("error: --backup-dir only applies with --in-place")
        backup_root = args.backup_dir.expanduser().resolve()
        if backup_root == src_root or src_root in backup_root.parents:
            return fail("error: --backup-dir must live outside the source folder")

    encoders = available_encoders()
    chain = choose_encoders(encoders, args.native_aac)
    labels = {"aac_at": "aac_at (Apple AudioToolbox)",
              "libfdk_aac": "libfdk_aac (Fraunhofer)",
              "aac": "aac (ffmpeg native)"}
    log(f"encoder: {labels.get(chain[0], chain[0])}"
        + (f"   fallbacks: {', '.join(chain[1:])}" if len(chain) > 1 else ""))
    if chain[0] == "aac" and not args.native_aac:
        log("note: neither aac_at nor libfdk_aac found; quality per bit will be lower.\n"
            "      on Windows, an ffmpeg build with libfdk_aac is worth seeking out")
    log(f"scanning {src_root} ...")

    files, warnings = find_m4b(src_root)
    for w in warnings[:5]:
        log(f"warning: {w}")
    if len(warnings) > 5:
        log(f"warning: ...and {len(warnings) - 5} more unreadable folders")
    if not files:
        log(f"no .m4b files found under {src_root}")
        log("check the path; the scan skips hidden folders and ._ sidecars")
        return 0

    log(f"found {len(files)} .m4b file(s), reading file info...")
    probes = probe_all(files, workers=max(2, args.jobs))

    jobs, skipped, unreadable, too_long = [], 0, 0, 0
    for src in files:
        reason = path_too_long(src)
        if reason:
            log(f"SKIP  {src.name}  ({reason})")
            too_long += 1
            continue
        info = probes.get(src)
        if info is None:
            log(f"SKIP  {src.relative_to(src_root)}  (could not probe)")
            unreadable += 1
            continue
        if args.in_place:
            dst = src
            if not args.force and already_converted(info, args):
                skipped += 1
                continue
        else:
            dst = dst_root / src.relative_to(src_root)
            if dst.exists() and not args.force and dst.stat().st_mtime >= src.stat().st_mtime:
                skipped += 1
                continue
        jobs.append((src, dst, info))

    hours = sum(i.duration or 0 for _, _, i in jobs) / 3600
    log(f"\n{len(files)} file(s) found, {skipped} already converted, "
        f"{unreadable} unreadable, "
        + (f"{too_long} path too long, " if too_long else "")
        + f"{len(jobs)} to process ({hours:.1f} hours of audio)\n")

    if args.dry_run:
        for src, _d, info in jobs:
            bitrate, cutoff = pick_settings(info, args)
            extras = ", cover art" if info.has_cover else ""
            extras += f", {info.chapters} chapters" if info.chapters else ""
            lp = f"low-pass {cutoff} Hz" if cutoff else "no low-pass (already band-limited)"
            log(f"{src.relative_to(src_root)}\n"
                f"    from  {info.codec} {info.profile}, {info.sample_rate} Hz, "
                f"{info.channels}ch, {info.bitrate_kbps or '?'} kbps{extras}\n"
                f"    to    AAC-LC mono, {bitrate} kbps, {lp}")
        return 0
    if not jobs:
        return 0

    if args.in_place and not args.yes:
        gib = sum(s.stat().st_size for s, _, _ in jobs) / (1024 ** 3)
        note = (f"Originals will be moved to {backup_root}." if backup_root
                else "Originals will be overwritten and not recoverable.")
        log(f"About to convert {len(jobs)} file(s) in place under {src_root} ({gib:.1f} GiB).\n"
            f"{note}\nEach encode is verified before the swap, but confirm your backup is current.")
        try:
            answer = input("Type 'replace' to continue: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "replace":
            log("aborted, nothing changed")
            return 1

    log(f"encoding {len(jobs)} file(s) with {args.jobs} parallel job(s)...\n")
    ok = failed = 0
    started = time.monotonic()
    counter = {"done": 0}
    stop = threading.Event()
    painter = threading.Thread(target=render_progress,
                               args=(stop, len(jobs), counter, started), daemon=True)
    painter.start()
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = {pool.submit(convert, s, d, i, args, encoders,
                                   backup_root, src_root): s for s, d, i in jobs}
            for future in as_completed(futures):
                rel = futures[future].relative_to(src_root)
                success, detail = future.result()
                counter["done"] += 1
                n = counter["done"]
                if success:
                    ok += 1
                    log(f"[{n}/{len(jobs)}] ok    {rel}  ->  {detail}")
                else:
                    failed += 1
                    log(f"[{n}/{len(jobs)}] FAIL  {rel}  ->  {detail}")
    finally:
        stop.set()
        painter.join(timeout=2)
        clear_status()

    log(f"\ndone: {ok} converted, {failed} failed, {skipped} skipped "
        f"in {fmt_hms(time.monotonic() - started)}")
    if failed:
        log("failed files were left untouched")
    return 1 if failed else 0


def fail(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\ninterrupted (leftover *{PART_SUFFIX} files are safe to delete)",
              file=sys.stderr)
        sys.exit(130)
