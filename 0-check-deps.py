#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
0-check-deps.py — Preflight dependency check for the Video Intake & Blender VSE Pipeline

Verifies every external tool the core pipeline shells out to is installed and
reachable, before you discover it missing halfway through a long transcode.
Reports each tool's resolved path and version, separates required from
optional, and prints the install command for anything missing.

Scope is the numbered pipeline only (steps 1–4). The optional add-ons each
ship their own checker, so this one stays meaningful on a machine that will
never install Whisper or After Effects:

    captions/check-deps.py
    after-effects/check-deps.py
    blender-km-macros/check-deps.py

Exits non-zero if a required tool is missing, so it can gate a setup script.

Usage:
    python3 0-check-deps.py
    python3 0-check-deps.py --quiet     # only report problems

Platform: macOS and Linux.
"""

# This is the one script that must run on whatever Python a new machine has,
# since reporting "your Python is too old" is part of its job. The rest of the
# pipeline uses 3.10+ syntax outright.
from __future__ import annotations

import argparse
import glob
import os
import platform
import re
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
def say(msg):    print(_c("0;36",  msg))
def header(msg): print(_c("1;36",  f"\n── {msg} ──\n"))

QUIET = False

def section(msg):
    """Section heading, suppressed in --quiet so a clean run prints nothing
    until the final result."""
    if not QUIET:
        header(msg)


IS_MACOS = platform.system() == "Darwin"

# The pipeline scripts use PEP 604 (`str | None`) annotations without a
# __future__ import, so they need 3.10 to even import.
MIN_PYTHON = (3, 10)

# Same discovery hints the pipeline scripts use, so a Blender this reports as
# found is the one they will actually run.
BLENDER_HINTS = [
    "/Applications/Blender.app/Contents/MacOS/Blender",   # macOS default
    "/usr/bin/blender",                                   # Linux distro package
    "/usr/local/bin/blender",
    "/snap/bin/blender",
    # Glob patterns. Extracting the official tarball is the normal way to run a
    # current Blender on Linux, since distro packages lag by years — those land
    # at versioned paths a fixed list can never cover.
    os.path.expanduser("~/opt/blender-*/blender"),
    os.path.expanduser("~/blender-*/blender"),
    os.path.expanduser("~/blender/blender"),
    "/opt/blender-*/blender",
    "/usr/local/blender-*/blender",
]

def resolve_hint(hints):
    """
    First hint resolving to an executable, in hint order so priority is kept.

    Entries may be glob patterns — see BLENDER_HINTS. When one pattern matches
    several installs, the highest version number in the path wins, so a machine
    with both blender-3.6 and blender-4.5 unpacked gets 4.5.
    """
    for pattern in hints:
        if not pattern:
            continue
        matches = [p for p in glob.glob(pattern)
                   if os.path.isfile(p) and os.access(p, os.X_OK)]
        if matches:
            return max(matches,
                       key=lambda p: [int(n) for n in re.findall(r"\d+", p)])
    return None


BREW = "brew install" if IS_MACOS else "sudo apt install"

# Only relevant when pipx isn't already here — see pipx_hint().
GET_PIPX = "brew install pipx" if IS_MACOS else "sudo apt install pipx"


# Continuation indent for multi-line install instructions, so line 2 onward
# lands under line 1 rather than at the left margin.
INSTALL_INDENT = " " * len("    install:  ")


def pipx_hint(package: str) -> str:
    """
    Install steps for a pipx-managed CLI, assuming nothing about who's running.

    Two things this has to get right that a one-liner doesn't:

    A non-admin account can have a perfectly working pipx on PATH while
    `brew install` fails outright, because the Homebrew prefix belongs to
    another user. Telling that person to install pipx is both wrong and
    impossible, so only say it when pipx is genuinely absent.

    And `pipx install` alone is not enough to make this check pass. pipx puts
    the command in ~/.local/bin, which is not on PATH by default on macOS, so
    without `pipx ensurepath` the package installs and stays invisible to both
    this script and 2-transcode.py — the exact dead end the pip instructions
    used to lead people into. ensurepath edits the shell config, so it also
    takes a new shell to matter; name the re-run command explicitly, rather
    than letting someone re-run in the same terminal and conclude it failed.
    """
    steps = [] if shutil.which("pipx") else [GET_PIPX]
    steps += [
        f"pipx install {package}",
        "pipx ensurepath",
        "then open a new terminal and re-run: python3 0-check-deps.py",
    ]
    return ("\n" + INSTALL_INDENT).join(steps)


class Dep:
    def __init__(self, name, purpose, install, version_args=None,
                 hints=None, required=True, macos_only=False,
                 skip_reason=None):
        self.name = name
        self.purpose = purpose
        self.install = install
        # An explicit empty list means "this tool has no version flag" — don't
        # probe it. Only an omitted argument falls back to --version.
        self.version_args = ["--version"] if version_args is None else version_args
        self.hints = hints or []
        self.required = required
        self.macos_only = macos_only
        # Why a macos_only dep is skipped on Linux — the Linux answer differs
        # per tool, so each one says its own.
        self.skip_reason = skip_reason or "macOS only"


CORE_DEPS = [
    Dep("ffmpeg",   "transcode, mux, image-sequence encode", f"{BREW} ffmpeg"),
    Dep("ffprobe",  "probe media metadata",                  f"{BREW} ffmpeg"),
    Dep("exiftool", "EXIF orientation, timestamps, camera make/model",
        f"{BREW} exiftool" if IS_MACOS else "sudo apt install libimage-exiftool-perl",
        version_args=["-ver"]),
    # The pipeline invokes the legacy IM6-style names directly, so check for
    # those rather than IM7's `magick` entry point — an install that only
    # provides `magick` will fail at step 2 even though ImageMagick is present.
    Dep("convert",  "image padding (2-transcode.py stills)", f"{BREW} imagemagick",
        hints=["/opt/homebrew/bin/convert", "/usr/local/bin/convert"]),
    Dep("identify", "image dimension reads (1-ingest.py stills)", f"{BREW} imagemagick",
        hints=["/opt/homebrew/bin/identify", "/usr/local/bin/identify"]),
    Dep("blender",  "headless VSE import, marker scripts, render",
        "brew install --cask blender" if IS_MACOS else "sudo apt install blender",
        version_args=["--version"], hints=BLENDER_HINTS),
    Dep("avconvert", "iPhone HDR → SDR (macOS only; Linux uses ffmpeg zscale)",
        "built in at /usr/bin/avconvert", version_args=[],
        hints=["/usr/bin/avconvert"], macos_only=True,
        skip_reason="macOS only — Linux uses the ffmpeg zscale fallback"),
]

OPTIONAL_DEPS = [
    # pipx, not pip. 2-transcode.py shells out to the `audio-offset-finder`
    # *command* (find_audio_offset), so what matters is that command landing on
    # PATH — not which interpreter's site-packages holds the module. `pip
    # install` puts it wherever that particular pip points, and on a machine
    # with several Pythons that is routinely not the one running the pipeline:
    # the install succeeds, the command never appears, and step 2 dies anyway.
    # pipx gives the tool its own venv plus a PATH entry, so it works no matter
    # which python3 you run things with — and unlike brew, it needs no write
    # access to a prefix someone else owns.
    Dep("audio-offset-finder",
        "sync external audio / align two camera angles (steps 1–2 pairing)",
        pipx_hint("audio-offset-finder"),
        required=False),
]

def resolve(dep: Dep) -> str | None:
    found = shutil.which(dep.name)
    if not found:
        return resolve_hint(dep.hints)
    return found


def probe_version(path: str, dep: Dep) -> str:
    """Best-effort one-line version string. Never fatal — a tool that runs but
    won't report a version is still usable."""
    if not dep.version_args:
        return ""
    try:
        r = subprocess.run([path] + dep.version_args, capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    out = (r.stdout or r.stderr or "").strip().splitlines()
    if not out:
        return ""
    line = out[0].strip()
    # Tools without a version flag answer with usage text or a man-page header;
    # that's noise, not a version.
    noise = ("usage:", "invalid", "unrecognized", "unknown option", "name")
    if any(line.lower().startswith(n) for n in noise):
        return ""
    return line[:72]


def check(dep: Dep, quiet: bool) -> bool:
    """Returns True if satisfied (or not applicable on this platform)."""
    if dep.macos_only and not IS_MACOS:
        if not quiet:
            say(f"  – {dep.name}: skipped ({dep.skip_reason})")
        return True

    path = resolve(dep)
    if path:
        if not quiet:
            version = probe_version(path, dep)
            suffix = f"  [{version}]" if version else ""
            ok(f"{dep.name}: {path}{suffix}")
        return True

    if dep.required:
        print(_c("0;31", f"✖ {dep.name}: NOT FOUND") + f" — {dep.purpose}")
        print(f"    install:  {dep.install}")
        return False

    warn(f"{dep.name}: not found (optional) — {dep.purpose}")
    print(f"    install:  {dep.install}")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Preflight dependency check for the Video Intake & Blender VSE Pipeline."
    )
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only report problems, not tools that are present")
    args = parser.parse_args()

    global QUIET
    QUIET = args.quiet

    section("Video Intake Pipeline — dependency check")
    if not QUIET:
        say(f"Platform: {platform.system()} {platform.release()}")
        say(f"Python:   {sys.version.split()[0]} ({sys.executable})")

    missing_required = []

    section("Required")

    py_ok = sys.version_info >= MIN_PYTHON
    if py_ok:
        if not args.quiet:
            ok(f"python3: {'.'.join(str(v) for v in sys.version_info[:3])}"
               f"  [>= {'.'.join(str(v) for v in MIN_PYTHON)}]")
    else:
        have = ".".join(str(v) for v in sys.version_info[:3])
        need = ".".join(str(v) for v in MIN_PYTHON)
        print(_c("0;31", f"✖ python3: {have} is too old") +
              f" — the pipeline scripts need {need}+")
        print(f"    install:  {BREW} python3"
              f"   (then re-run with the newer interpreter)")
        missing_required.append(
            Dep("python3", f"pipeline scripts need {need}+", f"{BREW} python3")
        )

    for dep in CORE_DEPS:
        if not check(dep, args.quiet):
            missing_required.append(dep)

    section("Optional")
    for dep in OPTIONAL_DEPS:
        check(dep, args.quiet)

    header("Result")
    if missing_required:
        names = ", ".join(d.name for d in missing_required)
        print(_c("0;31", f"✖ Missing {len(missing_required)} required tool(s): {names}"))
        print()
        print("Install them, then re-run this check:")
        seen = set()
        for d in missing_required:
            if d.install not in seen:
                seen.add(d.install)
                print(f"  {d.install}")
        print()
        sys.exit(1)

    ok("All required tools present.\n")
    say("Next step:")
    print("  python3 1-ingest.py /path/to/footage\n")

    if not QUIET:
        say("Optional add-ons check their own dependencies, so this stays "
            "limited to the core pipeline:")
        print("  python3 captions/check-deps.py")
        print("  python3 after-effects/check-deps.py")
        print("  python3 blender-km-macros/check-deps.py")
        print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        die("Interrupted by user (Ctrl-C).")
    except EOFError:
        print()
        die("No input available (stdin closed). Pass the path as an argument instead.")
