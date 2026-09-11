#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-deps.py — dependency check for the after-effects/ add-on

The core pipeline's `0-check-deps.py` deliberately ignores this folder. Run
this one before your first `export-to-ae.py`.

The two halves of this add-on have different requirements, and they're worth
separating: the export half needs only Blender and runs anywhere, while the
import half needs After Effects itself. Exporting JSON on Linux to open on a
different machine is a perfectly good workflow, so a missing AE is reported as
"import half unavailable here", not as a failure.

Usage:
    python3 after-effects/check-deps.py
    python3 after-effects/check-deps.py --quiet

Exits non-zero only if the export half can't run.

Platform: macOS and Linux (export half); After Effects is macOS/Windows.
"""

from __future__ import annotations

import argparse
import glob
import os
import platform
import shutil
import sys

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def ok(msg):     print(_c("0;32",  f"✔ {msg}"))
def warn(msg):   print(_c("1;33",  f"⚠ {msg}"), file=sys.stderr)
def die(msg):    print(_c("0;31",  f"✖ {msg}"), file=sys.stderr); sys.exit(1)
def bad(msg):    print(_c("0;31",  f"✖ {msg}"))
def say(msg):    print(_c("0;36",  msg))
def header(msg): print(_c("1;36",  f"\n── {msg} ──\n"))


IS_MACOS = platform.system() == "Darwin"
AE_DIR = os.path.dirname(os.path.abspath(__file__))

BLENDER_HINTS = [
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
    os.path.expanduser("~/blender/blender"),
]

# Adobe installs to a path carrying the version year, so glob it rather than
# pinning a year that goes stale every autumn.
AE_APP_PATTERNS = [
    "/Applications/Adobe After Effects */Adobe After Effects *.app",
    "/Applications/Adobe After Effects*.app",
]


def find_binary(name: str, hints: list[str]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for h in hints:
        if h and os.path.isfile(h) and os.access(h, os.X_OK):
            return h
    return None


def find_after_effects() -> list[str]:
    """All installed AE versions, oldest first. Several can coexist."""
    hits: set[str] = set()
    for pattern in AE_APP_PATTERNS:
        hits.update(p for p in glob.glob(pattern) if os.path.isdir(p))
    return sorted(hits)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dependency check for the after-effects/ add-on."
    )
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only report problems, not things that are present")
    args = parser.parse_args()

    if not args.quiet:
        header("after-effects/ add-on — dependency check")
        say(f"Platform: {platform.system()} {platform.release()}")

    problems = []

    # ── Export half (Blender → JSON) ─────────────────────────────────────────
    if not args.quiet:
        header("Export half — export-to-ae.py")

    blender = find_binary("blender", BLENDER_HINTS)
    if blender:
        if not args.quiet:
            ok(f"blender: {blender}  (shared with the core pipeline)")
    else:
        bad("blender: NOT FOUND — export-to-ae.py drives it headlessly")
        print("    install:  " + ("brew install --cask blender" if IS_MACOS
                                  else "sudo apt install blender"))
        problems.append("blender")

    exporter = os.path.join(AE_DIR, "export-to-ae.py")
    if os.path.isfile(exporter):
        if not args.quiet:
            ok(f"export-to-ae.py: {exporter}")
    else:
        bad(f"export-to-ae.py: missing at {exporter}")
        problems.append("export-to-ae.py")

    # ── Import half (JSON → AE comp) ─────────────────────────────────────────
    if not args.quiet:
        header("Import half — import_blender.jsx")

    jsx = os.path.join(AE_DIR, "import_blender.jsx")
    if os.path.isfile(jsx):
        if not args.quiet:
            ok(f"import_blender.jsx: {jsx}")
    else:
        bad(f"import_blender.jsx: missing at {jsx}")
        problems.append("import_blender.jsx")

    if not IS_MACOS:
        warn("After Effects: not checked — no Adobe install path on this platform.")
        print("    The export half still works here. Move the generated JSON to a "
              "machine with AE and run the .jsx there.")
    else:
        installs = find_after_effects()
        if installs:
            for p in installs:
                if not args.quiet:
                    ok(f"After Effects: {p}")
            if len(installs) > 1 and not args.quiet:
                say(f"    ({len(installs)} versions installed — the .jsx works in any)")
            if not args.quiet:
                say("    run it via File → Scripts → Run Script File… after exporting")
        else:
            # Not appended to `problems`: exporting JSON for another machine is
            # a legitimate way to use this folder.
            warn("After Effects: not installed — the export half still works.")
            print("    install:  Adobe Creative Cloud")
            print("    or:       export here, run the .jsx on a machine that has AE")

    # ── Result ───────────────────────────────────────────────────────────────
    header("Result")
    if problems:
        print(_c("0;31", f"✖ The export half can't run — missing: {', '.join(problems)}"))
        print()
        sys.exit(1)

    ok("after-effects/ export half is ready.\n")
    say("Next step:")
    print(f"  python3 \"{exporter}\" /path/to/project_N.blend\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        die("Interrupted by user (Ctrl-C).")
