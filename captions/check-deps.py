#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check-deps.py — dependency check for the captions/ add-on

The core pipeline's `0-check-deps.py` deliberately ignores this folder, so a
machine that will never run Whisper still gets a clean preflight. Run this one
before your first `captions.py` invocation.

Checks whisper-cli, the transcription model, and the Silero VAD model, and
reports which of the hallucination guards will actually be active.

Usage:
    python3 captions/check-deps.py
    python3 captions/check-deps.py --quiet      # only report problems
    python3 captions/check-deps.py --model-dir /some/other/dir

Exits non-zero only if captions.py could not run at all. A missing VAD model
warns but exits 0 — captions.py runs without it, just less safely.

Platform: macOS and Linux.
"""

from __future__ import annotations

import argparse
import os
import platform
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

# Mirrors captions.py — keep in sync if its defaults change.
DEFAULT_MODEL_SIZE = "medium"
DEFAULT_MODEL_DIR  = os.path.expanduser("~/whisper-models")
VAD_MODEL_NAME     = "ggml-silero-v5.1.2.bin"
MODEL_SIZES        = ["tiny", "base", "small", "medium", "large"]
HF_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"

WHISPER_INSTALL = (
    "brew install whisper-cpp" if IS_MACOS else
    "build whisper.cpp from source — https://github.com/ggerganov/whisper.cpp\n"
    "              (no apt package ships whisper-cli; put the built binary on PATH)"
)


def human_size(path: str) -> str:
    try:
        mb = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return ""
    return f"{mb/1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def check_whisper() -> str | None:
    found = shutil.which("whisper-cli")
    if not found:
        for h in ("/opt/homebrew/bin/whisper-cli", "/usr/local/bin/whisper-cli"):
            if os.path.isfile(h) and os.access(h, os.X_OK):
                found = h
                break
    if not found:
        bad("whisper-cli: NOT FOUND — captions.py cannot transcribe without it")
        print(f"    install:  {WHISPER_INSTALL}")
        return None
    ok(f"whisper-cli: {found}")
    return found


def check_vad_support(whisper_bin: str) -> bool:
    """
    Not every whisper.cpp build has native --vad. Builds without it reject the
    flag outright, so knowing up front beats a failed transcription — this is
    exactly why mac-minty shells its Whisper work out to another machine.
    """
    try:
        r = subprocess.run([whisper_bin, "--help"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--vad" in (r.stdout or "") + (r.stderr or "")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dependency check for the captions/ add-on."
    )
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only report problems, not things that are present")
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR,
                        help=f"Where ggml-*.bin models live (default: {DEFAULT_MODEL_DIR}).")
    args = parser.parse_args()

    model_dir = os.path.expanduser(args.model_dir)

    if not args.quiet:
        header("captions/ add-on — dependency check")
        say(f"Platform:  {platform.system()} {platform.release()}")
        say(f"Model dir: {model_dir}")

    fatal = []

    # ── Binaries ─────────────────────────────────────────────────────────────
    if not args.quiet:
        header("Binaries")

    whisper_bin = check_whisper()
    if not whisper_bin:
        fatal.append("whisper-cli")

    # ffmpeg/ffprobe belong to the core pipeline, but captions.py shells out to
    # both directly, so a captions-only user still needs them present.
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        if found:
            if not args.quiet:
                ok(f"{tool}: {found}  (shared with the core pipeline)")
        else:
            bad(f"{tool}: NOT FOUND — needed for audio extraction and muxing")
            print(f"    install:  {'brew install ffmpeg' if IS_MACOS else 'sudo apt install ffmpeg'}")
            fatal.append(tool)

    # ── Models ───────────────────────────────────────────────────────────────
    if not args.quiet:
        header("Models")

    present = [s for s in MODEL_SIZES
               if os.path.isfile(os.path.join(model_dir, f"ggml-{s}.bin"))]

    if not present:
        bad(f"No transcription model in {model_dir}")
        print(f"    install:  mkdir -p {model_dir} && \\")
        print(f"              curl -L -o {model_dir}/ggml-{DEFAULT_MODEL_SIZE}.bin \\")
        print(f"                {HF_BASE}/ggml-{DEFAULT_MODEL_SIZE}.bin")
        fatal.append("transcription model")
    else:
        for size in present:
            path = os.path.join(model_dir, f"ggml-{size}.bin")
            tag = "  ← default" if size == DEFAULT_MODEL_SIZE else ""
            if not args.quiet:
                ok(f"ggml-{size}.bin: {human_size(path)}{tag}")
        if DEFAULT_MODEL_SIZE not in present:
            warn(f"The default --model ({DEFAULT_MODEL_SIZE}) isn't here, so a bare "
                 f"`captions.py <file>` will exit with a download hint.")
            print(f"    either:   curl -L -o {model_dir}/ggml-{DEFAULT_MODEL_SIZE}.bin "
                  f"{HF_BASE}/ggml-{DEFAULT_MODEL_SIZE}.bin")
            print(f"    or:       pass --model {present[0]} explicitly")

    vad_path = os.path.join(model_dir, VAD_MODEL_NAME)
    vad_present = os.path.isfile(vad_path)
    if vad_present:
        if not args.quiet:
            ok(f"{VAD_MODEL_NAME}: {human_size(vad_path)}")
    else:
        warn(f"{VAD_MODEL_NAME}: not found — captions.py will run without VAD")
        print(f"    install:  curl -L -o {vad_path} \\")
        print(f"                {HF_BASE}/{VAD_MODEL_NAME}")

    # ── Hallucination guards ─────────────────────────────────────────────────
    # The whole point of this checker: say plainly which guards are live, since
    # the failure they prevent (invented captions, a phrase repeating for an
    # entire file) looks like a bad transcript rather than a missing dependency.
    if not args.quiet:
        header("Hallucination guards")
        ok("phase-cancellation detection: built into captions.py (always on)")
        ok("-mc 0 cross-chunk context off: built into captions.py (always on)")

        if not whisper_bin:
            warn("Silero VAD: unknown — whisper-cli missing")
        elif not check_vad_support(whisper_bin):
            warn("Silero VAD: this whisper-cli build has no --vad support")
            print("    Rebuild from a recent whisper.cpp, or transcribe on a machine "
                  "whose build has it.")
        elif not vad_present:
            warn(f"Silero VAD: supported by this build, but {VAD_MODEL_NAME} is missing")
        else:
            ok("Silero VAD: active")

    # ── Result ───────────────────────────────────────────────────────────────
    header("Result")
    if fatal:
        print(_c("0;31", f"✖ captions.py cannot run — missing: {', '.join(fatal)}"))
        print()
        sys.exit(1)

    ok("captions/ is ready.\n")
    say("Next step:")
    print("  python3 captions/captions.py /path/to/render.mp4\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        die("Interrupted by user (Ctrl-C).")
