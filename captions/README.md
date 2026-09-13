# captions

Optional add-on for the [Video Intake & Blender VSE Pipeline](../README.md).
Auto-captions an MP4 with [whisper.cpp](https://github.com/ggerganov/whisper.cpp)
transcription and ffmpeg muxing — designed for the moment after you've cut
your Blender project and rendered it out.

**Not required for the main pipeline.** If you only ship to YouTube and let
YouTube auto-caption, you never need this. Captions live in a subfolder
with their own dependencies so the global pipeline stays lightweight.

---

## Install

The script is **stdlib-only Python** — nothing to `pip install`. It just
shells out to two external binaries, the same way the rest of the pipeline
shells out to `ffmpeg` and `blender`:

```bash
brew install whisper-cpp
```

That installs `whisper-cli`, which uses Metal GPU acceleration on Apple
Silicon and is dramatically faster than the Python `openai-whisper`
package. `ffmpeg` is already required by the main pipeline.

### One-time model download

Whisper needs a model file. Pick a size, then `curl` it once:

```bash
mkdir -p ~/whisper-models

# Medium is the default — Metal makes it fast enough to be worth the accuracy.
curl -L -o ~/whisper-models/ggml-medium.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin

# Silero VAD — small, and the main defense against hallucinated captions.
curl -L -o ~/whisper-models/ggml-silero-v5.1.2.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-silero-v5.1.2.bin
```

| Model    | Size    | Speed (Apple Silicon w/ Metal) | Quality       |
|----------|---------|--------------------------------|---------------|
| `tiny`   | ~75 MB  | fastest                        | rough         |
| `base`   | ~140 MB | very fast                      | usable        |
| `small`  | ~466 MB | fast                           | good          |
| `medium` | ~1.5 GB | medium (default)               | very good     |
| `large`  | ~3 GB   | slowest                        | best          |

Replace `ggml-medium.bin` in the URL and filename with whichever size you
want. If you request a size whose file isn't present, `captions.py` will
print the exact `curl` command for it and exit cleanly.

The VAD model is **optional** — without it the script warns once and runs
anyway — but it's under a megabyte and materially improves output on
anything with silence, music, or room tone in it. See
[Hallucination guards](#hallucination-guards).

`~/whisper-models/` is the default search location for both. Override with
`--model-dir` or pass an explicit path with `--model /path/to/model.bin`.

### Check your setup

```bash
python3 captions/check-deps.py
```

Reports `whisper-cli`, which models you have, and — the part a generic
dependency check can't do — **which hallucination guards will actually be
active**. Not every whisper.cpp build has native `--vad`, so it probes for that
rather than assuming. The top-level `0-check-deps.py` deliberately skips this
folder, so a machine that will never run Whisper still gets a clean preflight.

---

## Usage

```bash
python3 captions/captions.py /path/to/render.mp4
python3 captions/captions.py /path/to/render.mp4 --burn
python3 captions/captions.py /path/to/render.mp4 --word-by-word --burn
python3 captions/captions.py                            # prompted — supports drag-and-drop
```

No venv, no activation step — just run with your normal `python3`.

### Default: soft-embedded muxed subtitles

The default mode writes two files next to your input:

- `<stem>.srt` — editable subtitle text.
- `<stem>_subbed.mp4` — the same video with the SRT muxed in as a
  `mov_text` subtitle stream. Video and audio are stream-copied (no
  re-encode), so the result is the same size as the input and finishes in
  seconds. Captions are **toggleable** in any player that supports MP4
  subtitles: QuickTime (View → Subtitles), VLC, iPad, Apple TV, web video
  players with `track` element support, etc.

### `--burn`: hard-burn pixels (additional output)

Additionally produces `<stem>_burned.mp4` with the captions rendered into
the video pixels via ffmpeg's `subtitles`/`ass` filter (libass). Slower
(re-encodes the video, CRF 18) and the captions can't be turned off — but
they travel through *anything*, including platforms that strip soft subs.

### `--word-by-word`: single-word flash captions

Default is phrase chunks (~3–6 second lines, sentence-aware). Pass
`--word-by-word` to flash each word individually (Arial-Bold 90pt,
centered on screen, white text with a 5px white stroke).

### `--keep-srt-only`

Stops after writing the SRT — useful when you want to hand-edit before
muxing. Re-run without `--keep-srt-only` once the SRT is clean.

### Other flags

| Flag           | Default               | Notes                                                                |
|----------------|-----------------------|----------------------------------------------------------------------|
| `--model`      | `medium`              | Size keyword (`tiny`/`base`/`small`/`medium`/`large`) or a `.bin` path. |
| `--model-dir`  | `~/whisper-models`    | Where size keywords and the VAD model are resolved.                  |
| `--language`   | `auto`                | ISO code (`en`, `es`, `de`, …) or `auto` for whisper.cpp to detect.  |
| `--no-vad`     | off (VAD on)          | Disable Silero VAD. See below — only if VAD is eating real speech.   |
| `--font`       | `Arial-Bold`          | Burn-in font name (must be installed on macOS). Ignored for SRT/soft.|
| `--size`       | 60 / 90               | Burn-in size: 60 phrase, 90 word-by-word. Ignored for SRT/soft.      |

---

## Hallucination guards

Whisper doesn't fail quietly. Handed audio it can't parse, it invents
confident-sounding text that was never said — and it can lock onto a single
phrase and repeat it for an entire file. A real batch on another machine came
back with **631 identical `(crickets chirping)` lines and zero actual
dialogue**. Three guards are built in, in the order they matter:

**1. Phase-cancellation detection (automatic).** The obvious way to make
whisper's 16 kHz mono input is `ffmpeg -ac 1`, which sums L+R. If the source's
channels are out of phase, that sum nearly cancels itself out — measured 30–45 dB
quieter than either channel alone. Whisper gets near-silence and starts
inventing. This was the *actual* root cause of the crickets batch, and it looks
exactly like a model-quality problem while being entirely upstream of the model.

`captions.py` extracts both the downmix and the left channel, compares mean
volume, and uses whichever has real signal. On normal in-phase audio the downmix
wins and nothing changes; when it doesn't, you get a warning naming both levels:

```
⚠ Mono downmix (-91.0 dB) is far quieter than the left channel alone
  (-21.1 dB) — likely L/R phase cancellation. Using the left channel instead.
```

Step 1 always reports which source it used, so this is visible in normal output.

**2. `-mc 0` (always on).** By default whisper.cpp feeds each 30-second chunk's
decoded text forward as the prompt for the next one. Mis-hear one chunk — theme
music, a laugh track, room tone — and that bad guess becomes context for
everything after it, so the model re-emits the same hallucinated phrase for the
rest of the file. One bad chunk silently destroys the whole transcript. With
`-mc 0` each chunk is decoded independently and a bad guess can't cascade.

**3. Silero VAD (automatic when the model is present).** Only segments that
actually contain speech reach the decoder. Whisper's training data makes it fill
silence and background noise with plausible boilerplate rather than admitting
uncertainty, so the fix is to never show it the silence. Enabled whenever
`ggml-silero-v5.1.2.bin` is in `--model-dir`; the header line reports `VAD: off`
when it isn't.

Use `--no-vad` only if VAD is dropping speech you need — very quiet or
heavily-processed dialogue is the case where it can be too aggressive.

## VAD timestamp drift (fixed)

On a long file with VAD enabled, captions used to land right on the beat at
the start and then fall further and further behind as the file went on — on a
20-minute test file the last line was misplaced by nearly 3 minutes. Not a
player bug (reproduced identically in QuickTime and Quick Look) and not a
video-framerate problem (the rendered MP4's frame timing was checked and was
solid CFR throughout).

**Root cause:** whisper.cpp's VAD path decodes a copy of the audio with the
silent stretches spliced out. It correctly remaps each *segment's* timestamps
back onto the real (pre-VAD) timeline, but the *per-token* offsets inside each
segment are left relative to that spliced, time-compressed audio. Every
silence VAD strips out adds to the gap between "token time" and "real time,"
so the drift is roughly proportional to how much silence has accumulated by
that point in the file — near-zero at the start, worst by the end. `transcribe()`
was reading only the per-token offsets, so it inherited the drift directly.

**Fix (already applied, in `transcribe()`):** for each segment, compute
`shift = segment's own offset − its first token's raw offset`, and add that
shift to every token in the segment. The segment-level offset is always
correct, so this re-anchors each token to real time. No-op when VAD is off
(segment and first-token offsets already agree there), so this is safe to
leave on unconditionally.

---

## Files in this folder

```
captions/
├── README.md        ← this file
├── check-deps.py    ← preflight: whisper-cli, models, hallucination guards
└── captions.py      ← the tool — stdlib Python, shells out to whisper-cli + ffmpeg
```

Three files. No requirements.txt, no venv, nothing to install via pip.

---

## Typical workflow

After `4-render-export.py` turns your finished `.blend` into an MP4:

```bash
python3 captions/captions.py ~/footage/trip/trip_edit_final.mp4
open ~/footage/trip/trip_edit_final_subbed.mp4
# Toggle captions in QuickTime: View > Subtitles
```

If you want pixels-baked captions for sharing on platforms that strip soft
subs:

```bash
python3 captions/captions.py ~/footage/trip/trip_edit_final.mp4 --burn
```

---

## Phrase grouping rules

Tunable constants at the top of `captions.py`:

- Hard-break after `.`, `?`, `!`.
- Soft-break after `,` if the current chunk has more than 3 words.
- Force-break if the chunk would exceed 7 words or 5 seconds of duration.
- Empty / pure-punctuation tokens from Whisper are dropped before chunking.

Edit those constants if you want longer or shorter phrase lines.
