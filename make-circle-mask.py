#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make-circle-mask.py — Generate a circle mask PNG next to a Blender project

Asks where the .blend file is, then renders a white circle on a transparent
background via ffmpeg and saves it into that same folder as circle-mask.png.
Drop the PNG onto an Image strip / texture in Blender's VSE or compositor to
mask a circle.

Usage:
    python3 make-circle-mask.py                       # prompted to drag-and-drop
    python3 make-circle-mask.py /path/to/project.blend
    python3 make-circle-mask.py project.blend --size 1024

Dependencies:
    ffmpeg — must be on PATH

Platform: macOS primary, Linux Mint compatible.
"""

import argparse
import os
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def ok(msg):   print(_c("0;32", f"✔ {msg}"))
def warn(msg): print(_c("1;33", f"⚠ {msg}"), file=sys.stderr)
def die(msg):  print(_c("0;31", f"✖ {msg}"), file=sys.stderr); sys.exit(1)
def say(msg):  print(_c("0;36", msg))


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


def prompt_for_blend_path() -> str:
    say("Drag and drop the .blend file here, then press Enter:")
    raw = sanitize_path(input())
    if not raw:
        die("No path given.")
    return raw


# ---------------------------------------------------------------------------
# Circle mask render
# ---------------------------------------------------------------------------

def render_circle_mask(out_path: str, size: int) -> None:
    # Supersample at 4x and downscale with area averaging for an
    # anti-aliased edge instead of ffmpeg's default hard-edged geq circle.
    ss = size * 4
    vf = (
        f"format=rgba,"
        f"geq=r='255':g='255':b='255':"
        f"a='if(lte(pow(X-{ss/2},2)+pow(Y-{ss/2},2),pow({ss/2},2)),255,0)',"
        f"scale={size}:{size}:flags=area"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={ss}x{ss}",
        "-vf", vf,
        "-frames:v", "1",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"ffmpeg failed:\n{result.stderr}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate a circle mask PNG next to a Blender project.")
    parser.add_argument("blend_path", nargs="?", help="Path to the .blend file (prompted if omitted)")
    parser.add_argument("--size", type=int, default=2048, help="Circle canvas size in pixels, square (default: 2048)")
    args = parser.parse_args()

    if not shutil.which("ffmpeg"):
        die("ffmpeg not found on PATH.")

    raw = sanitize_path(args.blend_path) if args.blend_path else prompt_for_blend_path()
    blend_path = os.path.abspath(os.path.expanduser(raw))

    if not os.path.isfile(blend_path):
        die(f"Not a file: {blend_path}")
    if not blend_path.lower().endswith(".blend"):
        warn(f"Doesn't look like a .blend file: {blend_path}")

    out_dir = os.path.dirname(blend_path)
    out_path = os.path.join(out_dir, "circle-mask.png")

    render_circle_mask(out_path, args.size)
    ok(f"Saved {out_path} ({args.size}x{args.size})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        die("Cancelled.")
