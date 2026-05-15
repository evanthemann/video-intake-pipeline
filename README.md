# Video Intake & Blender VSE Pipeline

Automates the journey from raw iPhone and GoPro footage to an edit-ready Blender VSE project. Run the four scripts in order, then do your manual editing in Blender, then use the two marker scripts to prep for and clean up after the Keyboard Maestro cutting macro.

---

## Pipeline Overview

```
Raw footage (iPhone · GoPro · stills)
        │
        ▼
1. ingest.py          — scan folder, extract metadata, write manifest
        │
        ▼
2. transcode.py       — normalize all media to SDR · CFR 29.97 · 16:9 MP4
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
   [ run Keyboard Maestro macro ]
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

Linux Mint: replace `avconvert` with ffmpeg zscale tone-map (handled automatically). `exiftool` via `sudo apt install libimage-exiftool-perl`.

---

## Scripts

### 1 — `ingest.py`

Scans a footage folder recursively, extracts metadata via ffprobe / exiftool, and writes three output files to `<input_dir>/_ingest/`:

- `manifest.json` — machine-readable, consumed by transcode.py
- `clips_ordered.txt` — human-readable chronological list
- `ingest_report.md` — counts, flags (HDR / VFR / vertical / missing timestamps), timeline gaps

**What it detects per file:** source (GoPro / iPhone / unknown), media type, orientation, dimensions, duration, FPS, HDR, VFR, creation timestamp.

```bash
python3 ingest.py /path/to/footage
python3 ingest.py /path/to/footage --output /path/to/output_dir
python3 ingest.py          # prompted — supports drag-and-drop
```

**Flags / options:**

| Flag | Description |
|---|---|
| `--output`, `-o` | Custom output directory (default: `<input_dir>/_ingest`) |

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

# --- Run Keyboard Maestro macro (N loops as printed above) ---

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
