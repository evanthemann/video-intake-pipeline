# Video Intake & Blender VSE Pipeline

Automates the journey from raw multi-camera footage (iPhone, GoPro, OBS, Canon Vixia / 7D, iVue Rincon, DJI drone, stills) to a rendered final MP4.

Run `0-check-deps.py` once on a new machine, then the numbered scripts in order: steps 1–3 get you to an edit-ready Blender VSE project, you do the editing, and step 4 renders the final cut to MP4.

---

## Pipeline Overview

Top-level scripts are numbered in run order, so a fresh clone lists them in the order you use them. Unnumbered folders are optional add-ons.

```
0-check-deps.py         — preflight: verify ffmpeg, exiftool, ImageMagick, Blender, … are installed
        │
        ▼
Raw footage (iPhone · GoPro · OBS · Canon · iVue · DJI · stills)
        │
        ▼
1-ingest.py             — scan folder, extract metadata; optionally pair clips with external audio or sync two camera angles; write manifest
        │
        ▼
2-transcode.py          — normalize all media to SDR · CFR 29.97 · 16:9 MP4; conform paired audio + measure offsets (external audio and camera-pairs)
        │
        ▼
3-import-vse.py         — create Blender project, import clips chronologically; overlap synced camera pairs on separate tracks
        │
        ▼
   [ edit in Blender ]  ←──┐  optionally via the KM cutting round:
        │                  │    blender-km-macros/  (macOS + Keyboard Maestro)
        │                  └──  validate-markers → cutting macro → remove-markers
        ▼                       loops back with the project number bumped each round
4-render-export.py      — render the final `.blend` to MP4 via PNG sequence + ffmpeg mux (real CRF/GOP/preset control)
```

---

## Dependencies

| Tool | Purpose | Install |
|---|---|---|
| `ffmpeg` + `ffprobe` | transcode, probe metadata | `brew install ffmpeg` |
| `avconvert` | iPhone HDR → SDR (macOS only) | built-in at `/usr/bin/avconvert` |
| ImageMagick `convert` + `identify` | image padding, dimension reads | `brew install imagemagick` |
| `exiftool` | EXIF orientation + timestamps on images; camera make/model detection on video | `brew install exiftool` |
| `blender` | headless VSE import, marker scripts, render | [blender.org](https://blender.org) or `brew install --cask blender` |
| `audio-offset-finder` | *optional* — sync external audio to video, align two camera angles. Only needed for external-audio or camera-sync pairing | `pipx install audio-offset-finder` then `pipx ensurepath`, then a new terminal (pipx itself: `brew install pipx`) |
| Keyboard Maestro | *optional, macOS only* — the `blender-km-macros/` cutting round | [keyboardmaestro.com](https://www.keyboardmaestro.com/) · setup in [`blender-km-macros/`](blender-km-macros/README.md) |

Run `python3 0-check-deps.py` to verify all of the above at once rather than checking by hand.

> **`audio-offset-finder` via pipx, not `pip install`.** The pipeline shells out to the
> `audio-offset-finder` *command*, so it only matters that the command is on `PATH`.
> A plain `pip install` drops it into whichever interpreter that particular `pip`
> belongs to — on a machine with several Pythons (Homebrew 3.11 *and* 3.14, plus
> system 3.9) that is routinely not the one you run the pipeline with, and the
> install "succeeds" while the command never appears. pipx gives it an isolated venv
> and a `PATH` entry, so it works regardless of interpreter. It also needs no write
> access to the Homebrew prefix, which matters on a shared Mac: a non-admin account
> can use a pipx that's already installed even though `brew install` would fail for
> it. Check-deps only tells you to install pipx when you don't already have it.
>
> `pipx install` on its own isn't enough: pipx puts the command in `~/.local/bin`,
> which isn't on `PATH` by default on macOS, so the package installs and stays
> invisible. `pipx ensurepath` fixes that, and since it edits your shell config you
> need a **new terminal** before the change takes. Check-deps spells out all three
> steps when the tool is missing.
>
> Check-deps prints the interpreter it's using in its header — compare that to
> `pip3 -V` if something installed but isn't being found.

Linux Mint: replace `avconvert` with ffmpeg zscale tone-map (handled automatically). `exiftool` via `sudo apt install libimage-exiftool-perl`, `pipx` via `sudo apt install pipx`.

---

## Scripts

### `0-check-deps.py`

Preflight check — verifies every external tool the pipeline shells out to is installed and reachable, so you find out now rather than halfway through a long transcode. Reports each tool's resolved path and version, separates required from optional, and prints the install command for anything missing. Exits non-zero if a required tool is absent, so it can gate a setup script.

It resolves tools using the same `PATH` + fallback-hint logic the pipeline scripts use, so a Blender it reports as found is the one they'll actually run. It also checks the Python version itself: the other scripts use 3.10+ syntax and won't even import on anything older.

```bash
python3 0-check-deps.py
python3 0-check-deps.py --quiet    # only report problems
```

Its scope is the numbered pipeline only. Each optional add-on ships its own checker, so this one stays meaningful on a machine that will never install Whisper or After Effects — and so each add-on can check things a generic tool-finder can't (whether your Keyboard Maestro macros are actually *imported*, whether your whisper.cpp build has `--vad`):

```bash
python3 captions/check-deps.py
python3 after-effects/check-deps.py
python3 blender-km-macros/check-deps.py
```

Run these once on a new machine. They're not part of the per-project flow — the numbered steps below are.

---

### `1-ingest.py`

Scans a footage folder recursively, extracts metadata via ffprobe / exiftool, and writes three output files to `<input_dir>/_ingest/`:

- `manifest.json` — machine-readable, consumed by 2-transcode.py
- `clips_ordered.txt` — human-readable chronological list
- `ingest_report.md` — counts, flags (HDR / VFR / vertical / missing timestamps / naive-local timestamps normalized to UTC / collapsed Live Photos + duplicates), external-audio + camera-sync pairings, timeline gaps

**What it detects per file:** source, camera model, media type, orientation, dimensions, duration, FPS, HDR, VFR, creation timestamp.

**Camera identification.** `source` is a short token used downstream: `iphone`, `gopro`, `obs`, `dji`, or a vendor brand (`canon`, `sony`, `panasonic`, …) for anything else. iPhones and GoPros come from cheap ffprobe/filename heuristics; OBS screen captures are identified by their default filename pattern (`YYYY-MM-DD HH-MM-SS.mkv`); DJI drones write no EXIF `Make`/`Model`, so they're caught by their encoder string, their `DJI meta` / `DJI dbgi` metadata handlers, or the `dji_` filename prefix; everything else falls through to an exiftool `Make`/`Model` lookup, so new camera brands work without code changes. `camera_model` carries the full string exposed by the file — e.g. `iPhone 16 Pro`, `HERO12 Black`, `Canon EOS 7D`, `Canon VIXIA HF R40`, `DJI Mini4 Pro` (read from the DJI encoder tag). Two iPhones of different generations in the same shoot show up distinctly in this field. OBS captures and anything else with no exposed model leave it as `—` in the report.

**External audio pairing.** After scanning, 1-ingest.py asks whether any video clips have a separate, higher-quality audio file (iPhone Voice Memo, dedicated recorder, etc.). If yes, you drag-and-drop each video clip and its matching audio file. The pairing is written to `manifest.json` as an `external_audio` field — no heavy processing happens at ingest time. The video clip keeps its native audio; in `2-transcode.py` (step 2) the paired file is conformed to a sibling WAV, `audio-offset-finder` measures the offset, and in `3-import-vse.py` (step 3) the conformed audio is dropped onto a separate VSE track aligned with the video.

**Camera-sync pairing.** 1-ingest.py then asks whether any two clips captured the same take from different cameras — e.g. an OBS screen recording (with a good USB mic) plus a Canon 7D angle. You drag-and-drop the **base** clip (its audio anchors the sync, typically OBS) and the **second camera** clip. Both must be scanned project files, and — unlike external audio — **both videos are preserved as separate files**. The pairing is written to `manifest.json` as a shared `sync_group` id with `sync_base` on the base clip. The actual offset measurement happens in `2-transcode.py` (step 2), and 3-import-vse.py (step 3) overlaps the pair on separate Blender tracks.

**Live Photos and duplicates.** A Live Photo is one moment stored as two files
(`IMG_1234.HEIC` + `IMG_1234.MOV`), and the same shot AirDropped between two phones lands in
two folders under the same name. Both cases would otherwise put one moment on the timeline
twice. Ingest collapses them using Apple's `ContentIdentifier` — the UUID that pairs a Live
Photo's still with its motion file, and that survives AirDrop even though the bytes don't
(AirDrop re-compresses, so the copies are not byte-identical). Policy: for a Live Photo the
**still is kept** and the motion file dropped; for duplicates the **highest-quality copy**
wins (most pixels, then largest file). Files with no `ContentIdentifier` — GoPro, non-Live
photos, non-Apple gear — fall back to exact-byte comparison within same-size groups, so no
real hashing cost is paid. Every collapsed file is listed in `ingest_report.md` with what
superseded it; nothing disappears silently. `--keep-duplicates` imports everything instead.

**Timezone normalization.** iPhone, GoPro, and DJI write `creation_time` in true UTC. Other
cameras — Canon Vixia, Canon 7D, iVue Rincon — write naive *local* wall-clock time but
still label it `Z`, so left alone they sort hours away from the Apple/GoPro/DJI clips. OBS
writes no `creation_time` tag at all, but its default filename template
(`%CCYY-%MM-%DD %hh-%mm-%ss.mkv`) encodes the local start-of-recording time, which ingest
parses as another naive-local source. Ingest then reads the real local offset from an
iPhone/GoPro clip in the same batch (e.g. `-0400`) and rewrites all naive clips to true
UTC so every clip shares one clock. If there is no Apple/GoPro clip in the batch it falls
back to this machine's timezone (and warns) — use `--local-offset` to set it explicitly.
Any per-camera embedded timezone tag is deliberately ignored, since it is often
misconfigured (the Vixia reports `-05:00` even when shooting in `-04:00`). Displayed times
in the reports are local wall-clock.

```bash
python3 1-ingest.py /path/to/footage
python3 1-ingest.py /path/to/footage --output /path/to/output_dir
python3 1-ingest.py /path/to/footage --local-offset -04:00   # footage shot in a zone with no Apple/GoPro clip
python3 1-ingest.py          # prompted — supports drag-and-drop
```

**Flags / options:**

| Flag | Description |
|---|---|
| `--output`, `-o` | Custom output directory (default: `<input_dir>/_ingest`) |
| `--local-offset` | UTC offset of the footage's local time, e.g. `-04:00`. Overrides offset detection for naive-local cameras (Canon, iVue). Default: read from an iPhone/GoPro clip, else this machine's timezone. |
| `--keep-duplicates` | Import every file, including Live Photo motion files and duplicate copies of the same shot. Default: collapse them to one clip each (see above). |

---

### `2-transcode.py`

Reads `_ingest/manifest.json` and normalizes every file. All output lands in `_ingest/transcoded/`. Already-transcoded files are skipped, so re-runs are safe.

**Output naming is per source file, not per stem.** Every input gets its own output. Naming
outputs after the filename stem alone silently loses files — `IMG_1234.HEIC` and
`IMG_1234.MOV`, or the same filename in two camera folders, all collapse onto `IMG_1234.mp4`,
and whichever is processed second hits "already exists", is skipped, and leaves a manifest
entry pointing at a *different* file's video. Ingest now collapses Live Photos and duplicates
upstream, so this is the backstop for what's left: genuinely different files that share a
name. Names escalate only as far as needed, each step naming what actually differs —
`IMG_1234.mp4` → `IMG_1234_heic.mp4` (same folder, different type) → `iphoneEvan__IMG_1234.mp4`
(same name, two folders) → `…__2.mp4`. Assignment is deterministic, so re-runs reuse prior work.

**"Already exists" means "already built from this file."** A provenance index
(`_ingest/transcoded/.transcode_index.json`) records which source produced each output, with
its size and mtime. A skip fires only when the output came from that exact source, unchanged —
so replacing a source file rebuilds its output instead of silently keeping the stale one.

**What it does per file type:**

| Input | Transform |
|---|---|
| Still image | 6-second slow-zoom MP4: 1s static lead-in + 5s Ken Burns zoom (libx264 · 29.97 fps · 1920×1080) |
| iPhone HDR video | `avconvert PresetAppleM4V1080pHD` → SDR .m4v, then ffmpeg for VFR/vertical/remux |
| Other HDR video | ffmpeg zscale tone-map → SDR |
| VFR video, or fps ≠ project rate (e.g. OBS at 30.000) | ffmpeg `-fps_mode cfr` → CFR 29.97 — avoids Blender VSE auto-flipping the scene fps and the 0.1% playback drift that puts video out of sync with audio |
| Vertical video | ffmpeg blurred 16:9 letterbox (portrait centred on blurred+darkened background) |
| GoPro / clean video | stream-copy (fast, no re-encode) |

Audio is normalized to **-16 LUFS / -1.5 dBTP** via `loudnorm` on all re-encoded clips.

**External audio overlay.** Clips that have an `external_audio` entry in the manifest (set during ingest) are transcoded normally — the video keeps its native audio. Afterward, the paired audio file is conformed to a sibling WAV next to the transcoded MP4 (`<basename>_extaudio.wav`) at the video's sample rate and channel count; PCM WAV avoids the AAC priming delay and on-the-fly resampling that drift Voice Memo (.m4a) tracks during VSE playback. The conformed WAV is **normalized to the same `-16 LUFS` target as every video clip** — this is the track you actually listen to (external audio exists because it is the better microphone, so the camera's own audio gets muted), which makes it the worst place to skip normalization. Normalization is two-pass with `linear=true`: an analysis pass measures the file, then a second pass applies one constant gain. Single-pass loudnorm is a *dynamic* filter that can move samples, which would undo the sample-exactness this step exists to guarantee, and it typically misses the target by a dB or more. A file too quiet to measure (digital silence) is conformed without normalization and warns, rather than having its noise floor amplified. `--no-loudnorm` disables normalization for video clips and external audio alike. `audio-offset-finder` then measures the offset between the conformed WAV and the transcoded video, and writes `external_audio_conformed_path` + `external_audio_offset_s` onto the entry. 3-import-vse.py (step 3) reads those and adds the WAV as a sound strip on channel 3, time-aligned with the video on channels 1/2.

**Camera-sync offsets.** For clips tagged with a `sync_group` (set during ingest), both angles are transcoded normally as separate files — nothing is muxed. Afterward, `audio-offset-finder` measures the audio offset between the two transcoded clips and writes `sync_offset_s` onto the second-camera entry. If the measurement fails or scores low, the pair is left at offset 0 with a warning (nudge it in Blender). 3-import-vse.py uses this offset to align the pair on separate tracks.

On completion writes `_ingest/transcoded/manifest_transcoded.json` — the input for 3-import-vse.py.

```bash
python3 2-transcode.py /path/to/project_folder
python3 2-transcode.py /path/to/project_folder --dry-run
python3 2-transcode.py          # prompted — supports drag-and-drop
```

**Flags / options:**

| Flag | Description |
|---|---|
| `--output`, `-o` | Custom output directory |
| `--fps` | Target CFR frame rate (default: `29.97`) |
| `--dry-run` | Print what would run, write nothing |
| `--no-loudnorm` | Skip audio loudness normalization |

---

### `3-import-vse.py`

Reads `manifest_transcoded.json`, prompts for project name and resolution, then launches Blender headlessly to create a `.blend` file with all clips placed on the VSE timeline in chronological order. Applies the Video Editing workspace layout and writes `blender_import.log`.

**Batched import (large projects).** Every movie strip holds an open ffmpeg decoder — roughly
35 MB each — so importing a large project in a single Blender process exhausts RAM. Measured on
a 360-clip project: memory climbed to 9.6 GB and flatlined, after which *every* remaining clip
came back as a 1-frame placeholder. The symptom points at the wrong thing — a per-file
`swscale can't transform from pixel format yuv420p to rgba` / `could not be loaded` on whichever
clip happened to be next. That clip is fine; it imports normally on its own and at an earlier
position.

Imports therefore run in batches (`--batch-size`, default 100), each in its own Blender process.
Reopening the `.blend` does not re-open the decoders, so peak memory tracks batch size rather
than total clip count. Each batch reports the frame the next one should start on, so the
timeline stays continuous. Use `--batch-size 0` to force a single process.

One detail is load-bearing: a resumed batch is given the `.blend` **on Blender's command
line**, never via `wm.open_mainfile()` from inside the running script — replacing the open file
frees the context that script is executing in, which segfaults Blender.

Strips are created with `bpy.ops.sequencer.*_strip_add`, whose defaults do real work:
`fit_method='FIT'` scales each strip to the scene, and `set_view_transform` picks the right
view transform for the media. Both matter here — transcode only normalizes *vertical* clips to
1920×1080, so a mixed shoot arrives with several resolutions at once, and graded footage left on
AgX gets re-tonemapped on render.

**Open-file limit.** Blender also keeps a file handle per strip, and macOS ships a soft limit of
256 — a secondary constraint that bit at 254 clips before the memory ceiling did. The script
raises it before launching Blender (two handles per clip plus headroom, capped to
`kern.maxfilesperproc`) and warns with the exact `ulimit -n` if it cannot.

**Failures are never silent.** `sequences.new_movie()` does not raise when Blender cannot read
the media; it returns a 1-frame placeholder. The import now checks every strip's duration and
fails loudly, rather than writing a `.blend` that looks complete but has silently dropped clips.

**Per-camera channel routing.** Every strip from the same physical camera lands on a shared VSE channel so per-camera mute/solo is one click. The routing key is the entry's `camera_model` (e.g. `Canon EOS 7D`, `Canon VIXIA HF R40`, `iPhone 16 Pro`, `HERO12 Black`, `DJI Mini4 Pro`), falling back to `source` for clips with no exposed model (OBS, anything without EXIF Make/Model) — so a Canon 7D and a Canon VIXIA never collide on one lane even though both report `source: "canon"`. Cameras fill channel pairs from ch1 upward in first-seen chronological order; whichever camera shoots first claims ch1/2. OBS is special-cased to always pin to ch1/2 (the bottom row) if any OBS clip is present, regardless of batch order, so the screen capture sits below the paired camera in a sync pair. External-audio sources (`zoom`, `voice-memo`, or a file-extension bucket — set by `1-ingest.py` from filename pattern + ffprobe handler tag) fill single channels above the highest camera pair. Channels are stable within a project: a second clip from the same camera lands on the same lane as the first, even if other cameras appear in between.

**Synced camera pairs.** A `sync_group` pair is placed as a single chronological slot with the two angles **overlapping on separate tracks**: each clip goes to its own camera's lane (e.g. an OBS+7D pair lands OBS on the OBS lane and 7D on the Canon lane, never colliding because they're different sources). The angle is shifted by the measured `sync_offset_s` so the same moment lines up. Both audio strips are imported active (mix or mute them in Blender). The pair reserves its combined span, after which solo clips resume sequentially.

**External-audio overlays.** A clip with `external_audio_conformed_path` is placed similarly: the video sits on its camera's lane (with its native audio intact), and the conformed audio WAV sits as a sound strip on its audio-source-assigned channel (e.g. every Zoom H1 file on one ch, every Voice Memo on another), shifted by `external_audio_offset_s`. Whichever started first anchors the slot at the cursor position — if the camera recorded before the audio, the previous clip butts up to the camera start; if the audio recorded before the video, the previous clip butts up to the audio start. The slot's end is whichever strip ends later.

```bash
python3 3-import-vse.py /path/to/project_folder
python3 3-import-vse.py /path/to/project_folder --name my_project
python3 3-import-vse.py          # prompted — supports drag-and-drop
```

**Flags / options:**

| Flag | Description |
|---|---|
| `--name`, `-n` | Project and `.blend` filename |
| `--blender` | Path to Blender executable (auto-detected if omitted) |
| `--dry-run` | Show import order and Blender command without running |

**Prompted interactively:** project name, resolution (1080p or 4K), confirmation of clip order.

---

### `4-render-export.py`

Renders your final cut `.blend` to a delivery-ready MP4 — outside Blender's built-in FFmpeg encoder, which doesn't expose real CRF/GOP/preset control. Instead: headlessly renders a lossless, resumable PNG image sequence via Blender's Python API (RGB, 8-bit, project fps), mixes the VSE's audio down to WAV via the Sequencer's sound mixdown, then shells out to **ffmpeg** to mux frames + WAV into the final MP4 — `libx264 -crf 18 -preset slow -pix_fmt yuv420p -c:a aac -b:a 192k`, matching the CRF 18 standard `2-transcode.py` already holds the rest of the pipeline to. The temp PNG sequence is deleted after a successful mux (`--keep-frames` to retain it for debugging a bad render).

```bash
python3 4-render-export.py /path/to/project.blend
python3 4-render-export.py                            # prompted — supports drag-and-drop
python3 4-render-export.py project.blend --handoff     # keyframe interval 1, for a
                                                       # scrub-friendly handoff to
                                                       # captions/ or after-effects/
                                                       # (bloats size — not for final delivery)
python3 4-render-export.py project.blend --dry-run     # show what would run, no render
python3 4-render-export.py project.blend --review      # small 480p review proxy of the
                                                       # whole timeline + a JSON frame
                                                       # map, for the marker round
```

**`--review`** renders a phone-friendly proxy instead of a delivery copy: JPEG intermediate frames, `--review-height` (default 480) applied via `resolution_percentage` so composition is untouched, CRF 28 / `veryfast` / `-g 30`, 96k audio. It writes two files — `<stem>_review.mp4` and `<stem>_review.json`, the frame map that turns a position in the proxy back into a Blender scene frame (`frame = round(seconds × fps_num ÷ fps_den) + frame_start`). Copy both to the machine running [cuts](https://github.com/evanthemann/cuts), place the F/u pairs on your phone, and bring the resulting markers JSON back. See the Roadmap entry for the round-trip.

Other flags: `--output/-o` (default: sibling `<blend_stem>.mp4`), `--crf` (default 18), `--preset` (default `slow`), `--blender` (override auto-detected Blender path), `--keep-frames`. The closing next-step hint points at the two optional add-ons below.

---

### Optional add-on: `blender-km-macros/` — the cutting round

**macOS + Keyboard Maestro only.** The fastest way to trim raw footage: mark the segments you want to keep, then let a Keyboard Maestro macro perform all the cuts in Blender for you. It's a self-contained loop that sits inside the "edit in Blender" phase between steps 3 and 4, and you can run it as many rounds as you like.

The whole F/u marker convention exists to drive this macro — **if you're not using Keyboard Maestro, you wouldn't place these markers at all**; you'd just edit, cut, and save-as in Blender directly, then go to step 4.

One round:

```bash
# 1. Place F / u markers in Blender on the segments to keep, then validate:
python3 blender-km-macros/validate-markers.py ~/footage/trip/trip_edit_1.blend
#    → saves trip_edit_1_cut.blend and prints the KM loop count N

# 2. Open the _cut.blend in Blender, mouse over the VSE timeline, run the macro:
./blender-km-macros/scripts/trigger.sh <N>
./blender-km-macros/scripts/trigger.sh --test <N>   # dry run — deletes nothing

# 3. Clean up the markers; bumps the project number for the next round:
python3 blender-km-macros/remove-markers.py ~/footage/trip/trip_edit_1_cut.blend
#    → writes trip_edit_2.blend
```

The chain advances one number per round (`_1.blend` → `_1_cut.blend` → `_2.blend` → `_2_cut.blend` → `_3.blend` → …). Loop as many times as you need, then render the latest `.blend` with step 4.

**Marker convention:** markers come in pairs — the start of a keep-range is named `F1`, `F2`, etc.; the matching end is named exactly `u`. `validate-markers.py` checks for an even count, correct alternating F…/u naming, and no overlapping ranges, then saves the `_cut.blend` copy and prints the loop count. `trigger.sh` won't fire if Blender isn't running. See [`blender-km-macros/README.md`](blender-km-macros/README.md) for importing the `.kmmacros` files, the safety / TEST flow, and the macro internals.

### Optional add-on: `captions/`

After you've rendered your cut Blender project to MP4, [`captions/`](captions/README.md) auto-generates captions via [whisper.cpp](https://github.com/ggerganov/whisper.cpp) and either soft-embeds them as a toggleable `mov_text` track (default) or hard-burns them into a pixels-baked copy. Not part of the main pipeline — install separately (`brew install whisper-cpp`, plus a one-time `curl` for the transcription model and the Silero VAD model) only when you want it — `captions/check-deps.py` reports what's missing and which hallucination guards are active. The script itself is stdlib-only Python, same architecture as the rest of the pipeline (orchestration shelling out to external binaries — `whisper-cli` joins `ffmpeg` / `blender` / `audio-offset-finder` in that role). Many runs just upload to YouTube and let YouTube do the captioning, so the main pipeline stays Whisper-free.

### Repair tool: `tools/fix-camera-clock.py`

For a card shot with a wrong camera clock — never set, or reset by a dead battery. Not part of
a normal run; use it **before** `1-ingest.py` when a camera's dates are obviously wrong.

This matters more than it looks. Ingest trusts GoPro / iPhone / DJI clips as true UTC and never
corrects them, so a card with a bad clock sorts years away from everything else and stacks at
the front of the timeline. The fix shifts every embedded timestamp by one fixed amount, so
clips keep their true *relative* spacing and land in the right place in the batch.

Give it one clip and when that clip was really recorded:

```bash
# dry run — prints a before/after table, writes nothing
./tools/fix-camera-clock.py /path/to/100GOPRO --anchor GX010346.MP4 --actual "2026-09-05 10:05"

# apply
./tools/fix-camera-clock.py /path/to/100GOPRO --anchor GX010346.MP4 --actual "2026-09-05 10:05" --apply
```

**Read the anchor time from the camera, not from Finder.** MP4 stores creation time as UTC and
Finder renders it using the timezone rules *for the stored date* — so a clip whose bogus date
falls in January is displayed with winter-time rules even though it was really shot in summer,
putting Finder's reading an hour out. `--actual` is local wall-clock (`--tz` to override), and
the offset is computed in UTC, so the result is right regardless of what anything displays.

| Flag | Description |
|---|---|
| `--anchor` / `--actual` | Reference clip and its true recording time. |
| `--offset` | Explicit shift instead of an anchor, e.g. `'0:0:3897 20:20:0'` (`Y:M:D h:m:s`). |
| `--tz` | Timezone `--actual` is in. Default: this machine's current offset. |
| `--before` | Only shift clips stored earlier than this date. Default: one year after the anchor's stored date — which selects the bad-clock clips and leaves correctly-dated ones alone. |
| `--apply` | Actually write. **Dry run is the default.** |
| `--allow-synced` | Permit running inside a cloud-synced folder. Refused by default, since rewriting there edits the master copy and re-uploads every file. |
| `--skip-telemetry-check` | Skip hashing GoPro GPMF telemetry before/after. Faster, weaker evidence. |

Every applied run verifies each file afterward: the new timestamp is what was predicted, the
file size and stream layout are unchanged, the container still parses, and GoPro GPMF telemetry
hashes identically. Any failure is listed and the run exits non-zero. Clips with no readable
timestamp (a corrupt file, say) are reported and left alone rather than guessed at.

The shift is exactly invertible — every run prints the `--offset` that undoes it.

### Optional add-on: `after-effects/`

For shoots that get cut in Blender (fastest for raw-footage trim work via the KM macro) but finished in After Effects (titles, motion graphics, finer compositing), [`after-effects/`](after-effects/README.md) exports your final `<project>_<N>.blend` timeline to JSON and rebuilds it as an AE composition called `Blender_VSE`. Stdlib-only Python wrapper that runs Blender headlessly, plus an ExtendScript that AE runs on the JSON. Per-clip timeline placement, in/out points, scale, and translation all carry through; the Blender VSE channel becomes the AE layer stack order, so the per-camera lane routing from `3-import-vse.py` survives the round trip. Not part of the main pipeline — only used when you finish in AE.

---

## Folder Structure (after full run)

```
project_folder/
├── _ingest/
│   ├── manifest.json               ← ingest output
│   ├── clips_ordered.txt
│   ├── ingest_report.md
│   └── transcoded/
│       ├── manifest_transcoded.json  ← transcode output
│       ├── .transcode_index.json     ← which source produced each output (skip provenance)
│       ├── *.mp4                     ← normalized clips
│       └── *_extaudio.wav            ← conformed external-audio strips (one per paired clip)
├── my_project_1.blend              ← VSE project, from 3-import-vse.py
├── my_project_1_cut.blend          ← KM cutting round only: copy with markers, macro cuts here
├── my_project_2.blend              ← KM cutting round only: markers removed, number bumped
│                                     (cycle continues: _2_cut.blend → _3.blend → …)
├── my_project_2.mp4                ← 4-render-export.py output
└── blender_import.log
```

Without the KM cutting round it's simpler — you edit `my_project_1.blend` in Blender directly (saving as whatever you like) and render that.

---

## Typical Full Run

```bash
# 0. Once per machine — verify dependencies
python3 0-check-deps.py

# 1. Ingest
python3 1-ingest.py ~/footage/trip

# 2. Transcode (can take a while — grab a coffee)
python3 2-transcode.py ~/footage/trip

# 3. Import into Blender
python3 3-import-vse.py ~/footage/trip --name trip_edit
# (or just `python3 3-import-vse.py ~/footage/trip` — the prompt defaults to
#  trip_1, auto-bumping if higher-numbered projects already exist)

# --- Edit trip_edit_1.blend in Blender ---
#
# OPTIONAL, macOS + Keyboard Maestro — the cutting round. Place F / u markers on
# the segments to keep, then loop these three as many rounds as you want:
#
#   python3 blender-km-macros/validate-markers.py ~/footage/trip/trip_edit_1.blend
#   ./blender-km-macros/scripts/trigger.sh <N>       # N = loop count just printed
#   python3 blender-km-macros/remove-markers.py ~/footage/trip/trip_edit_1_cut.blend
#
# → writes trip_edit_2.blend; repeat against that for another round.
# Not using Keyboard Maestro? Skip all of the above — just cut and save in
# Blender however you like, then render whatever .blend you ended up with.

# 4. Render the final MP4
python3 4-render-export.py ~/footage/trip/trip_edit_2.blend
```

---

## Platform Notes

- **macOS** is the primary target. `avconvert` (iPhone HDR→SDR) is macOS-only; on Linux, the pipeline falls back to ffmpeg's zscale tone-map automatically.
- Every script that takes a path supports **drag-and-drop** input when run without arguments — useful in Terminal on macOS. All support `--help`.
- Pipeline scripts require **Python 3.10+**; `0-check-deps.py` runs on older interpreters so it can tell you that.
- Blender is auto-detected on `PATH` and at common install locations (`/Applications/Blender.app` on macOS, `/usr/bin/blender` / `/usr/local/bin/blender` / `~/blender/blender` on Linux). Pass `--blender /path/to/blender` to override.

---

## Roadmap

- **360° angle picker (optional step 1.5: `360picker/`)** — handle 360° equirectangular footage. At **ingest** the "any 360° video?" prompt (alongside the external-audio / multicam questions) just flags those clips in the manifest with their **original creation timestamp**, then recommends running the step-1.5 picker tool before transcode. The **`360picker/` tool** lets you dial in a yaw / pitch / FOV angle against a preview and writes those four numbers back onto the manifest entry (it creates no media). **`2-transcode.py`** then does the actual rectilinear extraction via ffmpeg's `v360` filter, headless, with the output inheriting the original timestamp — so the flattened angle sorts into the chronological timeline correctly and the coffee-break batch stays unattended. **Build it ffmpeg-first:** render previews with `ffmpeg v360` itself (stdlib loop, zero new deps); only if that preview-refresh feels too clunky, upgrade the picker to an `opencv-python`/`numpy` live viewport for buttery-smooth real-time panning (those deps then stay inside `360picker/`, never the global table).
- **Mobile marker round — review proxy out, F/u markers back in** — place the cutting round's markers on a phone instead of at the Blender timeline. **`4-render-export.py --review` is built** (2026-09-22); the importer is not.
  - **`--review`** — a render preset alongside `--handoff`, not a new pipeline step. Same render path, different dials: JPEG intermediates instead of PNG (a review proxy covers the *whole uncut* timeline — a 2-hour 30fps project is ~216k frames, where lossless PNG runs to tens of GB of scratch for no visible gain), `--review-height 480` via `resolution_percentage` (**never** `resolution_x/y` — `3-import-vse.py` fits strips to the scene size, so changing the scene's pixel dimensions would re-frame every clip), `-crf 28 -preset veryfast -g 30`, 96k audio. Writes `<stem>_review.mp4` plus a `<stem>_review.json` frame map: `frame_start`/`frame_end`, the exact fps rational recovered from Blender's `fps`/`fps_base` pair, and the dimensions. A scaled render can land on an odd pixel height, which yuv420p refuses, so the mux rounds down to even rather than failing.
  - **The frame map is a sidecar, not MP4 metadata.** Custom tags do survive a mux (verified, with `-movflags use_metadata_tags`), but a separate file keeps the proxy an ordinary video, stays readable and hand-editable, and lets the cuts side parse it without shelling out to ffprobe. The two are matched by filename.
  - **Marking happens in [cuts](https://github.com/evanthemann/cuts)** — `markers.php`, built 2026-09-22. Alternating F/u buttons, frame-accurate transport, autosave, exporting `<stem>_review.markers.json`. Transport between the two machines is `scp` into the cuts host's `review/` folder for now.
  - **`mobile-markers/import-markers.py`** — still to build. New optional add-on folder (own `README.md` + `check-deps.py`, stdlib-only, no new global dependencies). Interactive in the same drag-and-drop style as the rest of the pipeline: prompts for the `.blend`, then for the markers JSON, applies the markers to the timeline, and saves **non-destructively** as `<stem>_cut-markers.blend` — the original is never touched. **It maps markers onto the timeline and stops there.** It does not cut, and does not try to build a trimmed project headlessly: the cutting stays a deliberate, watched step in Blender via the Keyboard Maestro round (`validate-markers.py` → macro → `remove-markers.py`), which is exactly the input its output is shaped for.

  Prior art: `~/projects/blender-vse-remote-review` solves the same problem by driving the real Blender UI over Jump Desktop. This replaces the remote-desktop round-trip with a plain video file and a web page; the two can coexist.
- **Proxy generation** — re-add `redo_proxies.py` as a post-import step; build 25% proxies for smooth VSE playback without leaving Blender
- **Premiere Pro export** — convert the Blender VSE timeline to a Premiere-compatible project file (via XML/EDL or the `.prproj` format) for finishing in Premiere Pro
- **Web frontend (PHP)** — explore a browser UI for the pipeline, modeled loosely on [cuts](https://github.com/evanthemann/cuts). Not everything the CLI does is feasible in a web context (long-running transcodes, headless Blender, local file paths), but ingest review, manifest inspection, clip ordering, and job status monitoring are strong candidates. Worth exploring deeply.
