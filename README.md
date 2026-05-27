# Video Intake & Blender VSE Pipeline

Automates the journey from raw multi-camera footage (iPhone, GoPro, Canon Vixia / 7D, iVue Rincon, stills) to an edit-ready Blender VSE project. Run the four scripts in order, then do your manual editing in Blender, then use the two marker scripts to prep for and clean up after the Keyboard Maestro cutting macro.

---

## Pipeline Overview

```
Raw footage (iPhone · GoPro · Canon · iVue · stills)
        │
        ▼
1. ingest.py          — scan folder, extract metadata; optionally pair clips with external audio files; write manifest
        │
        ▼
2. transcode.py       — normalize all media to SDR · CFR 29.97 · 16:9 MP4; sync + replace audio for clips paired in step 1
        │
        ▼
3. import-vse.py      — create Blender project, import clips in chronological order
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
| `exiftool` | EXIF orientation + timestamps on images | `brew install exiftool` |
| `blender` | headless VSE import + marker scripts | [blender.org](https://blender.org) or `brew install --cask blender` |
| Keyboard Maestro | run the clip-cutting macro (step 4.5) — macOS only | [keyboardmaestro.com](https://www.keyboardmaestro.com/) · setup in [`blender-km-macros/`](blender-km-macros/README.md) |
| `audio-offset-finder` | sync external audio to video (optional — only needed if you use external audio files) | `pip install audio-offset-finder` |

Linux Mint: replace `avconvert` with ffmpeg zscale tone-map (handled automatically). `exiftool` via `sudo apt install libimage-exiftool-perl`.

---

## Scripts

### 1 — `ingest.py`

Scans a footage folder recursively, extracts metadata via ffprobe / exiftool, and writes three output files to `<input_dir>/_ingest/`:

- `manifest.json` — machine-readable, consumed by transcode.py
- `clips_ordered.txt` — human-readable chronological list
- `ingest_report.md` — counts, flags (HDR / VFR / vertical / missing timestamps), timeline gaps

**What it detects per file:** source (GoPro / iPhone / unknown), media type, orientation, dimensions, duration, FPS, HDR, VFR, creation timestamp.

**External audio pairing.** After scanning, ingest.py asks whether any video clips have a separate, higher-quality audio file (iPhone Voice Memo, dedicated recorder, etc.). If yes, you drag-and-drop each video clip and its matching audio file. The pairing is written to `manifest.json` as an `external_audio` field — no heavy processing happens at ingest time. The actual sync and audio replacement runs in `transcode.py` (step 2) using `audio-offset-finder` to align the tracks automatically.

**Timezone normalization.** iPhone and GoPro write `creation_time` in true UTC. Other
cameras — Canon Vixia, Canon 7D, iVue Rincon — write naive *local* wall-clock time but
still label it `Z`, so left alone they sort hours away from the Apple/GoPro clips. Ingest
detects clips with no UTC proof, reads the real local offset from an iPhone/GoPro clip in
the same batch (e.g. `-0400`), and rewrites the naive clips to true UTC so every clip
shares one clock. If there is no Apple/GoPro clip in the batch it falls back to this
machine's timezone (and warns) — use `--local-offset` to set it explicitly. Any per-camera
embedded timezone tag is deliberately ignored, since it is often misconfigured (the Vixia
reports `-05:00` even when shooting in `-04:00`). Displayed times in the reports are local
wall-clock.

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
| VFR video | ffmpeg `-fps_mode cfr` → CFR 29.97 |
| Vertical video | ffmpeg blurred 16:9 letterbox (portrait centred on blurred+darkened background) |
| GoPro / clean video | stream-copy (fast, no re-encode) |

Audio is normalized to **-16 LUFS / -1.5 dBTP** via `loudnorm` on all re-encoded clips.

**External audio replacement.** Clips that have an `external_audio` entry in the manifest (set during ingest) are processed with `audio-offset-finder` to compute the sync offset, then ffmpeg replaces the native audio track with the external file. All standard transforms (HDR→SDR, VFR→CFR, vertical letterbox) still apply alongside the audio swap.

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
│       └── *.mp4                     ← normalized clips
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
- Blender is auto-detected at `/Applications/Blender.app` (macOS) and `/usr/bin/blender` (Linux). Pass `--blender /path/to/blender` to override.

---

## Roadmap

- **Proxy generation** — re-add `redo_proxies.py` as a post-import step; build 25% proxies for smooth VSE playback without leaving Blender
- **Subtitles** — auto-generate or import SRT / VTT and burn or soft-attach to the Blender timeline
- **After Effects export** — convert the VSE timeline to an AE-compatible project file (via ExtendScript or `aescript` bridge) for finishing in After Effects
- **External audio format validation** — iPhone Voice Memos are sometimes encoded at a sample rate or bitrate that causes audio drift against 29.97 CFR video; add a pre-sync check that validates the audio file's sample rate and codec before `audio-offset-finder` runs and warns (or auto-converts) if the format is likely to cause slipping
