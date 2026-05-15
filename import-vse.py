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

VIDEO_FILES = {video_files!r}

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
    def add_clip(file_path):
        current_frame = bpy.context.scene.frame_current
        bpy.ops.sequencer.movie_strip_add(filepath=file_path, frame_start=current_frame)
        active_strip = sequence_editor.active_strip
        bpy.context.scene.frame_set(active_strip.frame_final_end)

    total = len(VIDEO_FILES)
    imported = 0
    for idx, file_path in enumerate(VIDEO_FILES, 1):
        if os.path.isfile(file_path):
            print(f"[{{idx}}/{{total}}] Importing: {{os.path.basename(file_path)}}")
            add_clip(file_path)
            imported += 1
        else:
            print(f"[{{idx}}/{{total}}] SKIPPED (not found): {{file_path}}", file=sys.stderr)

    # Select all strips so they are visible when Blender opens
    for strip in sequence_editor.sequences:
        strip.select = True

print(f"Imported {{imported}} of {{total}} clips.")

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
    lines = [
        f"imported_at : {now}",
        f"blend_file  : {blend_file}",
        f"resolution  : {res_x}x{res_y}",
        f"fps         : {fps}",
        f"clip_count  : {len(entries)}",
        "",
        "clips (in import order):",
    ]
    for i, e in enumerate(entries, 1):
        ct = e.get("creation_time") or "no timestamp"
        lines.append(f"  {i:>3}.  {ct}  {e['filename']}")

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
        default = os.path.basename(project_dir)
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

    print(f"  {'#':<4}  {'DATE/TIME':<20}  FILENAME")
    print(f"  {'-'*4}  {'-'*20}  {'-'*40}")
    for i, e in enumerate(entries, 1):
        ct = (e.get("creation_time") or "no timestamp").replace("T", " ").rstrip("Z")
        print(f"  {i:<4}  {ct:<20}  {e['filename']}")
    print()

    say(f"{len(entries)} clip(s) will be imported in this order.")
    print("  Looks good? (y/n): ", end="", flush=True)
    if not input().strip().lower().startswith("y"):
        die("Aborted. Re-run ingest.py / transcode.py to change the order.")

    # ── Find Blender ─────────────────────────────────────────────────────────
    header("Step 5: Finding Blender")
    blender_bin = args.blender or find_blender()

    # ── Build and run the Blender script ─────────────────────────────────────
    header("Step 6: Importing into Blender VSE")

    video_files = [e["path"] for e in entries]

    script_src = IMPORT_TEMPLATE.format(
        blend_path = blend_file,
        res_x      = res_x,
        res_y      = res_y,
        fps_int    = fps_int,
        fps_base   = fps_base,
        video_files= video_files,
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
    say("Next steps:")
    say("  • Open the .blend file in Blender")
    say("  • View > Frame All  in the sequencer to see all clips")
    say("  • Run proxy generation when ready:")
    say("      blender_vse.sh --proxies")
    print()


if __name__ == "__main__":
    main()
