#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcode.py — Step 2 of the Video Intake & Blender VSE Pipeline

Reads manifest.json produced by ingest.py and processes every file:

    Images        → 6-second slow-zoom MP4  (1s static + 5s Ken Burns, libx264)
    iPhone HDR    → SDR via avconvert (PresetAppleM4V1080pHD), then ffmpeg for
                    any remaining transforms (VFR→CFR, vertical→16:9) + remux
    Other HDR     → SDR via ffmpeg zscale tone-map
    VFR video     → CFR 29.97  (ffmpeg -fps_mode cfr)
    Vertical video → 16:9 blurred letterbox  (ffmpeg filtergraph)

All processed files land in  <project_dir>/_ingest/transcoded/
Already-transcoded files are skipped (re-entrant).
On completion, writes  transcoded/manifest_transcoded.json  that mirrors
the original manifest but with updated paths, dimensions, and flags — ready
for the sort + Blender-import steps.

Usage:
    python3 transcode.py /path/to/project_folder
    python3 transcode.py /path/to/project_folder --output /path/to/transcoded/
    python3 transcode.py /path/to/project_folder --dry-run

Dependencies:
    avconvert    (macOS built-in, /usr/bin/avconvert) — iPhone HDR→SDR
    ffmpeg       (with libx264, zscale filter)
    ffprobe      (ffmpeg suite)
    ImageMagick `convert`  (for image→video padding step)

Platform: macOS primary, Linux Mint compatible.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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
def skip(msg):   print(_c("0;35",  f"↩ {msg}"))


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def find_tool(name: str, hints: list[str] | None = None) -> str:
    """Return absolute path to `name` or die."""
    import shutil
    found = shutil.which(name)
    if not found and hints:
        for h in hints:
            if h and os.path.isfile(h) and os.access(h, os.X_OK):
                found = h
                break
    if not found:
        die(
            f"'{name}' not found. Install it or add it to PATH.\n"
            f"  macOS:  brew install ffmpeg   (or ImageMagick for 'convert')\n"
            f"  Linux:  sudo apt install ffmpeg imagemagick"
        )
    return found


# ---------------------------------------------------------------------------
# ffprobe helper (used for post-transcode verification)
# ---------------------------------------------------------------------------

def probe_video(path: str, ffprobe_bin: str) -> dict:
    cmd = [
        ffprobe_bin, "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Rotation detection via side_data_list (iPhone stores rotation there)
# ---------------------------------------------------------------------------

def get_rotation(path: str, ffprobe_bin: str) -> int:
    """
    Return the display rotation in degrees for the first video stream.
    Checks side_data_list for a Display Matrix entry (modern iPhones store
    rotation here, not in the tags dict).  Returns 0 if not found.
    """
    cmd = [
        ffprobe_bin, "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout)
    except Exception:
        return 0

    for stream in data.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        for sd in stream.get("side_data_list", []):
            if "rotation" in sd:
                try:
                    return abs(int(sd["rotation"])) % 360
                except (ValueError, TypeError):
                    pass
        # Fallback: legacy rotate tag in stream tags
        rotate = stream.get("tags", {}).get("rotate", "")
        try:
            return abs(int(rotate)) % 360
        except (ValueError, TypeError):
            pass

    return 0


# ---------------------------------------------------------------------------
# Image → 6-second slow-zoom MP4
# ---------------------------------------------------------------------------

IMAGE_DURATION_S = 6      # total clip length
IMAGE_STATIC_S   = 1      # lead-in static second
IMAGE_ZOOM_S     = 5      # Ken Burns zoom
IMAGE_FPS        = "30000/1001"   # 29.97 — must match project timeline to avoid stutter
IMAGE_FPS_NUM    = 29.97          # numeric version for frame-count calculations (e.g. zoompan d=)
IMAGE_RESOLUTION = "1920x1080"
TARGET_W, TARGET_H = 1920, 1080
PAD_W,  PAD_H  = 3840, 2160   # pad canvas before zoompan


def convert_image_to_video(src: str, dst: str, convert_bin: str, ffmpeg_bin: str,
                            dry_run: bool = False) -> bool:
    """
    1. ImageMagick: pad image to 3840×2160 on black → output.jpg (temp)
    2. ffmpeg:  1s static clip  →  output_static.mp4   (temp)
    3. ffmpeg:  5s slow-zoom    →  output_zoom.mp4     (temp)
    4. ffmpeg:  concat both     →  dst
    """
    base = dst + "_tmp"
    padded  = base + "_padded.jpg"
    static  = base + "_static.mp4"
    zoomed  = base + "_zoom.mp4"

    say(f"  image→video  {os.path.basename(src)}")

    if dry_run:
        skip("  [dry-run] would run ImageMagick + ffmpeg (3 passes)")
        return True

    try:
        # ── 1. Pad to 4K canvas ─────────────────────────────────────────────
        subprocess.run([
            convert_bin, src,
            "-auto-orient",
            "-resize", f"{PAD_W}x{PAD_H}",
            "-size", f"{PAD_W}x{PAD_H}",
            "xc:black", "+swap",
            "-gravity", "center",
            "-composite",
            padded,
        ], check=True, capture_output=True)

        # ── 2. 1-second static lead-in ──────────────────────────────────────
        subprocess.run([
            ffmpeg_bin, "-y",
            "-loop", "1", "-i", padded,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-vf", f"scale={TARGET_W}:{TARGET_H}",
            "-r", str(IMAGE_FPS), "-t", str(IMAGE_STATIC_S),
            static,
        ], check=True, capture_output=True)

        # ── 3. 5-second slow Ken Burns zoom ────────────────────────────────
        zoompan = (
            f"zoompan="
            f"z='min(zoom+0.001,1.1)':"
            f"d={int(IMAGE_ZOOM_S * IMAGE_FPS_NUM)}:"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"s={IMAGE_RESOLUTION}"
        )
        subprocess.run([
            ffmpeg_bin, "-y",
            "-loop", "1", "-i", padded,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-vf", zoompan,
            "-r", str(IMAGE_FPS), "-t", str(IMAGE_ZOOM_S),
            zoomed,
        ], check=True, capture_output=True)

        # ── 4. Concatenate ──────────────────────────────────────────────────
        subprocess.run([
            ffmpeg_bin, "-y",
            "-i", static, "-i", zoomed,
            "-filter_complex", "[0:v:0][1:v:0]concat=n=2:v=1[v]",
            "-map", "[v]",
            "-c:v", "libx264",
            dst,
        ], check=True, capture_output=True)

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if e.stderr else ""
        warn(f"  image→video failed for {os.path.basename(src)}:\n{stderr[-800:]}")
        return False
    finally:
        for tmp in (padded, static, zoomed):
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass

    ok(f"  {os.path.basename(dst)}")
    return True


# ---------------------------------------------------------------------------
# Video transcode  (HDR→SDR / VFR→CFR / vertical→16:9)
# ---------------------------------------------------------------------------

# Vertical → 16:9 blurred letterbox filtergraph.
# Foreground (portrait) is scaled to fit height=1080, centred on a blurred
# + scaled background.  Both streams come from the single input via split.
VERTICAL_FILTER = (
    "split=2[bg][fg];"
    "[bg]scale=1920:1080:force_original_aspect_ratio=increase,"
    "crop=1920:1080,"
    "gblur=sigma=120,"
    "eq=brightness=-0.2[blurred];"
    "[fg]scale=-2:1080[portrait];"
    "[blurred][portrait]overlay=(W-w)/2:(H-h)/2[out]"
)

# HDR → SDR tone-map filtergraph (zscale → tonemap → zscale → format)
HDR_TONEMAP_FILTER = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def build_ffmpeg_filter(is_hdr: bool, is_vertical: bool) -> str | None:
    """
    Return a -vf / -filter_complex string for ffmpeg covering HDR→SDR and/or
    vertical→16:9.  Called after avconvert has already handled iPhone HDR, so
    is_hdr here means non-iPhone HDR only.
    Returns None if no filter is needed.
    """
    if is_vertical and is_hdr:
        hdr_part = HDR_TONEMAP_FILTER + "[hdr_out]"
        v_part   = (
            "[hdr_out]split=2[bg][fg];"
            "[bg]scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080,"
            "gblur=sigma=120,"
            "eq=brightness=-0.2[blurred];"
            "[fg]scale=-2:1080[portrait];"
            "[blurred][portrait]overlay=(W-w)/2:(H-h)/2[out]"
        )
        return hdr_part + ";" + v_part   # filter_complex with named [out]

    if is_vertical:
        return VERTICAL_FILTER            # filter_complex with named [out]

    if is_hdr:
        return HDR_TONEMAP_FILTER         # simple -vf chain

    return None


def avconvert_hdr_to_sdr(src: str, m4v_out: str, avconvert_bin: str,
                          dry_run: bool = False) -> bool:
    """
    Run avconvert to convert an iPhone HDR clip to SDR .m4v.
    Returns True on success.
    """
    say(f"    avconvert HDR→SDR  {os.path.basename(src)}")
    if dry_run:
        skip(f"    [dry-run] avconvert --preset PresetAppleM4V1080pHD --source \"{src}\" --output \"{m4v_out}\"")
        return True

    cmd = [
        avconvert_bin,
        "--preset", "PresetAppleM4V1080pHD",
        "--source", src,
        "--output", m4v_out,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=3600)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            warn(f"    avconvert failed:\n{stderr[-800:]}")
            return False
    except subprocess.TimeoutExpired:
        warn(f"    avconvert timed out for {os.path.basename(src)}")
        return False

    ok(f"    avconvert done → {os.path.basename(m4v_out)}")
    return True


def find_audio_offset(video_path: str, audio_path: str) -> tuple[float, float]:
    """
    Run audio-offset-finder to locate video_path's audio within audio_path.
    Returns (time_offset_seconds, standard_score).
    """
    result = subprocess.run(
        ["audio-offset-finder", "--find-offset-of", video_path,
         "--within", audio_path, "--json"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "audio-offset-finder exited non-zero")
    data = json.loads(result.stdout)
    return float(data["time_offset"]), float(data["standard_score"])


def transcode_with_external_audio(entry: dict, dst: str, ffmpeg_bin: str,
                                   avconvert_bin: str, ffprobe_bin: str,
                                   target_fps: float = 29.97,
                                   dry_run: bool = False,
                                   loudnorm: bool = True) -> bool:
    """
    Replace a clip's audio with a synced external audio file.

    Uses audio-offset-finder to compute the sync offset, then runs ffmpeg with
    -map 0:v -map 1:a to keep the video track and swap in the external audio.
    Applies the same HDR/VFR/vertical transforms as transcode_video.

    Offset logic (mirrors merge.sh):
      offset < 0 → video started first → seek into video by abs(offset)
      offset >= 0 → audio started first → seek into audio by offset
    """
    src        = entry["path"]
    audio_path = entry["external_audio"]
    is_hdr     = entry.get("is_hdr", False)
    is_vfr     = entry.get("is_vfr", False)
    is_iphone  = entry.get("source") == "iphone"
    use_avconvert = is_hdr and is_iphone
    force_cfr  = is_iphone or is_vfr

    rotation   = get_rotation(src, ffprobe_bin)
    is_vertical = rotation in (90, 270)
    if not is_vertical:
        is_vertical = entry.get("orientation") == "vertical"

    say(f"  video+audio  {os.path.basename(src)}")
    say(f"               audio: {os.path.basename(audio_path)}")

    if dry_run:
        skip(f"    [dry-run] would find offset then ffmpeg with external audio")
        return True

    # Find sync offset
    say(f"    analysing audio offset…")
    try:
        offset, score = find_audio_offset(src, audio_path)
    except Exception as e:
        warn(f"    audio-offset-finder failed: {e}")
        return False

    say(f"    offset: {offset}s  (score: {score:.1f})")
    if score < 10:
        warn(f"    low confidence sync (score {score:.1f}) — result may be inaccurate")

    # Step 1: avconvert for iPhone HDR
    ffmpeg_src = src
    m4v_tmp    = None
    if use_avconvert:
        m4v_tmp = dst.replace(".mp4", "_sdr_tmp.m4v")
        if not avconvert_hdr_to_sdr(src, m4v_tmp, avconvert_bin):
            return False
        ffmpeg_src = m4v_tmp
        is_hdr = False

    vf_str            = build_ffmpeg_filter(is_hdr, is_vertical)
    use_filter_complex = is_vertical
    needs_video_work  = is_hdr or force_cfr or is_vertical

    LOUDNORM_FILTER = "loudnorm=I=-16:LRA=11:TP=-1.5"
    audio_args = ["-c:a", "aac", "-b:a", "192k"]
    if loudnorm:
        audio_args += ["-af", LOUDNORM_FILTER]

    # Build inputs — seek into whichever file started later
    if offset < 0:
        cmd = [ffmpeg_bin, "-y", "-ss", str(abs(offset)), "-i", ffmpeg_src, "-i", audio_path]
    else:
        cmd = [ffmpeg_bin, "-y", "-i", ffmpeg_src, "-ss", str(offset), "-i", audio_path]

    # Map: video from input 0, audio from input 1
    if use_filter_complex:
        cmd += ["-filter_complex", vf_str, "-map", "[out]", "-map", "1:a"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
        if vf_str:
            cmd += ["-vf", vf_str]

    if needs_video_work:
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p"]
        if force_cfr:
            cmd += ["-r", "30000/1001", "-fps_mode", "cfr"]
    else:
        cmd += ["-c:v", "copy"]

    cmd += audio_args
    cmd.append(dst)

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=3600)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            warn(f"  ffmpeg failed for {os.path.basename(ffmpeg_src)}:\n{stderr[-800:]}")
            return False
    except subprocess.TimeoutExpired:
        warn(f"  ffmpeg timed out for {os.path.basename(ffmpeg_src)}")
        return False
    finally:
        if m4v_tmp and os.path.isfile(m4v_tmp):
            try:
                os.remove(m4v_tmp)
            except OSError:
                pass

    ok(f"  {os.path.basename(dst)}")
    return True


def transcode_video(entry: dict, dst: str, ffmpeg_bin: str, avconvert_bin: str,
                    ffprobe_bin: str, target_fps: float = 29.97,
                    dry_run: bool = False, loudnorm: bool = True) -> bool:
    """
    Transcode a video file, applying whichever transforms are needed:

        iPhone HDR  → avconvert (PresetAppleM4V1080pHD) → .m4v intermediate,
                      then ffmpeg for VFR/vertical/remux → final .mp4
        Other HDR   → ffmpeg zscale tone-map
        VFR         → ffmpeg -fps_mode cfr
        vertical    → ffmpeg blurred letterbox filtergraph
        none        → ffmpeg stream-copy (fast)

    All iPhone clips are forced to CFR 29.97 regardless of VFR flag.
    Vertical detection reads side_data_list rotation via ffprobe, overriding
    the orientation field in the manifest (which can miss iPhone rotation tags).
    Audio is normalized to -16 LUFS / -1.5 dBTP via loudnorm unless
    loudnorm=False is passed.
    """
    src         = entry["path"]
    is_hdr      = entry.get("is_hdr", False)
    is_vfr      = entry.get("is_vfr", False)
    is_iphone   = entry.get("source") == "iphone"
    use_avconvert = is_hdr and is_iphone

    # ── Vertical detection: read rotation from side_data_list ────────────────
    rotation = get_rotation(src, ffprobe_bin)
    is_vertical = rotation in (90, 270)
    if not is_vertical:
        # Fall back to manifest orientation if no rotation tag found
        is_vertical = entry.get("orientation") == "vertical"

    # iPhone clips are always forced to CFR 29.97
    force_cfr = is_iphone or is_vfr

    flags = []
    if is_hdr:
        flags.append("HDR→SDR(avconvert)" if use_avconvert else "HDR→SDR(ffmpeg)")
    if force_cfr:   flags.append("CFR29.97")
    if is_vertical: flags.append("vertical→16:9")
    flag_str = " + ".join(flags) if flags else "stream-copy"

    say(f"  video  {os.path.basename(src)}  [{flag_str}]")

    if dry_run:
        if use_avconvert:
            m4v_tmp = dst.replace(".mp4", "_sdr_tmp.m4v")
            avconvert_hdr_to_sdr(src, m4v_tmp, avconvert_bin, dry_run=True)
        needs_ffmpeg = not use_avconvert or force_cfr or is_vertical
        if needs_ffmpeg:
            skip(f"    [dry-run] ffmpeg → {os.path.basename(dst)}")
        return True

    # ── Step 1: avconvert for iPhone HDR ────────────────────────────────────
    ffmpeg_src = src
    m4v_tmp    = None

    if use_avconvert:
        m4v_tmp = dst.replace(".mp4", "_sdr_tmp.m4v")
        success = avconvert_hdr_to_sdr(src, m4v_tmp, avconvert_bin)
        if not success:
            return False
        ffmpeg_src = m4v_tmp
        # After avconvert the clip is SDR — clear the flag for filter building
        is_hdr = False

    # ── Step 2: ffmpeg for everything else ──────────────────────────────────
    needs_ffmpeg_work = is_hdr or force_cfr or is_vertical
    vf_str = build_ffmpeg_filter(is_hdr, is_vertical)
    use_filter_complex = is_vertical

    cmd = [ffmpeg_bin, "-y", "-i", ffmpeg_src]

    LOUDNORM_FILTER = "loudnorm=I=-16:LRA=11:TP=-1.5"
    audio_args = ["-c:a", "aac", "-b:a", "192k"]
    if loudnorm:
        audio_args += ["-af", LOUDNORM_FILTER]

    if not needs_ffmpeg_work and use_avconvert:
        # avconvert already did the heavy lifting; just remux m4v → mp4
        cmd += ["-c:v", "copy"] + audio_args
    elif not needs_ffmpeg_work:
        # Nothing to do — stream-copy straight through
        cmd += ["-c:v", "copy"] + audio_args
    else:
        if use_filter_complex:
            cmd += ["-filter_complex", vf_str, "-map", "[out]", "-map", "0:a?"]
        elif vf_str:
            cmd += ["-vf", vf_str]
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p"]
        if force_cfr:
            cmd += ["-r", "30000/1001", "-fps_mode", "cfr"]
        cmd += audio_args

    cmd.append(dst)

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=3600)
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            warn(f"  ffmpeg failed for {os.path.basename(ffmpeg_src)}:\n{stderr[-800:]}")
            return False
    except subprocess.TimeoutExpired:
        warn(f"  ffmpeg timed out for {os.path.basename(ffmpeg_src)}")
        return False
    finally:
        # Clean up avconvert intermediate
        if m4v_tmp and os.path.isfile(m4v_tmp):
            try:
                os.remove(m4v_tmp)
            except OSError:
                pass

    ok(f"  {os.path.basename(dst)}")
    return True


# ---------------------------------------------------------------------------
# Manifest update
# ---------------------------------------------------------------------------

def output_entry(original: dict, dst_path: str, ffprobe_bin: str) -> dict:
    """
    Build an updated manifest entry for a transcoded file.
    Re-probes the output so width/height/fps/flags are accurate.
    """
    entry = dict(original)
    entry["source_path"] = original["path"]   # keep original for reference
    entry["path"]        = os.path.abspath(dst_path)
    entry["filename"]    = os.path.basename(dst_path)
    entry["size_bytes"]  = os.path.getsize(dst_path)

    # Re-probe
    probe = probe_video(dst_path, ffprobe_bin)
    fmt   = probe.get("format", {})
    video_stream = next(
        (s for s in probe.get("streams", []) if s.get("codec_type") == "video"),
        {}
    )
    w = video_stream.get("width")
    h = video_stream.get("height")
    if w and h:
        entry["width"]       = int(w)
        entry["height"]      = int(h)
        entry["orientation"] = "vertical" if int(h) > int(w) else "landscape"
    raw_dur = fmt.get("duration") or video_stream.get("duration")
    if raw_dur:
        entry["duration_s"] = round(float(raw_dur), 2)

    # After transcode these should always be False / CFR
    entry["is_hdr"] = False
    entry["is_vfr"] = False
    entry["media_type"] = "video"   # images became video

    return entry


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def sanitize_path(raw: str) -> str:
    """
    Clean up a path that may have been drag-and-dropped or copy-pasted
    on macOS — strips surrounding quotes, removes shell backslash escapes,
    and strips leading/trailing whitespace.
    """
    p = raw.strip()
    # Remove wrapping quotes (single or double)
    if len(p) >= 2 and p[0] in ('"', "'") and p[-1] == p[0]:
        p = p[1:-1]
    # Unescape backslash-escaped characters (macOS drag-and-drop adds these)
    p = p.replace("\\ ", " ")
    p = p.replace("\\&", "&")
    p = p.replace("\\(", "(")
    p = p.replace("\\)", ")")
    p = p.replace("\\[", "[")
    p = p.replace("\\]", "]")
    p = p.replace("\\!", "!")
    p = p.replace("\\#", "#")
    p = p.replace("\\$", "$")
    p = p.replace("\\@", "@")
    p = p.replace("\\,", ",")
    p = p.replace("\\;", ";")
    return p.strip()


def prompt_for_project_dir() -> str:
    """Interactively ask the user for the project folder, supporting drag-and-drop."""
    print()
    print("  Drag-and-drop your project folder here, or paste/type the path:")
    print("  > ", end="", flush=True)
    raw = input()
    return sanitize_path(raw)


def resolve_manifest(project_dir: str) -> str:
    """
    Given a project folder, find manifest.json inside _ingest/.
    Dies with a clear message if not found.
    """
    candidate = os.path.join(project_dir, "_ingest", "manifest.json")
    if os.path.isfile(candidate):
        return candidate
    die(
        f"No manifest found at: {candidate}\n"
        f"  Run ingest.py first:  python3 ingest.py \"{project_dir}\""
    )


def main():
    parser = argparse.ArgumentParser(
        description="Transcode step: reads _ingest/manifest.json and processes all media files."
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=None,
        help="Project folder containing _ingest/manifest.json. "
             "If omitted, you will be prompted to enter or drag-and-drop the path.",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output folder (default: <project_dir>/_ingest/transcoded/)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=29.97,
        help="Target CFR frame rate (default: 29.97)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without running any tools.",
    )
    parser.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip loudnorm audio normalization (default: enabled, -16 LUFS / -1.5 dBTP).",
    )
    args = parser.parse_args()

    # ── Resolve project directory ────────────────────────────────────────────
    if args.project_dir:
        raw_dir = sanitize_path(args.project_dir)
    else:
        raw_dir = prompt_for_project_dir()

    project_dir = os.path.abspath(os.path.expanduser(raw_dir))
    if not os.path.isdir(project_dir):
        print(f"\nError: '{project_dir}' is not a directory.", file=sys.stderr)
        print("Tip: try drag-and-dropping the folder directly into the terminal.", file=sys.stderr)
        sys.exit(1)

    manifest_path = resolve_manifest(project_dir)

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    entries    = manifest.get("files", [])
    input_dir  = manifest.get("input_dir", project_dir)

    output_dir = args.output or os.path.join(project_dir, "_ingest", "transcoded")
    os.makedirs(output_dir, exist_ok=True)

    # ── Tool discovery ───────────────────────────────────────────────────────
    ffmpeg_bin     = find_tool("ffmpeg",     ["/opt/homebrew/bin/ffmpeg",  "/usr/bin/ffmpeg"])
    ffprobe_bin    = find_tool("ffprobe",    ["/opt/homebrew/bin/ffprobe", "/usr/bin/ffprobe"])
    convert_bin    = find_tool("convert",    ["/opt/homebrew/bin/convert", "/usr/bin/convert"])
    avconvert_bin  = find_tool("avconvert",  ["/usr/bin/avconvert"])
    if any(e.get("external_audio") for e in entries):
        find_tool("audio-offset-finder")

    # ── Summary ──────────────────────────────────────────────────────────────
    images        = [e for e in entries if e["media_type"] == "image"]
    videos        = [e for e in entries if e["media_type"] == "video"]
    hdr_iphone    = [e for e in videos  if e.get("is_hdr") and e.get("source") == "iphone"]
    hdr_other     = [e for e in videos  if e.get("is_hdr") and e.get("source") != "iphone"]
    iphone_v      = [e for e in videos  if e.get("source") == "iphone"]
    vert_v        = [e for e in videos  if e.get("orientation") == "vertical"]
    ext_audio_v   = [e for e in videos  if e.get("external_audio")]
    passthru      = [e for e in videos  if not e.get("is_hdr") and e.get("source") != "iphone"
                                           and e.get("orientation") != "vertical"
                                           and not e.get("external_audio")]

    print(f"\n{'='*60}")
    print(f"  TRANSCODE — Video Intake Pipeline")
    print(f"{'='*60}")
    print(f"  Project  : {project_dir}")
    print(f"  Manifest : {manifest_path}")
    print(f"  Output   : {output_dir}")
    print(f"  Files    : {len(entries)} total")
    print(f"             {len(images)} image(s) → slow-zoom video")
    print(f"             {len(hdr_iphone)} iPhone HDR video(s) → SDR via avconvert")
    print(f"             {len(hdr_other)} other HDR video(s) → SDR via ffmpeg")
    print(f"             {len(iphone_v)} iPhone video(s) → forced CFR 29.97")
    print(f"             {len(vert_v)} vertical video(s) → 16:9 letterbox (manifest; re-checked per clip)")
    if ext_audio_v:
        print(f"             {len(ext_audio_v)} clip(s) → audio replaced from external file")
    print(f"             {len(passthru)} GoPro/other video(s) passing through unchanged")
    if args.dry_run:
        print(f"  Mode     : DRY RUN — no files will be written")
    print(f"{'='*60}\n")

    if not entries:
        print("Nothing to do — manifest is empty.")
        sys.exit(0)

    # ── Process each file ────────────────────────────────────────────────────
    results   = []
    succeeded = 0
    failed    = 0
    skipped   = 0
    total     = len(entries)

    for idx, entry in enumerate(entries, 1):
        src      = entry["path"]
        basename = entry["filename"]
        stem     = Path(basename).stem
        mtype    = entry["media_type"]
        prefix   = f"[{idx}/{total}]"

        print(f"{prefix} {basename}")

        if not os.path.isfile(src):
            warn(f"  Source not found, skipping: {src}")
            failed += 1
            continue

        # ── Images ──────────────────────────────────────────────────────────
        if mtype == "image":
            dst = os.path.join(output_dir, stem + ".mp4")
            if os.path.isfile(dst):
                skip(f"  already exists: {os.path.basename(dst)}")
                skipped += 1
                results.append(output_entry(entry, dst, ffprobe_bin))
                continue

            success = convert_image_to_video(src, dst, convert_bin, ffmpeg_bin, args.dry_run)
            if success and not args.dry_run:
                succeeded += 1
                results.append(output_entry(entry, dst, ffprobe_bin))
            elif success and args.dry_run:
                succeeded += 1
                # dry-run: record as-is but note dst
                e2 = dict(entry); e2["path"] = dst; results.append(e2)
            else:
                failed += 1

        # ── Videos ──────────────────────────────────────────────────────────
        elif mtype == "video":
            dst = os.path.join(output_dir, stem + ".mp4")
            if os.path.isfile(dst):
                skip(f"  already exists: {os.path.basename(dst)}")
                skipped += 1
                results.append(output_entry(entry, dst, ffprobe_bin))
                continue

            if entry.get("external_audio"):
                success = transcode_with_external_audio(entry, dst, ffmpeg_bin, avconvert_bin, ffprobe_bin, args.fps, args.dry_run, not args.no_loudnorm)
            else:
                success = transcode_video(entry, dst, ffmpeg_bin, avconvert_bin, ffprobe_bin, args.fps, args.dry_run, not args.no_loudnorm)
            if success and not args.dry_run:
                succeeded += 1
                results.append(output_entry(entry, dst, ffprobe_bin))
            elif success and args.dry_run:
                succeeded += 1
                e2 = dict(entry); e2["path"] = dst; results.append(e2)
            else:
                failed += 1

        else:
            warn(f"  Unknown media_type '{mtype}', skipping.")
            failed += 1

        print()  # blank line between files

    # ── Write updated manifest ───────────────────────────────────────────────
    if not args.dry_run and results:
        # Sort by creation_time (mirrors ingest.py sort)
        def sort_key(e):
            ct = e.get("creation_time") or ""
            return (0 if ct else 1, ct, e.get("filename", ""))
        results.sort(key=sort_key)

        out_manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "input_dir":    input_dir,
            "transcoded_dir": os.path.abspath(output_dir),
            "source_manifest": manifest_path,
            "file_count":   len(results),
            "files":        results,
        }
        out_path = os.path.join(output_dir, "manifest_transcoded.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out_manifest, f, indent=2)
        ok(f"Updated manifest → {out_path}")

    # ── Final summary ────────────────────────────────────────────────────────
    header("Done")
    ok(f"Succeeded : {succeeded}")
    if skipped:  print(f"  Skipped  : {skipped}  (already existed)")
    if failed:   warn(f"Failed    : {failed}  (see warnings above)")
    print(f"  Output   : {output_dir}\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
