#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-deps.py — dependency check for the blender-km-macros/ add-on

The core pipeline's `0-check-deps.py` deliberately ignores this folder. Run
this one before your first cutting round.

Unlike the other add-ons, "installed" and "configured" are different things
here: Keyboard Maestro being present doesn't mean the macros are imported, and
an imported macro group restricted to Blender won't fire. Both are checked.

Usage:
    python3 blender-km-macros/check-deps.py
    python3 blender-km-macros/check-deps.py --quiet

Platform: macOS only — Keyboard Maestro doesn't exist elsewhere. On Linux this
reports that the whole add-on is unavailable and exits 0, since the core
pipeline is fully usable without it (edit and cut in Blender directly, then
render with 4-render-export.py).
"""

from __future__ import annotations

import argparse
import glob
import os
import platform
import plistlib
import shutil
import subprocess
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
KM_DIR = os.path.dirname(os.path.abspath(__file__))

KM_APP_HINTS = [
    "/Applications/Keyboard Maestro.app",
    "/Applications/Setapp/Keyboard Maestro.app",
    os.path.expanduser("~/Applications/Keyboard Maestro.app"),
]

KM_MACROS_PLIST = os.path.expanduser(
    "~/Library/Application Support/Keyboard Maestro/Keyboard Maestro Macros.plist"
)

# The macro names trigger.sh and the README refer to by name.
EXPECTED_MACROS = ["Delete clips Blender", "Delete clips Blender (TEST)"]

BLENDER_HINTS = [
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
    os.path.expanduser("~/blender/blender"),
]


def find_binary(name: str, hints: list[str]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for h in hints:
        if h and os.path.isfile(h) and os.access(h, os.X_OK):
            return h
    return None


def find_km_app() -> str | None:
    for p in KM_APP_HINTS:
        if os.path.isdir(p):
            return p
    return None


def km_engine_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-x", "Keyboard Maestro Engine"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def imported_macro_names() -> set[str] | None:
    """
    Macro names Keyboard Maestro currently has, read from its own macro store.

    Returns None if the store can't be read at all — that's "unknown", not
    "nothing imported", and the caller must not report it as a failure.
    """
    if not os.path.isfile(KM_MACROS_PLIST):
        return None
    try:
        with open(KM_MACROS_PLIST, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return None

    names: set[str] = set()

    # The store is a list of macro groups, each with a "Macros" list. Walk it
    # defensively — this is KM's private format and may change between
    # versions, so anything unexpected is skipped rather than fatal.
    def walk(node):
        if isinstance(node, dict):
            if "Name" in node and isinstance(node["Name"], str):
                names.add(node["Name"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dependency check for the blender-km-macros/ add-on."
    )
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only report problems, not things that are present")
    args = parser.parse_args()

    if not args.quiet:
        header("blender-km-macros/ add-on — dependency check")
        say(f"Platform: {platform.system()} {platform.release()}")

    # ── Platform gate ────────────────────────────────────────────────────────
    if not IS_MACOS:
        header("Result")
        warn("This add-on is macOS-only — Keyboard Maestro has no Linux version.")
        print()
        say("Nothing is broken: the cutting round is optional. Without it, edit "
            "and cut in Blender directly, save-as with a bumped number, then:")
        print("  python3 4-render-export.py /path/to/project_N.blend")
        print()
        sys.exit(0)

    problems = []

    # ── Keyboard Maestro ─────────────────────────────────────────────────────
    if not args.quiet:
        header("Keyboard Maestro")

    km_app = find_km_app()
    if km_app:
        if not args.quiet:
            ok(f"Keyboard Maestro: {km_app}")
    else:
        bad("Keyboard Maestro: NOT FOUND")
        print("    install:  https://www.keyboardmaestro.com/  (or via Setapp)")
        problems.append("Keyboard Maestro")

    if km_app:
        if km_engine_running():
            if not args.quiet:
                ok("Keyboard Maestro Engine: running")
        else:
            warn("Keyboard Maestro Engine: not running — triggers will fail until "
                 "it starts (launch Keyboard Maestro once).")

    # ── Are the macros actually imported? ────────────────────────────────────
    # The gap that actually bites: the app installs fine, the macros are just
    # files in this folder until someone double-clicks them.
    if km_app:
        names = imported_macro_names()
        if names is None:
            warn("Could not read Keyboard Maestro's macro store — skipping the "
                 "import check. Confirm by hand that the Blender group exists.")
        else:
            missing = [m for m in EXPECTED_MACROS if m not in names]
            if not missing:
                if not args.quiet:
                    ok(f"Macros imported: {', '.join(EXPECTED_MACROS)}")
                    say("    (a name match can't confirm the Blender group is set "
                        "'available in all applications' — verify that once in the editor)")
            else:
                bad(f"Macros not imported: {', '.join(missing)}")
                print("    fix:      double-click each file to import it:")
                for f in sorted(glob.glob(os.path.join(KM_DIR, "macros", "*.kmmacros"))):
                    print(f"                open \"{f}\"")
                print("    then:     confirm the Blender group is set "
                      "'available in all applications'")
                problems.append("macro import")

    # ── Supporting tools ─────────────────────────────────────────────────────
    if not args.quiet:
        header("Supporting tools")

    blender = find_binary("blender", BLENDER_HINTS)
    if blender:
        if not args.quiet:
            ok(f"blender: {blender}  (shared with the core pipeline)")
    else:
        bad("blender: NOT FOUND — the marker scripts run it headlessly")
        print("    install:  brew install --cask blender")
        problems.append("blender")

    osa = shutil.which("osascript")
    if osa:
        if not args.quiet:
            ok(f"osascript: {osa}")
    else:
        bad("osascript: NOT FOUND — trigger.sh cannot talk to Keyboard Maestro")
        problems.append("osascript")

    trigger = os.path.join(KM_DIR, "scripts", "trigger.sh")
    if not os.path.isfile(trigger):
        bad(f"trigger.sh: missing at {trigger}")
        problems.append("trigger.sh")
    elif not os.access(trigger, os.X_OK):
        warn(f"trigger.sh: present but not executable")
        print(f"    fix:      chmod +x \"{trigger}\"")
    elif not args.quiet:
        ok(f"trigger.sh: {trigger}")

    for script in ("validate-markers.py", "remove-markers.py"):
        path = os.path.join(KM_DIR, script)
        if os.path.isfile(path):
            if not args.quiet:
                ok(f"{script}: {path}")
        else:
            bad(f"{script}: missing at {path}")
            problems.append(script)

    # ── Result ───────────────────────────────────────────────────────────────
    header("Result")
    if problems:
        print(_c("0;31", f"✖ The cutting round isn't ready — {len(problems)} problem(s): "
                         f"{', '.join(problems)}"))
        print()
        sys.exit(1)

    ok("blender-km-macros/ is ready.\n")
    say("Next step — start a cutting round:")
    print(f"  python3 \"{os.path.join(KM_DIR, 'validate-markers.py')}\" "
          f"/path/to/project_1.blend\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        die("Interrupted by user (Ctrl-C).")
