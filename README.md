# Video Intake & Blender VSE Pipeline

Automates the journey from raw multi-camera footage (iPhone, GoPro, OBS, Canon Vixia / 7D, iVue Rincon, stills) to an edit-ready Blender VSE project. Run the four scripts in order, then do your manual editing in Blender, then use the two marker scripts to prep for and clean up after the Keyboard Maestro cutting macro.

---

## Pipeline Overview

```
Raw footage (iPhone · GoPro · OBS · Canon · iVue · stills)
        │
        ▼
1. ingest.py          — scan folder, extract metadata; optionally pair clips with external audio or sync two camera angles; write manifest
        │
        ▼
2. transcode.py       — normalize all media to SDR · CFR 29.97 · 16:9 MP4; conform paired audio + measure offsets (external audio and camera-pairs)
        │
        ▼
3. import-vse.py      — create Blender project, import clips chronologically; overlap synced camera pairs on separate tracks
        │
        ▼
   [ manual editing in Blender — place F / u markers ]
        │
        ▼
4. vse-validate-markers.py   — validate marker pairs, save _cut.blend, print KM loop count
        │
        ▼
   [ run Keyboard Maestro cutting macro — blender-km-macros/scripts/trigger.sh N ]
        │
        ▼
5. vse-remove-markers.py     — wipe all timeline markers, save in place
        │
        ▼
   Edit-ready Blender project
```

---

## Dependencies

| Tool | Purpose | Install |
|---|---|---|
| `ffmpeg` + `ffprobe` | transcode, probe metadata | `brew install ffmpeg` |
| `avconvert` | iPhone HDR → SDR (macOS only) | built-in at `/usr/bin/avconvert` |
| ImageMagick `convert` + `identify` | image padding, dimension reads | `brew install imagemagick` |
| `exiftool` | EXIF orientation + timestamps on images; camera make/model detection on video | `brew install exiftool` |
| `blender` | headless VSE import + marker scripts | [blender.org](https://blender.org) or `brew install --cask blender` |
| Keyboard Maestro | run the clip-cutting macro (step 4.5) — macOS only | [keyboardmaestro.com](https://www.keyboardmaestro.com/) · setup in [`blender-km-macros/`](blender-km-macros/README.md) |
| `audio-offset-finder` | sync external audio to video, and align two camera angles (optional — only needed for external audio or camera-sync pairing) | `pip install audio-offset-finder` |

Linux Mint: replace `avconvert` with ffmpeg zscale tone-map (handled automatically). `exiftool` via `sudo apt install libimage-exiftool-perl`.

---

## Scripts

### 1 — `ingest.py`

Scans a footage folder recursively, extracts metadata via ffprobe / exiftool, and writes three output files to `<input_dir>/_ingest/`:

- `manifest.json` — machine-readable, consumed by transcode.py
- `clips_ordered.txt` — human-readable chronological list
- `ingest_report.md` — counts, flags (HDR / VFR / vertical / missing timestamps / naive-local timestamps normalized to UTC), external-audio + camera-sync pairings, timeline gaps

**What it detects per file:** source, camera model, media type, orientation, dimensions, duration, FPS, HDR, VFR, creation timestamp.

**Camera identification.** `source` is a short token used downstream: `iphone`, `gopro`, `obs`, or a vendor brand (`canon`, `sony`, `panasonic`, …) for anything else. iPhones and GoPros come from cheap ffprobe/filename heuristics; OBS screen captures are identified by their default filename pattern (`YYYY-MM-DD HH-MM-SS.mkv`); everything else falls through to an exiftool `Make`/`Model` lookup, so new camera brands work without code changes. `camera_model` carries the full string exposed by the file — e.g. `iPhone 16 Pro`, `HERO12 Black`, `Canon EOS 7D`, `Canon VIXIA HF R40`. Two iPhones of different generations in the same shoot show up distinctly in this field. OBS captures and anything else with no Make/Model leave it as `—` in the report.

**External audio pairing.** After scanning, ingest.py asks whether any video clips have a separate, higher-quality audio file (iPhone Voice Memo, dedicated recorder, etc.). If yes, you drag-and-drop each video clip and its matching audio file. The pairing is written to `manifest.json` as an `external_audio` field — no heavy processing happens at ingest time. The video clip keeps its native audio; in `transcode.py` (step 2) the paired file is conformed to a sibling WAV, `audio-offset-finder` measures the offset, and in `import-vse.py` (step 3) the conformed audio is dropped onto a separate VSE track aligned with the video.

**Camera-sync pairing.** ingest.py then asks whether any two clips captured the same take from different cameras — e.g. an OBS screen recording (with a good USB mic) plus a Canon 7D angle. You drag-and-drop the **base** clip (its audio anchors the sync, typically OBS) and the **second camera** clip. Both must be scanned project files, and — unlike external audio — **both videos are preserved as separate files**. The pairing is written to `manifest.json` as a shared `sync_group` id with `sync_base` on the base clip. The actual offset measurement happens in `transcode.py` (step 2), and import-vse.py (step 3) overlaps the pair on separate Blender tracks.

**Timezone normalization.** iPhone and GoPro write `creation_time` in true UTC. Other
cameras — Canon Vixia, Canon 7D, iVue Rincon — write naive *local* wall-clock time but
still label it `Z`, so left alone they sort hours away from the Apple/GoPro clips. OBS
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
python3 ingest.py /path/to/footage
python3 ingest.py /path/to/footage --output /path/to/output_dir
python3 ingest.py /path/to/footage --local-offset -04:00   # footage shot in a zone with no Apple/GoPro clip
python3 ingest.py          # prompted — supports drag-and-drop
```

**Flags / options:**

| Flag | Description |
|---|---|
| `--output`, `-o` | Custom output directory (default: `<input_dir>/_ingest`) |
| `--local-offset` | UTC offset of the footage's local time, e.g. `-04:00`. Overrides offset detection for naive-local cameras (Canon, iVue). Default: read from an iPhone/GoPro clip, else this machine's timezone. |

---

### 2 — `transcode.py`

Reads `_ingest/manifest.json` and normalizes every file. All output lands in `_ingest/transcoded/`. Already-transcoded files are skipped, so re-runs are safe.

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

**External audio overlay.** Clips that have an `external_audio` entry in the manifest (set during ingest) are transcoded normally — the video keeps its native audio. Afterward, the paired audio file is conformed to a sibling WAV next to the transcoded MP4 (`<basename>_extaudio.wav`) at the video's sample rate and channel count; PCM WAV avoids the AAC priming delay and on-the-fly resampling that drift Voice Memo (.m4a) tracks during VSE playback. `audio-offset-finder` then measures the offset between the conformed WAV and the transcoded video, and writes `external_audio_conformed_path` + `external_audio_offset_s` onto the entry. import-vse.py (step 3) reads those and adds the WAV as a sound strip on channel 3, time-aligned with the video on channels 1/2.

**Camera-sync offsets.** For clips tagged with a `sync_group` (set during ingest), both angles are transcoded normally as separate files — nothing is muxed. Afterward, `audio-offset-finder` measures the audio offset between the two transcoded clips and writes `sync_offset_s` onto the second-camera entry. If the measurement fails or scores low, the pair is left at offset 0 with a warning (nudge it in Blender). import-vse.py uses this offset to align the pair on separate tracks.

On completion writes `_ingest/transcoded/manifest_transcoded.json` — the input for import-vse.py.

```bash
python3 transcode.py /path/to/project_folder
python3 transcode.py /path/to/project_folder --dry-run
python3 transcode.py          # prompted — supports drag-and-drop
```

**Flags / options:**

| Flag | Description |
|---|---|
| `--output`, `-o` | Custom output directory |
| `--fps` | Target CFR frame rate (default: `29.97`) |
| `--dry-run` | Print what would run, write nothing |
| `--no-loudnorm` | Skip audio loudness normalization |

---

### 3 — `import-vse.py`

Reads `manifest_transcoded.json`, prompts for project name and resolution, then launches Blender headlessly to create a `.blend` file with all clips placed on the VSE timeline in chronological order. Applies the Video Editing workspace layout and writes `blender_import.log`.

**Per-source channel routing.** Every strip from the same camera or recorder lands on a shared VSE channel so per-source mute/solo is one click. Camera sources (the entry's `source` value — `iphone`, `gopro`, `canon`, `obs`, …) fill channel pairs from ch1 upward in first-seen chronological order; whichever camera shoots first claims ch1/2. External-audio sources (`zoom`, `voice-memo`, or a file-extension bucket — set by `ingest.py` from filename pattern + ffprobe handler tag) fill single channels above the highest camera pair. Channels are stable within a project: a second clip from the same camera lands on the same lane as the first, even if other cameras appear in between.

**Synced camera pairs.** A `sync_group` pair is placed as a single chronological slot with the two angles **overlapping on separate tracks**: each clip goes to its own camera's lane (e.g. an OBS+7D pair lands OBS on the OBS lane and 7D on the Canon lane, never colliding because they're different sources). The angle is shifted by the measured `sync_offset_s` so the same moment lines up. Both audio strips are imported active (mix or mute them in Blender). The pair reserves its combined span, after which solo clips resume sequentially.

**External-audio overlays.** A clip with `external_audio_conformed_path` is placed similarly: the video sits on its camera's lane (with its native audio intact), and the conformed audio WAV sits as a sound strip on its audio-source-assigned channel (e.g. every Zoom H1 file on one ch, every Voice Memo on another), shifted by `external_audio_offset_s`. Whichever started first anchors the slot at the cursor position — if the camera recorded before the audio, the previous clip butts up to the camera start; if the audio recorded before the video, the previous clip butts up to the audio start. The slot's end is whichever strip ends later.

```bash
python3 import-vse.py /path/to/project_folder
python3 import-vse.py /path/to/project_folder --name my_project
python3 import-vse.py          # prompted — supports drag-and-drop
```

**Flags / options:**

| Flag | Description |
|---|---|
| `--name`, `-n` | Project and `.blend` filename |
| `--blender` | Path to Blender executable (auto-detected if omitted) |
| `--dry-run` | Show import order and Blender command without running |

**Prompted interactively:** project name, resolution (1080p or 4K), confirmation of clip order.

---

### 4 — `vse-validate-markers.py`

Opens a `.blend` file in Blender headlessly, reads all timeline markers, and validates them as F / u pairs before you run the Keyboard Maestro cutting macro.

**Marker convention:** markers must come in pairs — the start of a keep-range is named `F1`, `F2`, etc.; the matching end is named exactly `u`. Each pair defines one segment to retain.

**What it checks:**
- Even number of markers (every start has an end)
- Alternating F… / u naming
- No overlapping ranges

On success: saves a `_cut.blend` copy of your project and prints the number of KM macro loops needed.

```bash
python3 vse-validate-markers.py /path/to/project.blend
python3 vse-validate-markers.py          # prompted — supports drag-and-drop
```

---

### 4.5 — Keyboard Maestro cutting macro (`blender-km-macros/`)

Between validating markers and removing them, run the Keyboard Maestro macro that
performs the actual cuts in Blender. It lives in
[`blender-km-macros/`](blender-km-macros/README.md), which has the full setup guide —
importing the `.kmmacros` files into Keyboard Maestro and the safety / TEST flow.

Once the macros are imported, trigger the cutting macro with the loop count that
step 4 printed:

```bash
./blender-km-macros/scripts/trigger.sh <N>          # N = KM loop count from step 4
./blender-km-macros/scripts/trigger.sh --test <N>   # dry run — deletes nothing
```

`trigger.sh` won't fire if Blender isn't running. See the subfolder README for the
macro internals and import notes.

---

### 5 — `vse-remove-markers.py`

Opens a `.blend` file in Blender headlessly, removes every timeline marker, and saves in place. Run this after the Keyboard Maestro macro has finished cutting.

```bash
python3 vse-remove-markers.py /path/to/project.blend
python3 vse-remove-markers.py          # prompted — supports drag-and-drop
```

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
│       ├── *.mp4                     ← normalized clips
│       └── *_extaudio.wav            ← conformed external-audio strips (one per paired clip)
├── my_project.blend                ← VSE project
├── my_project_cut.blend            ← copy with cuts applied (post-KM)
└── blender_import.log
```

---

## Typical Full Run

```bash
# 1. Ingest
python3 ingest.py ~/footage/trip

# 2. Transcode (can take a while — grab a coffee)
python3 transcode.py ~/footage/trip

# 3. Import into Blender
python3 import-vse.py ~/footage/trip --name trip_edit

# --- Open trip_edit.blend, do manual editing, place F/u markers ---

# 4. Validate markers
python3 vse-validate-markers.py ~/footage/trip/trip_edit.blend

# 4.5 Run the Keyboard Maestro cutting macro (N = loop count printed by step 4)
./blender-km-macros/scripts/trigger.sh <N>

# 5. Clean up markers
python3 vse-remove-markers.py ~/footage/trip/trip_edit_cut.blend
```

---

## Platform Notes

- **macOS** is the primary target. `avconvert` (iPhone HDR→SDR) is macOS-only; on Linux, the pipeline falls back to ffmpeg's zscale tone-map automatically.
- All scripts support **drag-and-drop** path input when run without arguments — useful in Terminal on macOS.
- Blender is auto-detected on `PATH` and at common install locations (`/Applications/Blender.app` on macOS, `/usr/bin/blender` / `/usr/local/bin/blender` / `~/blender/blender` on Linux). Pass `--blender /path/to/blender` to override.

---

## Roadmap

- **Multi-model cameras from one brand share a lane (bug)** — per-camera channel allocation uses the `source` field from ingest, which is currently brand-only (Canon EOS 7D and Canon VIXIA HF R40 both report `source: "canon"`). Result: footage from both cameras lands on the same VSE lane, breaking per-camera mute/solo. Filename pattern (both happen to be `MVI_####`) is a coincidence, not the cause. Fix needs a more specific routing key — likely the `camera_model` string already on the manifest, either as the primary routing key or as a tiebreaker when distinct models share a `source`. Decide which at implementation time so an iPhone-on-iPhone-on-iPhone shoot doesn't fragment unnecessarily (or does, if that's preferred).
- **Enumerate `.blend` outputs on each cut run** — `vse-validate-markers.py` currently writes `<name>_cut.blend`, overwriting any previous cut from earlier runs of the same project. Suffix subsequent runs with `_2`, `_3`, etc. (e.g. `<name>_cut.blend`, then `<name>_cut_2.blend`, then `<name>_cut_3.blend`) so historical cuts are preserved for comparison or rollback. Companion change: `import-vse.py` should suggest `<name>_1.blend` as the default project filename on creation, so the cut chain starts at `<name>_1_cut.blend`, `<name>_1_cut_2.blend`, etc. — keeps the numbering convention consistent from the project's first .blend onward. Decide whether the follow-up `vse-remove-markers.py` next-step hint always points at the latest, or asks.
- **Proxy generation** — re-add `redo_proxies.py` as a post-import step; build 25% proxies for smooth VSE playback without leaving Blender
- **Subtitles** — auto-generate or import SRT / VTT and burn or soft-attach to the Blender timeline
- **After Effects export** — convert the VSE timeline to an AE-compatible project file (via ExtendScript or `aescript` bridge) for finishing in After Effects
