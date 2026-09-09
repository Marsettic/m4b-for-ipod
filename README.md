# m4b-for-ipod

> [!NOTE]
> This was **vibe-coded** and limited testing has been done besides my own personal use. The code and the readme was generated using AI. Use this tool at your own risk and **DO NOT** encode your audiobooks with this tool if you have not backed them up.

**Fix chirping audiobooks on the iPod.** Batch-converts `.m4b` files to AAC-LC mono,
stripping the HE-AAC artifacts that Classic-era iPods can't decode properly.

Preserves chapters, chapter titles, metadata and cover art. Never modifies a source file
until the encode has been verified against it.

Sample:
Audiobook before encoding: https://jumpshare.com/share/xKTcZ0Q61ViPsWq6DaeQ
Audiobook after encoding: https://jumpshare.com/share/1u0ixji3xx2wkCyq8JT8

---

## The problem

Audiobooks are commonly distributed as HE-AAC at 32–64 kbps. HE-AAC codes a narrow
low-frequency core and *synthesises* everything above it using Spectral Band Replication.
On speech that produces a warbling, watery artifact around sibilants — the "birdies" or
chirping.

Classic-era iPods decode AAC-LC only. Feed one an HE-AAC stream and it either refuses it
or decodes just the core layer, which sounds worse still.

This tool re-encodes to real AAC-LC mono and low-passes away the synthetic high end, so
the iPod gets a stream it can actually decode and the fake frequencies are gone rather
than faithfully reproduced.

An AAC-LC file that codes real frequencies up to 11 kHz beats an HE-AAC file that fakes
its way to 16 kHz, for a human voice. The output can genuinely sound better than the
input despite being a lossy-to-lossy transcode.

---

## Requirements

- `ffmpeg` and `ffprobe` on `PATH`
- Python 3.8 or newer (developed and tested on 3.12)

No third-party Python packages.

**Encoder quality depends on your ffmpeg build.** The script picks the best available:

| Encoder | Availability | Notes |
|---|---|---|
| `aac_at` | macOS, ffmpeg 4.4+ | Apple AudioToolbox. Best available. |
| `libfdk_aac` | Builds compiled with it | Excellent. Omitted from most distributions for licensing reasons. |
| `aac` | Everywhere | ffmpeg's native encoder. Works fine at these bitrates; noticeably weaker below ~128 kbps. |

It reports which one it chose on startup. On Windows, an fdk-enabled build is worth
seeking out.

---

## Quick start

Try one book first. Mirrored mode writes a copy and touches nothing:

```bash
mkdir -p ~/m4b-test && cp "/path/to/one book.m4b" ~/m4b-test/
python3 m4b_for_ipod.py ~/m4b-test ~/m4b-test-out
```

Check the result: artwork present, chapters navigate, chirping gone. Then run the library.

```bash
# see what it found and what it would do — costs nothing
python3 m4b_for_ipod.py /path/to/Audiobooks --in-place --dry-run

# do it
python3 m4b_for_ipod.py /path/to/Audiobooks --in-place --jobs 2
```

In-place mode requires you to type `replace` at a prompt.

---

## Modes

### Mirrored (default)

```bash
python3 m4b_for_ipod.py SOURCE DEST
```

Writes converted copies into `DEST`, mirroring `SOURCE`'s folder structure. Sources are
never touched. Source and destination may not be nested inside each other.

### In-place

```bash
python3 m4b_for_ipod.py SOURCE --in-place [--backup-dir DIR]
```

Overwrites originals. Each book is encoded to a temp file beside it, verified, then
swapped in with an atomic rename.

`--backup-dir` moves each original into a mirrored tree elsewhere instead of overwriting
it, giving you a local undo.

### Restore covers

```bash
python3 m4b_for_ipod.py SOURCE --restore-covers-from PRISTINE_COPY
```

Repairs already-converted files that lost their cover art, grafting it from an untouched
copy of the library. **Audio is stream-copied, not re-encoded** — the AAC packets come
through bit-identical, so there is no second generation of loss.

Use this rather than re-running the conversion with `--force`, which would re-encode
already-lossy files for no benefit.

---

## Options

| Option | Default | What it does |
|---|---|---|
| `--in-place` | off | Overwrite originals after verifying each encode |
| `--backup-dir DIR` | — | With `--in-place`, archive originals here instead of overwriting |
| `--restore-covers-from DIR` | — | Repair missing covers from a pristine copy; no re-encode |
| `--bitrate K` | 64, or 48 below 32 kHz | Target kbps |
| `--cutoff HZ` | 11000 | Low-pass cutoff. Raise to 13000–14000 if voices sound dull |
| `--cover-max PX` | 0 (off) | Shrink cover art wider than this |
| `--jobs N` | 4 | Parallel encodes. Use 2 on a USB spinning disk |
| `--force` | off | Re-encode files that already look converted |
| `--dry-run` | off | Report what would happen; encode nothing |
| `--yes` | off | Skip the `--in-place` confirmation prompt |
| `--native-aac` | off | Force ffmpeg's native encoder |

### Tuning the cutoff

Speech carries almost nothing above 10–12 kHz except sibilance. The default 11 kHz cutoff
removes the synthesised band where HE-AAC artifacts live.

If voices sound dull or lispy, raise it. If chirping survives, lower it. The low-pass is
skipped automatically when the source sample rate already band-limits below the cutoff,
which is common for 22.05 kHz spoken-word files.

### Bitrate

Audiobooks are functionally mono, so folding to one channel means 64 kbps buys what 128
would in stereo. That is comfortable for speech. Re-encoding at or above the source's
effective rate keeps second-generation loss negligible.

---

## Safety model

This tool can overwrite your library. The design assumes that will eventually go wrong.

**Encode, verify, then swap.** Each book is written to `<name>.part.m4b` beside the
source, on the same filesystem so the final rename is atomic. Before that rename, the
output is checked: mono, duration within tolerance of the source, chapter count
unchanged. A truncated or crashed encode fails verification, the temp file is deleted,
and the original is untouched.

**Interruption is safe.** Tested by hard-killing a run mid-encode with `SIGKILL`, which
gives the script no chance to clean up. Every original came through byte-identical; the
only debris was orphan `.part.m4b` files, which are safe to delete.

**Re-running is safe.** Already-converted books are detected by inspecting the audio
stream, not by a state file. Stop the run, unplug the drive, resume later — it skips what
is done and does not stack generation loss.

**One instance at a time.** There is currently no lockfile. Two concurrent runs over the
same folder derive the same `.part.m4b` paths and will race. Don't do that.

Have a backup regardless.

---

## What it actually runs

Cover art extraction, as a separate pass:

```
ffmpeg -i SRC -map 0:v:0 -frames:v 1 -c:v mjpeg -q:v 2 cover.jpg
```

Then the encode:

```
ffmpeg -i SRC -i cover.jpg \
  -map 0:a:0 -map 1:v:0 -c:v copy -disposition:v attached_pic \
  -c:a aac_at -profile:a aac_low -b:a 64k -ac 1 \
  -af lowpass=f=11000 \
  -map_metadata 0 -map_chapters 0 OUT.m4b
```

### Why cover art needs two passes

Writing to a `.m4b` extension makes ffmpeg select the **`ipod` muxer**, a stricter variant
of `mp4` with a narrower codec-tag table.

Many audiobooks store their cover as an ordinary video track rather than one flagged
`attached_pic`. Stream-copying that into the `ipod` muxer fails:

```
[ipod] Tag mp4v incompatible with output codec id '7'
```

Adding `-disposition:v attached_pic` does not help, because the muxer's codec-tag check
runs before the disposition is applied.

The obvious fix — adding `-frames:v 1` to the main command — is a trap. It ends the entire
output, not just the video stream, truncating a 20-second test file to 1 second. Extracting
the cover to a still image in its own pass avoids both problems.

If you are writing your own ffmpeg pipeline for m4b files, those two behaviours are the
ones that will cost you an afternoon.

---

## Known limitations

**Input must be `.m4b`.** Other formats are not scanned. Widening this is easy for the
encoding but complicates `--in-place`, since `book.mp3` → `book.m4b` is a rename rather
than a replace.

**Folders of MP3s are not merged.** A book split across 47 files needs concatenation and
generated chapters. Out of scope.

**The `stik` atom is unverified.** This MP4 atom marks a file as an audiobook and drives
bookmark-resume in many players. Whether ffmpeg preserves it through this conversion has
not been confirmed. Check with `strings file.m4b | grep stik` before and after. If it is
dropped, a post-pass with AtomicParsley or mp4v2 can restore it.

**Container brand comes out `M4A `, not `M4B `.** Some players sniff this to decide
audiobook behaviour.

**No `iTunSMPB` atom**, so gapless metadata is not written. Rarely matters for
chapter-based audiobooks.

**`--force` is sometimes necessary.** Files that are *already* AAC-LC mono near the target
bitrate are skipped, since re-encoding them normally just adds loss. But a file in that
shape with chirping baked in by whoever encoded it originally will also be skipped, and
the low-pass that would help never runs. Use `--force` for those.

**Windows support is untested on real hardware.** The console encoding, ANSI fallback,
retrying atomic replace and long-path guard are implemented and unit-tested against forced
platform flags, but nobody has run it on Windows yet. Reports welcome.

**Long Windows paths are skipped, not handled.** Files whose temp path would exceed 259
characters are reported and skipped rather than attempted via the `\\?\` prefix, because
ffmpeg's URL parser may misread that prefix as a protocol specifier.

---

## Troubleshooting

**Nothing appears for a while at startup.** It is scanning and reading file headers. A
progress line appears within a second on a terminal; if you piped stdout, progress goes to
stderr.

**"no .m4b files found".** The scan skips hidden folders, `._` AppleDouble sidecars, and
volume metadata directories. Confirm with
`find /your/path -iname "*.m4b" | wc -l`. If your books are `.m4a` or `.mp3`, they are not
supported yet.

**`[cover dropped: ...]` in the log.** The message includes the actual ffmpeg error. If it
mentions the `ipod` muxer or a codec tag, the cover format is unusual — please open an
issue with the ffprobe output.

**A file failed.** Failed files are left untouched. The log line carries the reason. A
`duration mismatch` means verification caught a truncated encode and correctly refused it.

**Leftover `.part.m4b` files.** Safe to delete. They only exist when a run was
interrupted.

**Ctrl+C doesn't return the prompt immediately.** In-flight encodes are allowed to wind
down first. Normal.

---

## Contributing

Bug reports are most useful with the output of `--dry-run` for the affected file, plus:

```bash
ffprobe -v error -show_entries \
  "stream=codec_type,codec_name,profile,sample_rate,channels,bit_rate:format=duration:chapter=id" \
  -of json "your book.m4b"
```

Reports from Windows users are especially welcome, since that platform has not been
exercised on real hardware.

---

## License

MIT. See [LICENSE](LICENSE).

### A note on ffmpeg's licensing

This script is MIT, but ffmpeg is not, and the distinction matters if you plan to ship a
bundle rather than just this file.

ffmpeg itself is LGPL or GPL depending on how it was built. Building it with
`--enable-libfdk-aac` additionally requires `--enable-nonfree`, and the resulting binary
**cannot be redistributed at all**. So if you want the quality that `libfdk_aac` gives you,
build or obtain ffmpeg yourself rather than expecting to redistribute it.

None of this restricts this script. It shells out to whatever ffmpeg it finds on `PATH`
and links against nothing.
