# after-effects

Optional add-on for the [Video Intake & Blender VSE Pipeline](../README.md).
Exports the final Blender VSE timeline to a JSON description, then
reconstructs it as an After Effects composition — for users who do their
cuts in Blender (because Blender's VSE + the KM cutting macro is the
fastest workflow) but want to finish in After Effects (titles, motion
graphics, finer compositing).

**Not required for the main pipeline.** If you render directly from
Blender, you never need this. The folder follows the same in-repo add-on
pattern as [`blender-km-macros/`](../blender-km-macros/) and
[`captions/`](../captions/) — it has its own README, doesn't pollute the
global dependencies table, and stays out of the way until you opt in.

Adapted from the original
[blender-vse-to-ae](https://github.com/evanthemann/blender-vse-to-ae)
repo and standardized to match the rest of this pipeline (ANSI helpers,
drag-and-drop prompt, `find_blender` auto-discovery, end-of-script
next-step hint).

---

## What it does

1. `export-to-ae.py` runs Blender headlessly on the .blend you point it
   at (typically your latest `<project>_<N>.blend` after rounds of cuts)
   and produces a sibling `<stem>_vse_export.json` containing every
   MOVIE strip's source path, timeline placement, in/out points,
   channel, scale, and translation. The Blender-side payload is embedded
   inside `export-to-ae.py` as a template string and written to a temp
   file at invocation time — the same pattern `import-vse.py`,
   `vse-validate-markers.py`, and `vse-remove-markers.py` already use.
2. `import_blender.jsx`, run inside After Effects, prompts for that
   JSON, creates a comp called **`Blender_VSE`** at the right dimensions
   and frame rate, and adds every clip as a layer at its timeline
   position — sorted bottom-to-top by Blender's VSE channel order so the
   per-camera lane routing from `import-vse.py` carries through to AE's
   layer stack.

The pipeline's existing chain works for you: each round of cuts
(`_1.blend` → `_2.blend` → …) leaves a clean editable project; when
you're done cutting, point this exporter at the latest one and finish
in AE.

---

## Install

Nothing to `pip install` — the wrapper is stdlib-only Python and shells
out to Blender like every other script in the repo. The only extra
requirement is **After Effects itself** for the import step.

```bash
brew install --cask blender   # already required by the main pipeline
```

After Effects: any reasonably modern version supports the ExtendScript
in `import_blender.jsx`. No version-specific gotchas observed.

---

## Usage

```bash
python3 after-effects/export-to-ae.py /path/to/project_5.blend
python3 after-effects/export-to-ae.py            # prompted — supports drag-and-drop
```

The wrapper:
- Auto-detects Blender (same hint list as the rest of the pipeline).
- Runs the bundled `vse_export.py` headlessly inside Blender.
- Captures the JSON path Blender writes and prints a concrete next-step
  hint pointing at the AE side of the flow.

End-of-script output looks like:

```
── Next step ──

  1. Open After Effects.
  2. File > Scripts > Run Script File…
       /…/after-effects/import_blender.jsx
  3. When the file picker appears, select:
       /…/project_5_vse_export.json
```

Copy-paste the JSX path, run it, pick the JSON, and AE builds the comp.

### Make `import_blender.jsx` permanent in AE

Drop the `.jsx` into AE's **`Scripts/`** folder (on macOS:
`/Applications/Adobe After Effects <year>/Scripts/`) and it'll appear
under **File > Scripts** directly — no "Run Script File…" step needed.

---

## Files in this folder

```
after-effects/
├── README.md             ← this file
├── export-to-ae.py       ← stdlib Python, runs Blender headlessly with an embedded
│                           Blender Python payload
└── import_blender.jsx    ← After Effects ExtendScript
```

---

## What gets exported per clip

| Field           | Source                                | Notes                                          |
|-----------------|---------------------------------------|------------------------------------------------|
| `name`          | strip name                            |                                                |
| `filepath`      | absolute path to the source media     | Resolved through Blender (handles relative paths). |
| `timeline_start`| seconds from comp start               | Computed from `frame_start + frame_offset_start`. |
| `in_point`      | seconds into the source               |                                                |
| `out_point`     | seconds into the source               | `in_point + final_duration`.                   |
| `channel`       | Blender VSE channel number            | Drives layer stack order in AE.                |
| `scale_x` / `scale_y` | Blender VSE transform scale     | Multiplied by 100 in AE (AE uses percent).     |
| `translate_x` / `translate_y` | VSE transform offset    | Y is flipped (Blender Y is up, AE Y is down).  |

Comp-wide fields: `fps`, `comp_width`, `comp_height`, `comp_duration`.

---

## Known limitations / gotchas

- Only **MOVIE** strips are exported. SOUND strips (your `_extaudio.wav`
  overlays, sync-pair audio) and IMAGE strips are not picked up by
  `vse_export.py` today — would be a straightforward extension.
- The Blender→AE Y-axis flip is computed from the transform offset
  only; if you've done unusual pivot/anchor edits, double-check
  positions in AE.
- Missing source media is reported in AE's final alert instead of
  failing the import — you'll see which paths couldn't be resolved.
- `import_blender.jsx` always names the comp `Blender_VSE`. If you
  re-import on top of an existing project that already has a comp by
  that name, AE will create a numbered duplicate.
