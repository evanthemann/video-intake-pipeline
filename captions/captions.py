#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
captions.py — auto-caption an MP4 with whisper.cpp + ffmpeg

Takes an MP4 (typically a Blender render exported from the main pipeline)
and produces:
  <stem>.srt          — editable subtitle text, always written
  <stem>_subbed.mp4   — soft-embedded (mov_text) subtitles, toggleable in
                        QuickTime / VLC / iPad / Apple TV
  <stem>_burned.mp4   — only with --burn: hard-rendered captions in pixels

Caption styles:
  default            phrase chunks (~3–6 second lines, sentence-aware)
  --word-by-word     single-word flash captions

Usage:
    python3 captions.py /path/to/render.mp4
    python3 captions.py /path/to/render.mp4 --burn
    python3 captions.py /path/to/render.mp4 --word-by-word --burn
    python3 captions.py                    # prompted to drag-and-drop

Dependencies:
    ffmpeg + ffprobe — already required by the main pipeline
    whisper-cli      — `brew install whisper-cpp` (Homebrew formula)
    ggml-*.bin model — `curl` one to ~/whisper-models/ once; the script
                       prints the exact command if the requested one is
                       missing.

This script imports only Python stdlib. Whisper runs as a subprocess
(whisper.cpp via whisper-cli) — no PyTorch, no venv, no pip install.

Platform: macOS primary, Linux Mint compatible.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty()

def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def ok(msg):     print(_c("0;32",  f"✔ {msg}"))
def warn(msg):   print(_c("1;33",  f"⚠ {msg}"), file=sys.stderr)
def skip(msg):   print(_c("0;35",  f"↩ {msg}"))
def die(msg):    print(_c("0;31",  f"✖ {msg}"), file=sys.stderr); sys.exit(1)
def say(msg):    print(_c("0;36",  msg))
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


def prompt_for_mp4() -> str:
    print()
    print("  Drag-and-drop the MP4 to caption, or paste/type the path:")
    print("  > ", end="", flush=True)
    return sanitize_path(input())


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def find_tool(name: str, hints: list[str] | None = None, install_hint: str = "") -> str:
    found = shutil.which(name)
    if not found and hints:
        for h in hints:
            if h and os.path.isfile(h) and os.access(h, os.X_OK):
                found = h
                break
    if not found:
        msg = f"'{name}' not found on PATH or at common locations."
        if install_hint:
            msg += f"\n  Install:  {install_hint}"
        die(msg)
    return found


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv"}

# Phrase grouping heuristics
PHRASE_MAX_WORDS     = 7
PHRASE_MAX_SECONDS   = 5.0
HARD_BREAK_PUNCT     = ".?!"
SOFT_BREAK_PUNCT     = ","
SOFT_BREAK_MIN_WORDS = 3

# Defaults
DEFAULT_MODEL_SIZE = "small"
DEFAULT_MODEL_DIR  = os.path.expanduser("~/whisper-models")
DEFAULT_LANGUAGE   = "auto"
DEFAULT_FONT       = "Arial-Bold"
DEFAULT_PHRASE_SIZE = 60
DEFAULT_WORD_SIZE   = 90

MODEL_SIZES = {"tiny", "base", "small", "medium", "large"}
MODEL_DOWNLOAD_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"


# ---------------------------------------------------------------------------
# Model file resolution
# ---------------------------------------------------------------------------

def resolve_model_path(model_arg: str, model_dir: str) -> str:
    """
    Resolve --model to a concrete .bin path.

    - If the arg is a path that exists (or looks like a path), use it directly.
    - Otherwise treat it as a size keyword and look for
      <model_dir>/ggml-<size>.bin.

    If the resolved path doesn't exist, die with a curl command the user
    can copy-paste.
    """
    candidate = os.path.expanduser(model_arg)
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)

    if model_arg in MODEL_SIZES or "/" not in model_arg:
        size = model_arg
        path = os.path.join(model_dir, f"ggml-{size}.bin")
        if os.path.isfile(path):
            return os.path.abspath(path)
        die(
            f"Model file not found: {path}\n"
            f"  Download it once with:\n"
            f"    mkdir -p {model_dir}\n"
            f"    curl -L -o {path} \\\n"
            f"      {MODEL_DOWNLOAD_BASE}/ggml-{size}.bin\n"
            f"  Sizes: tiny ~75 MB, base ~140 MB, small ~466 MB, "
            f"medium ~1.5 GB, large ~3 GB."
        )

    # Looks like a path but doesn't exist
    die(f"Model file not found: {candidate}")


# ---------------------------------------------------------------------------
# Audio extraction (mono 16 kHz wav — whisper.cpp's native input)
# ---------------------------------------------------------------------------

def extract_audio_to_wav(mp4_path: str, out_wav: str, ffmpeg_bin: str) -> None:
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error",
        "-i", mp4_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-f", "wav", out_wav,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        die(f"ffmpeg audio extract failed:\n{r.stderr[-800:]}")


# ---------------------------------------------------------------------------
# whisper-cli (whisper.cpp) transcription
# ---------------------------------------------------------------------------

def transcribe(
    wav_path: str,
    model_path: str,
    language: str,
    whisper_bin: str,
) -> list[tuple[float, float, str]]:
    """
    Run whisper-cli with full-JSON output (per-token timestamps), parse,
    and return [(start_sec, end_sec, text), ...]. Skips special tokens
    ([_BEG_], [_TT_*], …) and pure-punctuation tokens.
    """
    tmp_fd, json_stem_path = tempfile.mkstemp(suffix="", prefix="caps_wcli_")
    os.close(tmp_fd)
    os.unlink(json_stem_path)  # whisper-cli appends .json itself
    json_path = json_stem_path + ".json"
    try:
        cmd = [
            whisper_bin,
            "-m", model_path,
            "-l", language,
            "-ojf",                 # output full JSON (per-token timestamps)
            "-of", json_stem_path,  # output filename stem (no extension)
            "-np",                  # no progress prints to stderr
            wav_path,
        ]
        # whisper-cli prints model info + a transcription preview to stderr;
        # capture it for diagnostics on failure but otherwise stay quiet.
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            die(f"whisper-cli failed:\n{r.stderr[-1200:]}")

        if not os.path.isfile(json_path):
            die(f"whisper-cli did not produce expected JSON at {json_path}")

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    finally:
        for p in (json_path, json_stem_path):
            try: os.unlink(p)
            except OSError: pass

    # whisper.cpp emits BPE tokens. A leading space means "new word"; no
    # leading space means "continuation of the previous word" (e.g.
    # " spreadshe" + "ets" = " spreadsheets", or " country" + "." = " country.",
    # or " you" + " '" + "ve" = " you 've"… wait, " '" has a leading space but
    # is pure punctuation — we still glue those back to the previous word
    # rather than emitting them as standalone "words", so phrase grouping's
    # hard-break-on-`.?!` heuristic actually fires.
    words: list[tuple[float, float, str]] = []
    for seg in data.get("transcription", []):
        for tok in seg.get("tokens", []):
            raw = tok.get("text") or ""
            if not raw.strip():
                continue
            if raw.lstrip().startswith("["):    # specials: [_BEG_], [_TT_*], etc.
                continue
            offs = tok.get("offsets") or {}
            try:
                start_ms = int(offs.get("from", 0))
                end_ms   = int(offs.get("to", start_ms))
            except (TypeError, ValueError):
                continue
            if end_ms < start_ms:
                end_ms = start_ms
            start_sec = start_ms / 1000.0
            end_sec   = end_ms   / 1000.0

            stripped = raw.strip()
            is_continuation  = not raw[:1].isspace()          # no leading space
            is_punct_only    = not any(ch.isalnum() for ch in stripped)

            # Continuation or stray punctuation glued to the previous word.
            if words and (is_continuation or is_punct_only):
                prev_start, prev_end, prev_text = words[-1]
                words[-1] = (prev_start, max(prev_end, end_sec),
                             prev_text + stripped)
                continue

            # Pure-punctuation token with no prior word to attach to — skip.
            if is_punct_only:
                continue

            words.append((start_sec, end_sec, stripped))
    return words


# ---------------------------------------------------------------------------
# SRT formatting
# ---------------------------------------------------------------------------

def format_srt_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms  = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(entries: list[tuple[float, float, str]], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(entries, 1):
            f.write(f"{i}\n")
            f.write(f"{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n")
            f.write(f"{text}\n\n")


# ---------------------------------------------------------------------------
# Phrase grouping
# ---------------------------------------------------------------------------

def group_into_phrases(
    words: list[tuple[float, float, str]],
) -> list[tuple[float, float, str]]:
    """
    Walk the per-word list and emit phrase-length SRT entries.

    Rules (tunable at top of file):
      - Hard-break after . ? !
      - Soft-break after , if chunk already has >3 words
      - Force-break if chunk would exceed 7 words or 5 seconds
    """
    entries: list[tuple[float, float, str]] = []
    current: list[tuple[float, float, str]] = []

    def flush() -> None:
        if not current:
            return
        start = current[0][0]
        end   = current[-1][1]
        text  = " ".join(w[2] for w in current).strip()
        if text:
            entries.append((start, end, text))
        current.clear()

    for w in words:
        current.append(w)
        chunk_n = len(current)
        chunk_dur = current[-1][1] - current[0][0]
        last_text = w[2]

        if last_text and last_text[-1] in HARD_BREAK_PUNCT:
            flush()
            continue
        if (last_text and last_text[-1] in SOFT_BREAK_PUNCT
                and chunk_n > SOFT_BREAK_MIN_WORDS):
            flush()
            continue
        if chunk_n >= PHRASE_MAX_WORDS or chunk_dur >= PHRASE_MAX_SECONDS:
            flush()

    flush()
    return entries


# ---------------------------------------------------------------------------
# ASS generation (for hard-burn via libass)
# ---------------------------------------------------------------------------

def ass_time(seconds: float) -> str:
    """ASS uses H:MM:SS.cs (centi-seconds, single-digit hours)."""
    total_cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(total_cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs  = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    return (text.replace("\\", "\\\\")
                .replace("{", "\\{")
                .replace("}", "\\}")
                .replace("\n", "\\N"))


def write_ass(
    entries: list[tuple[float, float, str]],
    style_mode: str,
    font: str,
    size: int,
    video_w: int,
    video_h: int,
    out_path: str,
) -> None:
    """
    Generate an ASS subtitle file with a single Default style.

    Word-by-word style replicates the moviepy reference:
      Arial-Bold 90pt centered with a 5px white outline.

    Phrase style is conventional bottom-center captions:
      Arial-Bold 60pt with a subtle 2px black outline for readability.
    """
    if style_mode == "word":
        alignment      = 5             # middle-center
        outline_colour = "&H00FFFFFF"  # white (ASS BGR + alpha)
        outline_px     = 5
        margin_v       = 0
    else:
        alignment      = 2             # bottom-center
        outline_colour = "&H00000000"  # black
        outline_px     = 2
        margin_v       = max(40, video_h // 16)

    primary_colour = "&H00FFFFFF"
    back_colour    = "&H00000000"

    style_line = (
        f"Style: Default,{font},{size},"
        f"{primary_colour},{primary_colour},{outline_colour},{back_colour},"
        f"-1,0,0,0,100,100,0,0,1,{outline_px},0,{alignment},20,20,{margin_v},1"
    )

    lines: list[str] = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {video_w}",
        f"PlayResY: {video_h}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
         "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
         "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding"),
        style_line,
        "",
        "[Events]",
        ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
         "MarginV, Effect, Text"),
    ]
    for start, end, text in entries:
        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,"
            f"0,0,0,,{_ass_escape(text)}"
        )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# ffmpeg invocations: soft mux + hard burn
# ---------------------------------------------------------------------------

def soft_mux_subs(mp4_in: str, srt_in: str, mp4_out: str, ffmpeg_bin: str) -> None:
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error",
        "-i", mp4_in,
        "-i", srt_in,
        "-map", "0:v?", "-map", "0:a?", "-map", "1:s",
        "-c:v", "copy", "-c:a", "copy",
        "-c:s", "mov_text",
        "-metadata:s:s:0", "language=eng",
        mp4_out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        die(f"ffmpeg soft-mux failed:\n{r.stderr[-800:]}")


def burn_subs(mp4_in: str, ass_in: str, mp4_out: str, ffmpeg_bin: str) -> None:
    # libass's ass= filter takes a path with sensitive characters; escape ':',
    # apostrophe, and backslash inside single-quoted filter arg.
    ass_escaped = ass_in.replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")
    cmd = [
        ffmpeg_bin, "-y", "-loglevel", "error",
        "-i", mp4_in,
        "-vf", f"ass='{ass_escaped}'",
        "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        mp4_out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        die(f"ffmpeg burn failed:\n{r.stderr[-800:]}")


# ---------------------------------------------------------------------------
# Probe video dimensions (for ASS PlayResX/Y)
# ---------------------------------------------------------------------------

def probe_video_dims(mp4_path: str, ffprobe_bin: str) -> tuple[int, int]:
    cmd = [
        ffprobe_bin, "-v", "quiet", "-print_format", "json",
        "-select_streams", "v:0", "-show_streams", mp4_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        data = json.loads(r.stdout)
        s = data["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:
        warn("Could not probe video dimensions; defaulting to 1920×1080.")
        return 1920, 1080


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add captions (soft-embedded by default, optional hard-burn) "
                    "to an MP4 via whisper.cpp transcription."
    )
    parser.add_argument("input", nargs="?", default=None,
                        help="Input MP4 file. Omit to be prompted.")
    parser.add_argument("--burn", action="store_true",
                        help="Additionally produce a hard-burned MP4 (default: off).")
    parser.add_argument("--word-by-word", action="store_true",
                        help="Single-word flash captions instead of phrase chunks.")
    parser.add_argument("--model", default=DEFAULT_MODEL_SIZE,
                        help=f"Model size keyword (tiny|base|small|medium|large) or a "
                             f"path to a ggml-*.bin file. Default: {DEFAULT_MODEL_SIZE}.")
    parser.add_argument("--model-dir", default=DEFAULT_MODEL_DIR,
                        help=f"Where to look for ggml-<size>.bin model files "
                             f"(default: {DEFAULT_MODEL_DIR}).")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
                        help=f"ISO language code (e.g. en) or 'auto'. "
                             f"Default: {DEFAULT_LANGUAGE}.")
    parser.add_argument("--font", default=DEFAULT_FONT,
                        help=f"Font for burn-in (default: {DEFAULT_FONT}).")
    parser.add_argument("--size", type=int, default=None,
                        help=f"Font size for burn-in (default: {DEFAULT_PHRASE_SIZE} "
                             f"phrase, {DEFAULT_WORD_SIZE} word-by-word).")
    parser.add_argument("--keep-srt-only", action="store_true",
                        help="Write the SRT and stop. No soft-mux or burn.")
    args = parser.parse_args()

    # ── Resolve input ────────────────────────────────────────────────────────
    if args.input:
        input_path = sanitize_path(args.input)
    else:
        input_path = prompt_for_mp4()
    input_path = os.path.abspath(os.path.expanduser(input_path))

    if not os.path.isfile(input_path):
        die(f"File not found: {input_path}")
    ext = os.path.splitext(input_path)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        warn(f"Input doesn't look like a video file (ext: {ext or 'none'}). "
             f"Trying anyway.")

    # ── Tool discovery ───────────────────────────────────────────────────────
    ffmpeg_bin  = find_tool("ffmpeg",  ["/opt/homebrew/bin/ffmpeg",  "/usr/bin/ffmpeg"])
    ffprobe_bin = find_tool("ffprobe", ["/opt/homebrew/bin/ffprobe", "/usr/bin/ffprobe"])
    whisper_bin = find_tool("whisper-cli",
                            ["/opt/homebrew/bin/whisper-cli"],
                            install_hint="brew install whisper-cpp")

    # ── Resolve model path ───────────────────────────────────────────────────
    model_path = resolve_model_path(args.model, os.path.expanduser(args.model_dir))

    # ── Output paths ─────────────────────────────────────────────────────────
    stem, _      = os.path.splitext(input_path)
    srt_path     = stem + ".srt"
    subbed_path  = stem + "_subbed.mp4"
    burned_path  = stem + "_burned.mp4"

    burn_size = args.size if args.size is not None else (
        DEFAULT_WORD_SIZE if args.word_by_word else DEFAULT_PHRASE_SIZE
    )

    print(f"\n{'='*60}")
    print(f"  CAPTIONS — Video Intake Pipeline (optional add-on)")
    print(f"{'='*60}")
    print(f"  Input    : {input_path}")
    print(f"  Style    : {'word-by-word' if args.word_by_word else 'phrase'}")
    print(f"  Whisper  : {whisper_bin}")
    print(f"  Model    : {model_path}")
    print(f"  Language : {args.language}")
    print(f"  Outputs  : {os.path.basename(srt_path)}"
          + ("" if args.keep_srt_only else f", {os.path.basename(subbed_path)}")
          + (f", {os.path.basename(burned_path)}" if args.burn else ""))
    print(f"{'='*60}\n")

    # ── Step 1: Extract audio ────────────────────────────────────────────────
    header("Step 1: Extract audio")
    tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="caps_audio_")
    os.close(tmp_fd)
    try:
        extract_audio_to_wav(input_path, tmp_wav, ffmpeg_bin)
        ok("Audio extracted (16 kHz mono)")

        # ── Step 2: Transcribe ──────────────────────────────────────────────
        header("Step 2: Transcribe (whisper.cpp)")
        say("Running whisper-cli (Metal-accelerated on Apple Silicon)…")
        words = transcribe(tmp_wav, model_path, args.language, whisper_bin)
        ok(f"Transcribed {len(words)} word(s).")
        if not words:
            warn("No speech detected. SRT will be empty.")
    finally:
        try: os.unlink(tmp_wav)
        except OSError: pass

    # ── Step 3: Build SRT ────────────────────────────────────────────────────
    header("Step 3: Build SRT")
    if args.word_by_word:
        srt_entries = list(words)
        say(f"Style: word-by-word — {len(srt_entries)} flash entry(s).")
    else:
        srt_entries = group_into_phrases(words)
        say(f"Style: phrase — grouped into {len(srt_entries)} entry(s).")
    write_srt(srt_entries, srt_path)
    ok(f"Wrote {os.path.basename(srt_path)}")

    if args.keep_srt_only:
        header("Done")
        ok(f"SRT: {srt_path}\n")
        header("Next step")
        print(f"  open \"{srt_path}\"")
        print()
        return

    # ── Step 4: Soft-mux ─────────────────────────────────────────────────────
    header("Step 4: Mux soft subtitles into MP4")
    say("Copying video/audio streams and adding mov_text subtitle track…")
    soft_mux_subs(input_path, srt_path, subbed_path, ffmpeg_bin)
    ok(f"Soft-subbed MP4: {os.path.basename(subbed_path)}")

    # ── Step 5: Hard burn (optional) ─────────────────────────────────────────
    if args.burn:
        header("Step 5: Burn hardsub MP4")
        w, h = probe_video_dims(input_path, ffprobe_bin)
        ass_fd, ass_path = tempfile.mkstemp(suffix=".ass", prefix="caps_burn_")
        os.close(ass_fd)
        try:
            style_mode = "word" if args.word_by_word else "phrase"
            write_ass(srt_entries, style_mode, args.font, burn_size, w, h, ass_path)
            say(f"Burning {burn_size}pt {args.font} via libass (re-encode, "
                f"will take longer than the soft-mux step)…")
            burn_subs(input_path, ass_path, burned_path, ffmpeg_bin)
            ok(f"Burned MP4: {os.path.basename(burned_path)}")
        finally:
            try: os.unlink(ass_path)
            except OSError: pass

    # ── Done ─────────────────────────────────────────────────────────────────
    header("Done")
    ok(f"SRT:    {srt_path}")
    ok(f"Soft:   {subbed_path}")
    if args.burn:
        ok(f"Burned: {burned_path}")
    print()

    header("Next step")
    final_path = burned_path if args.burn else subbed_path
    print(f"  open \"{final_path}\"")
    if not args.burn:
        print()
        print("  Captions are toggleable in QuickTime via View > Subtitles.")
        print("  Re-run with --burn to also produce a hardsub copy.")
    print()


if __name__ == "__main__":
    main()
