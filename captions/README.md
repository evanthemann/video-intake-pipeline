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

# Small is a good default — fast, accurate enough for most edits.
curl -L -o ~/whisper-models/ggml-small.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
```

| Model    | Size    | Speed (Apple Silicon w/ Metal) | Quality       |
|----------|---------|--------------------------------|---------------|
| `tiny`   | ~75 MB  | fastest                        | rough         |
| `base`   | ~140 MB | very fast                      | usable        |
| `small`  | ~466 MB | fast (default)                 | good          |
| `medium` | ~1.5 GB | medium                         | very good     |
| `large`  | ~3 GB   | slowest                        | best          |

Replace `ggml-small.bin` in the URL and filename with whichever size you
want. If you request a size whose file isn't present, `captions.py` will
print the exact `curl` command for it and exit cleanly.

`~/whisper-models/` is the default search location. Override with
`--model-dir` or pass an explicit path with `--model /path/to/model.bin`.

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
| `--model`      | `small`               | Size keyword (`tiny`/`base`/`small`/`medium`/`large`) or a `.bin` path. |
| `--model-dir`  | `~/whisper-models`    | Where size keywords are resolved.                                    |
| `--language`   | `auto`                | ISO code (`en`, `es`, `de`, …) or `auto` for whisper.cpp to detect.  |
| `--font`       | `Arial-Bold`          | Burn-in font name (must be installed on macOS). Ignored for SRT/soft.|
| `--size`       | 60 / 90               | Burn-in size: 60 phrase, 90 word-by-word. Ignored for SRT/soft.      |

---

## Files in this folder

```
captions/
├── README.md      ← this file
└── captions.py    ← the tool — stdlib Python, shells out to whisper-cli + ffmpeg
```

Just two files. No requirements.txt, no venv, nothing to install via pip.

---

## Typical workflow

After the main 5-step pipeline produces a cut `.blend`, render it to MP4
in Blender, then:

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
