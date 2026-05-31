#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import-vse.py — Step 3 of the Video Intake & Blender VSE Pipeline

Reads manifest_transcoded.json produced by transcode.py and imports every
clip into a new Blender VSE project in chronological order (the order is
already baked into the manifest — no re-sorting needed).

Also applies the Video Editing workspace layout and writes a log file.
Proxy generation is a separate pass; run:
    blender_vse.sh --proxies

Usage:
    python3 import-vse.py /path/to/project_folder
    python3 import-vse.py /path/to/project_folder --name my_project
    python3 import-vse.py /path/to/project_folder --dry-run

    Or run with no arguments and you will be prompted to drag-and-drop
    your project folder.

The script:
    1. Locates  _ingest/transcoded/manifest_transcoded.json
    2. Asks for a project name (used as the .blend filename)
    3. Detects resolution + fps from the first clip in the manifest
    4. Shows the import order and asks for confirmation
    5. Launches Blender headlessly to create and populate the VSE
    6. Applies the Video Editing workspace layout
    7. Writes  <project_folder>/blender_import.log

Dependencies:
    blender  — 3.x or 4.x, must be on PATH or at the macOS default location

Platform: macOS primary, Linux Mint compatible.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# ANSI color helpers (auto-disabled when not a tty)
# ---------------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def say(msg):    print(_c("0;36",  msg))
def ok(msg):     print(_c("0;32",  f"✔ {msg}"))
def warn(msg):   print(_c("1;33",  f"⚠ {msg}"), file=sys.stderr)
def die(msg):    print(_c("0;31",  f"✖ {msg}"), file=sys.stderr); sys.exit(1)
def header(msg): print(_c("1;36",  f"\n── {msg} ──\n"))


# ---------------------------------------------------------------------------
# Path sanitiser (drag-and-drop / copy-paste safe)
# ---------------------------------------------------------------------------

def sanitize_path(raw: str) -> str:
    p = raw.strip()
    if len(p) >= 2 and p[0] in ('"', "'") and p[-1] == p[0]:
        p = p[1:-1]
    for esc in (" ", "&", "(", ")", "[", "]", "!", "#", "$", "@", ",", ";"):
        p = p.replace(f"\\{esc}", esc)
    return p.strip()


def prompt_for_dir() -> str:
    print()
    print("  Drag-and-drop your project folder here, or paste/type the path:")
    print("  > ", end="", flush=True)
    return sanitize_path(input())


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

BLENDER_HINTS = [
    "/Applications/Blender.app/Contents/MacOS/Blender",   # macOS default
    "/usr/bin/blender",                                    # Linux
    "/usr/local/bin/blender",
    os.path.expanduser("~/blender/blender"),
]

def find_blender() -> str:
    found = shutil.which("blender")
    if not found:
        for h in BLENDER_HINTS:
            if h and os.path.isfile(h) and os.access(h, os.X_OK):
                found = h
                break
    if not found:
        warn("blender not found on PATH or at common locations.")
        print("  Enter the full path to the Blender executable:")
        print("  > ", end="", flush=True)
        found = sanitize_path(input())
        if not (os.path.isfile(found) and os.access(found, os.X_OK)):
            die(f"Not executable: {found}")
    ok(f"Found blender: {found}")
    return found


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

def find_manifest(project_dir: str) -> str:
    candidate = os.path.join(project_dir, "_ingest", "transcoded", "manifest_transcoded.json")
    if os.path.isfile(candidate):
        return candidate
    die(
        f"No transcoded manifest found at:\n  {candidate}\n\n"
        f"  Run the pipeline steps first:\n"
        f"    python3 ingest.py   \"{project_dir}\"\n"
        f"    python3 transcode.py \"{project_dir}\""
    )


def load_manifest(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    files = data.get("files", [])
    if not files:
        die(f"Manifest is empty: {path}")
    return files


def next_project_name(project_dir: str) -> str:
    """
    Suggest the next project filename for a fresh import.

    First import → `<dirname>_1`. Subsequent imports of the same project
    → `<dirname>_<max+1>`, scanning existing `<dirname>_<N>.blend` files
    in the project dir. Keeps a consistent numbering convention with the
    `_cut` chain produced by vse-validate-markers.py.
    """
    basename = os.path.basename(os.path.normpath(project_dir))
    pattern  = re.compile(rf"^{re.escape(basename)}_(\d+)\.blend$")
    existing: list[int] = []
    if os.path.isdir(project_dir):
        for entry in os.listdir(project_dir):
            m = pattern.match(entry)
            if m:
                existing.append(int(m.group(1)))
    next_n = (max(existing) + 1) if existing else 1
    return f"{basename}_{next_n}"


# ---------------------------------------------------------------------------
# Resolution + fps from manifest
# ---------------------------------------------------------------------------

def detect_fps(entries: list[dict]) -> float:
    """Pick fps from the first entry that has one. Falls back to 29.97."""
    for e in entries:
        fps = e.get("fps")
        if fps:
            return float(fps)
    return 29.97


def fps_to_blender(fps: float) -> tuple[int, float]:
    """Return (fps_int, fps_base) for Blender's render settings."""
    if abs(fps - 23.976) < 0.01:
        return 24, 1.001
    if abs(fps - 29.97) < 0.01:
        return 30, 1.001
    if abs(fps - 59.94) < 0.01:
        return 60, 1.001
    return int(round(fps)), 1.0


# ---------------------------------------------------------------------------
# Import items (solo clips + synced camera pairs)
# ---------------------------------------------------------------------------

def camera_routing_key(entry: dict) -> str:
    """
    Stable identifier for which VSE lane a clip routes to.

    Prefers camera_model (e.g. "Canon EOS 7D", "Canon VIXIA HF R40", "iPhone
    16 Pro", "HERO12 Black") so distinct physical cameras get distinct lanes
    even when they share a brand source. Falls back to source for clips with
    no exposed model (OBS screen captures, anything else without EXIF
    Make/Model). Mirrors the same intent everywhere the allocator + display
    + log need to identify a clip's lane.
    """
    return entry.get("camera_model") or entry.get("source") or "unknown"


def allocate_track_channels(entries: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    """
    Pre-scan the chronological manifest and assign every unique camera a
    stable VSE channel so every clip from the same physical camera or
    recorder lands on one shared row in Blender.

    Returns (camera_channels, audio_channels):
      - camera_channels[key] = base channel (sound on this ch, movie on +1)
      - audio_channels[src]  = single channel for external-audio sound strips

    Camera routing keys come from `camera_routing_key` (camera_model when
    present, else source). Cameras fill channel pairs from ch1 upward in
    first-seen chronological order — whichever camera shoots first claims
    ch1/2. External-audio sources fill single channels above the highest
    camera pair. All video roles (solo, sync base, sync angle, the video
    side of an external-audio overlay) share the same per-camera channel,
    so muting "all 7D" mutes every Canon EOS 7D strip across the project
    regardless of role — and a 7D never collides with a Vixia even though
    both share `source: "canon"`.

    OBS is special-cased: if any clip is sourced from OBS, the "obs" key
    is pinned to ch1/2 (the lowest pair) so it always sits visually below
    the paired camera in a sync pair, regardless of batch order. OBS clips
    have no camera_model, so their routing key naturally falls back to
    "obs" and the pin lines up.
    """
    camera_order: list[str] = []
    audio_order: list[str] = []
    has_obs = any(
        e.get("media_type") == "video" and e.get("source") == "obs"
        for e in entries
    )
    if has_obs:
        camera_order.append("obs")
    for e in entries:
        if e.get("media_type") == "video":
            key = camera_routing_key(e)
            if key not in camera_order:
                camera_order.append(key)
        if e.get("external_audio_conformed_path"):
            src = e.get("external_audio_source") or "audio"
            if src not in audio_order:
                audio_order.append(src)

    camera_channels = {key: 1 + 2 * i for i, key in enumerate(camera_order)}
    next_free = 1 + 2 * len(camera_order)
    audio_channels = {src: next_free + i for i, src in enumerate(audio_order)}
    return camera_channels, audio_channels


def build_import_items(entries: list[dict], fps: float) -> list[dict]:
    """
    Turn the chronological manifest into an ordered list of import items.

    Each item carries the channel(s) it should land on, looked up from
    allocate_track_channels so every clip from the same camera (or every
    external-audio strip from the same recorder) shares one row. Three kinds:

      - {"kind": "solo", "path", "channel"}
      - {"kind": "sync", "base", "angle", "offset_frames",
         "base_channel", "angle_channel"}
      - {"kind": "extaudio", "video", "audio", "offset_frames",
         "video_channel", "audio_channel"}

    Incomplete sync groups (a missing partner) fall back to solo so nothing
    is silently dropped.
    """
    camera_channels, audio_channels = allocate_track_channels(entries)

    groups: dict[str, dict] = {}
    for e in entries:
        gid = e.get("sync_group")
        if not gid:
            continue
        slot = groups.setdefault(gid, {})
        slot["base" if e.get("sync_base") else "angle"] = e

    def cam_ch(entry: dict) -> int:
        return camera_channels.get(camera_routing_key(entry), 1)

    items: list[dict] = []
    consumed: set[str] = set()
    for e in entries:
        path = e["path"]
        if path in consumed:
            continue
        gid = e.get("sync_group")
        group = groups.get(gid) if gid else None
        if group and "base" in group and "angle" in group:
            base, angle = group["base"], group["angle"]
            offset_s = angle.get("sync_offset_s") or 0
            items.append({
                "kind": "sync",
                "base": base["path"],
                "angle": angle["path"],
                "offset_frames": round(offset_s * fps),
                "base_channel": cam_ch(base),
                "angle_channel": cam_ch(angle),
            })
            consumed.add(base["path"])
            consumed.add(angle["path"])
        elif e.get("external_audio_conformed_path"):
            # Overlay: keep the clip's original audio; drop the paired audio
            # file onto its source's assigned channel, aligned with the offset
            # measured in transcode.py.
            offset_s = e.get("external_audio_offset_s") or 0
            aud_src = e.get("external_audio_source") or "audio"
            items.append({
                "kind": "extaudio",
                "video": path,
                "audio": e["external_audio_conformed_path"],
                "offset_frames": round(offset_s * fps),
                "video_channel": cam_ch(e),
                "audio_channel": audio_channels[aud_src],
            })
        else:
            items.append({
                "kind": "solo",
                "path": path,
                "channel": cam_ch(e),
            })
    return items


# ---------------------------------------------------------------------------
# Blender Python payload (written to a temp file, run headlessly)
# ---------------------------------------------------------------------------

IMPORT_TEMPLATE = '''\
# AUTO-GENERATED by import-vse.py — do not edit by hand
import bpy, os, sys

BLEND_SAVE_PATH = {blend_path!r}
RESOLUTION_X    = {res_x}
RESOLUTION_Y    = {res_y}
FPS_INT         = {fps_int}
FPS_BASE        = {fps_base}

IMPORT_ITEMS = {import_items!r}

# ── Configure scene ──────────────────────────────────────────────────────────
scene = bpy.context.scene
scene.render.resolution_x = RESOLUTION_X
scene.render.resolution_y = RESOLUTION_Y
scene.render.fps          = FPS_INT
scene.render.fps_base     = FPS_BASE

# ── Sequence editor ──────────────────────────────────────────────────────────
if not scene.sequence_editor:
    scene.sequence_editor_create()
sequence_editor = scene.sequence_editor

scene.frame_set(1)

# ── Find or create a SEQUENCE_EDITOR area ───────────────────────────────────
area_type = "SEQUENCE_EDITOR"
areas = [a for a in bpy.context.window.screen.areas if a.type == area_type]
if not areas:
    largest = max(bpy.context.window.screen.areas, key=lambda a: a.width * a.height)
    largest.type = area_type
    areas = [largest]

with bpy.context.temp_override(
    window=bpy.context.window,
    area=areas[0],
    region=[r for r in areas[0].regions if r.type == "WINDOW"][0],
    screen=bpy.context.window.screen,
):
    def add_strip(file_path, frame_start, channel):
        # movie_strip_add places the SOUND strip on `channel` and the MOVIE
        # strip on channel+1, so each clip occupies the pair (channel, channel+1).
        # Base uses channel=1 -> (1,2); angle uses channel=3 -> (3,4); disjoint,
        # no collision. Returns the active strip (same frame range as its pair).
        bpy.ops.sequencer.movie_strip_add(
            filepath=file_path, frame_start=int(frame_start), channel=channel)
        return sequence_editor.active_strip

    def add_sound_strip(file_path, frame_start, channel):
        # sound_strip_add places a single SOUND strip on `channel` (no movie pair).
        bpy.ops.sequencer.sound_strip_add(
            filepath=file_path, frame_start=int(frame_start), channel=channel)
        return sequence_editor.active_strip

    total = len(IMPORT_ITEMS)
    imported = 0
    current_frame = 1
    for idx, item in enumerate(IMPORT_ITEMS, 1):
        if item["kind"] == "sync":
            base_fp, angle_fp = item["base"], item["angle"]
            if not (os.path.isfile(base_fp) and os.path.isfile(angle_fp)):
                print("[%d/%d] SKIPPED sync (missing file): %s | %s"
                      % (idx, total, base_fp, angle_fp), file=sys.stderr)
                continue
            off = item["offset_frames"]
            # offset_frames = angle start relative to base. Whichever camera
            # started first anchors the slot; the other is shifted right.
            base_start  = current_frame + (-off if off < 0 else 0)
            angle_start = current_frame + (off if off > 0 else 0)
            print("[%d/%d] Importing sync pair: %s (ch%d) + %s (ch%d, offset %+d frames)"
                  % (idx, total, os.path.basename(base_fp), item["base_channel"],
                     os.path.basename(angle_fp), item["angle_channel"], off))
            base_strip  = add_strip(base_fp,  base_start,  item["base_channel"])
            angle_strip = add_strip(angle_fp, angle_start, item["angle_channel"])
            current_frame = max(base_strip.frame_final_end, angle_strip.frame_final_end)
            imported += 2
        elif item["kind"] == "extaudio":
            video_fp, audio_fp = item["video"], item["audio"]
            if not (os.path.isfile(video_fp) and os.path.isfile(audio_fp)):
                print("[%d/%d] SKIPPED extaudio (missing file): %s | %s"
                      % (idx, total, video_fp, audio_fp), file=sys.stderr)
                continue
            off = item["offset_frames"]
            # offset_frames = audio start relative to video. Whichever started
            # first anchors the slot; the other is shifted right.
            video_start = current_frame + (-off if off < 0 else 0)
            audio_start = current_frame + (off if off > 0 else 0)
            print("[%d/%d] Importing video+ext-audio: %s (ch%d) + %s (ch%d, offset %+d frames)"
                  % (idx, total, os.path.basename(video_fp), item["video_channel"],
                     os.path.basename(audio_fp), item["audio_channel"], off))
            video_strip = add_strip(video_fp, video_start, item["video_channel"])
            audio_strip = add_sound_strip(audio_fp, audio_start, item["audio_channel"])
            current_frame = max(video_strip.frame_final_end, audio_strip.frame_final_end)
            imported += 2
        else:
            file_path = item["path"]
            if not os.path.isfile(file_path):
                print("[%d/%d] SKIPPED (not found): %s" % (idx, total, file_path), file=sys.stderr)
                continue
            ch = item["channel"]
            print("[%d/%d] Importing: %s (ch%d)" % (idx, total, os.path.basename(file_path), ch))
            strip = add_strip(file_path, current_frame, ch)
            current_frame = strip.frame_final_end
            imported += 1

    # Select all strips so they are visible when Blender opens
    for strip in sequence_editor.sequences:
        strip.select = True

    # Zoom the VSE to fit every imported strip. The area override is required
    # for view_all; this runs inside the same temp_override block as the strip
    # add ops. Whether the framing survives save+reopen is up to Blender's
    # per-area view persistence — if it doesn't, this is still a no-op cost.
    try:
        bpy.ops.sequencer.view_all()
    except RuntimeError as e:
        print(f"Warning: view_all failed: {{e}}")

# Set scene end to the last frame of the last clip so the playback range
# matches the imported content instead of Blender's default 250.
# current_frame is the insertion point for the *next* clip (exclusive), so
# the last playable frame is current_frame - 1.
if imported > 0:
    scene.frame_end = max(1, current_frame - 1)
    print("Scene end frame set to %d." % scene.frame_end)

print("Imported %d clip(s) across %d timeline slot(s)." % (imported, total))

# ── Apply Video Editing workspace ────────────────────────────────────────────
template_path = None
for p in bpy.utils.app_template_paths():
    candidate = os.path.join(p, "Video_Editing", "startup.blend")
    if os.path.isfile(candidate):
        template_path = candidate
        break

if template_path:
    with bpy.data.libraries.load(template_path) as (data_from, data_to):
        data_to.workspaces = [ws for ws in data_from.workspaces if "Video Editing" in ws]
    for ws in bpy.data.workspaces:
        if "Video Editing" in ws.name:
            bpy.context.window.workspace = ws
            print(f"Switched to workspace: {{ws.name}}")
            break
else:
    print("Warning: Video_Editing template not found — layout unchanged.")

# ── Save ─────────────────────────────────────────────────────────────────────
bpy.ops.wm.save_as_mainfile(filepath=BLEND_SAVE_PATH)
print(f"Saved: {{BLEND_SAVE_PATH}}")
print("IMPORT_COMPLETE")
'''


# ---------------------------------------------------------------------------
# Run Blender headlessly
# ---------------------------------------------------------------------------

def run_blender(blender_bin: str, script_path: str, dry_run: bool = False) -> bool:
    """
    Launch Blender in background mode with the given Python script.
    Streams output live so the user can see progress.
    Returns True if Blender exited cleanly and printed IMPORT_COMPLETE.
    """
    cmd = [blender_bin, "--background", "--python", script_path]

    if dry_run:
        say(f"  [dry-run] would run: {' '.join(cmd)}")
        return True

    say("Launching Blender (this may take a minute)…")
    print()

    success_marker = False
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"  blender | {line}")
            if "IMPORT_COMPLETE" in line:
                success_marker = True
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        die("Interrupted.")

    if proc.returncode != 0:
        warn(f"Blender exited with code {proc.returncode}")
        return False

    return success_marker


# ---------------------------------------------------------------------------
# Log writer
# ---------------------------------------------------------------------------

def write_log(project_dir: str, blend_file: str, entries: list[dict],
              res_x: int, res_y: int, fps: float) -> str:
    log_path = os.path.join(project_dir, "blender_import.log")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    camera_channels, audio_channels = allocate_track_channels(entries)
    lines = [
        f"imported_at : {now}",
        f"blend_file  : {blend_file}",
        f"resolution  : {res_x}x{res_y}",
        f"fps         : {fps}",
        f"clip_count  : {len(entries)}",
        "",
    ]
    if camera_channels or audio_channels:
        lines.append("channel routing:")
        for key, ch in camera_channels.items():
            lines.append(f"  ch{ch}/{ch+1}  {key}")
        for src, ch in audio_channels.items():
            lines.append(f"  ch{ch}    external audio: {src}")
        lines.append("")
    lines.append("clips (in import order):")
    for i, e in enumerate(entries, 1):
        ct = e.get("creation_time") or "no timestamp"
        gid = e.get("sync_group")
        key = camera_routing_key(e)
        ch = camera_channels.get(key, 1)
        tag = ""
        if gid:
            role = "base" if e.get("sync_base") else "angle"
            off = e.get("sync_offset_s")
            off_str = f", offset {off:+.3f}s" if (off is not None and role == "angle") else ""
            tag = f"  [{gid} {role} {key} → ch{ch}/{ch+1}{off_str}]"
        elif e.get("external_audio_conformed_path"):
            off = e.get("external_audio_offset_s")
            off_str = f", offset {off:+.3f}s" if off is not None else ""
            aud = os.path.basename(e["external_audio_conformed_path"])
            aud_src = e.get("external_audio_source") or "audio"
            aud_ch = audio_channels.get(aud_src)
            aud_ch_str = f"ch{aud_ch}" if aud_ch else "?"
            tag = f"  [{key} → ch{ch}/{ch+1}; ext-audio ({aud_src}) → {aud_ch_str}: {aud}{off_str}]"
        else:
            tag = f"  [{key} → ch{ch}/{ch+1}]"
        lines.append(f"  {i:>3}.  {ct}  {e['filename']}{tag}")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return log_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Import step: load transcoded clips into a Blender VSE project."
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=None,
        help="Project folder containing _ingest/transcoded/manifest_transcoded.json. "
             "Omit to be prompted.",
    )
    parser.add_argument(
        "--name", "-n",
        default=None,
        help="Project (and .blend file) name. Prompted if omitted.",
    )
    parser.add_argument(
        "--blender",
        default=None,
        help="Path to the Blender executable. Auto-detected if omitted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without running Blender.",
    )
    args = parser.parse_args()

    # ── Resolve project directory ────────────────────────────────────────────
    if args.project_dir:
        raw_dir = sanitize_path(args.project_dir)
    else:
        raw_dir = prompt_for_dir()

    project_dir = os.path.abspath(os.path.expanduser(raw_dir))
    if not os.path.isdir(project_dir):
        die(f"Not a directory: {project_dir}")

    # ── Load manifest ────────────────────────────────────────────────────────
    header("Step 1: Loading Manifest")
    manifest_path = find_manifest(project_dir)
    say(f"Manifest: {manifest_path}")
    entries = load_manifest(manifest_path)
    ok(f"Found {len(entries)} clip(s) in manifest.")

    # ── Project name / blend file path ───────────────────────────────────────
    header("Step 2: Project Name")

    project_name = args.name
    if not project_name:
        default = next_project_name(project_dir)
        print(f"  Project name [{default}]: ", end="", flush=True)
        entered = input().strip()
        project_name = entered if entered else default

    # Sanitise: replace spaces with underscores, strip unsafe chars
    project_name = re.sub(r"[^\w\-.]", "_", project_name)

    blend_file = os.path.join(project_dir, project_name + ".blend")

    if os.path.isfile(blend_file):
        warn(f"Blend file already exists: {blend_file}")
        print("  Overwrite? (y/n): ", end="", flush=True)
        if not input().strip().lower().startswith("y"):
            die("Aborted.")

    say(f"Will save: {blend_file}")

    # ── Scene settings ───────────────────────────────────────────────────────
    header("Step 3: Scene Settings")

    fps = detect_fps(entries)
    fps_int, fps_base = fps_to_blender(fps)

    say(f"FPS: {fps}  →  Blender fps={fps_int}, fps_base={fps_base}")
    print()
    print("  Project resolution:")
    print("    1. 1080p  (1920×1080)  ← recommended")
    print("    2. 4K     (3840×2160)")
    print()
    print("  Choose [1/2]: ", end="", flush=True)
    res_choice = input().strip()
    if res_choice == "2":
        res_x, res_y = 3840, 2160
        ok("Project resolution: 4K (3840×2160)")
    else:
        res_x, res_y = 1920, 1080
        ok("Project resolution: 1080p (1920×1080)")

    # ── Confirm clip order ───────────────────────────────────────────────────
    header("Step 4: Import Order")

    camera_channels, audio_channels = allocate_track_channels(entries)

    if camera_channels or audio_channels:
        print("  Channel routing (every strip from a given camera/recorder shares one row):")
        for key, ch in camera_channels.items():
            print(f"    ch{ch}/{ch+1}  {key}")
        for src, ch in audio_channels.items():
            print(f"    ch{ch}    external audio: {src}")
        print()

    def cam_ch(e):
        return camera_channels.get(camera_routing_key(e), 1)

    print(f"  {'#':<4}  {'DATE/TIME':<20}  FILENAME")
    print(f"  {'-'*4}  {'-'*20}  {'-'*40}")
    for i, e in enumerate(entries, 1):
        ct = (e.get("creation_time") or "no timestamp").replace("T", " ").rstrip("Z")
        tag = ""
        gid = e.get("sync_group")
        key = camera_routing_key(e)
        ch = cam_ch(e)
        if gid:
            role = "base" if e.get("sync_base") else "angle"
            tag = f"   ⇄ {gid} ({role} {key} → ch{ch}/{ch+1})"
        elif e.get("external_audio_conformed_path"):
            off = e.get("external_audio_offset_s")
            off_str = f", offset {off:+.3f}s" if off is not None else ""
            aud_src = e.get("external_audio_source") or "audio"
            aud_ch = audio_channels.get(aud_src)
            tag = (f"   ⇄ {key} → ch{ch}/{ch+1}  +  ext-audio ({aud_src}) → ch{aud_ch}{off_str}"
                   if aud_ch else f"   ⇄ {key} → ch{ch}/{ch+1}  +  ext-audio{off_str}")
        else:
            tag = f"   → ch{ch}/{ch+1}  ({key})"
        print(f"  {i:<4}  {ct:<20}  {e['filename']}{tag}")
    print()

    n_sync = sum(1 for e in entries if e.get("sync_group") and e.get("sync_base"))
    n_extaud = sum(1 for e in entries if e.get("external_audio_conformed_path"))
    say(f"{len(entries)} clip(s) will be imported in this order.")
    say(f"Each camera gets its own VSE channel pair — mute/solo per source is one click in Blender.")
    if n_sync:
        say(f"{n_sync} synced camera pair(s) will overlap on their respective camera lanes.")
    if n_extaud:
        say(f"{n_extaud} clip(s) with external audio will overlay on shared per-source audio channels.")
    print("  Looks good? (y/n): ", end="", flush=True)
    if not input().strip().lower().startswith("y"):
        die("Aborted. Re-run ingest.py / transcode.py to change the order.")

    # ── Find Blender ─────────────────────────────────────────────────────────
    header("Step 5: Finding Blender")
    blender_bin = args.blender or find_blender()

    # ── Build and run the Blender script ─────────────────────────────────────
    header("Step 6: Importing into Blender VSE")

    import_items = build_import_items(entries, fps)

    script_src = IMPORT_TEMPLATE.format(
        blend_path  = blend_file,
        res_x       = res_x,
        res_y       = res_y,
        fps_int     = fps_int,
        fps_base    = fps_base,
        import_items= import_items,
    )

    # Write to a temp file that Blender can read
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="import_vse_")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(script_src)

        success = run_blender(blender_bin, tmp_path, args.dry_run)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not success:
        die("Blender import did not complete successfully. Check output above.")

    # ── Write log ────────────────────────────────────────────────────────────
    if not args.dry_run:
        log_path = write_log(project_dir, blend_file, entries, res_x, res_y, fps)
        ok(f"Log written: {log_path}")

    # ── Done ─────────────────────────────────────────────────────────────────
    header("Done")
    ok(f"Project saved: {blend_file}")
    print()
    header("Next step")
    print("  1. Open the .blend in Blender and place F / u markers on the segments to keep.")
    print(f"     open \"{blend_file}\"")
    print("  2. When markers are in, validate them:")
    print(f"       python3 vse-validate-markers.py \"{blend_file}\"")
    print()


if __name__ == "__main__":
    main()
