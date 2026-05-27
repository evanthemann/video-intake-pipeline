#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ingest.py — Step 1 of the Video Intake & Blender VSE Pipeline

Scans an input folder for video and image files, extracts metadata via
ffprobe / ImageMagick, and writes three output files:

    manifest.json       — machine-readable, consumed by downstream steps
    clips_ordered.txt   — human-readable ordered list, one line per file
    ingest_report.md    — narrative summary: counts, flags, timeline gaps

Usage:
    python3 ingest.py /path/to/footage
    python3 ingest.py /path/to/footage --output /path/to/output_dir

Dependencies:
    ffprobe   (ffmpeg suite)
    identify  (ImageMagick) — for image dimensions when ffprobe can't read them

Platform: macOS primary, Linux Mint compatible.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts", ".m2ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff", ".dng", ".raw", ".arw", ".cr2"}

# GoPro filename prefixes (case-insensitive)
GOPRO_PREFIXES = ("gh", "gp", "gopr", "gl")

# Column widths for clips_ordered.txt
COL_WIDTHS = {
    "idx":         4,
    "datetime":   20,
    "source":      7,
    "media_type":  6,
    "orient":     10,
    "duration":    8,
    "filename":    0,   # variable — no truncation
}

# Gap threshold for ingest_report (seconds)
GAP_THRESHOLD_S = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Source detection
# ---------------------------------------------------------------------------

def detect_source(filename: str, ffprobe_tags: dict) -> str:
    """Return 'gopro', 'iphone', or 'unknown'."""
    base = os.path.basename(filename).lower()

    # 1. Filename prefix heuristic
    for prefix in GOPRO_PREFIXES:
        if base.startswith(prefix):
            return "gopro"

    # 2. ffprobe tags
    make = (
        ffprobe_tags.get("com.apple.quicktime.make", "")
        or ffprobe_tags.get("make", "")
    ).lower()
    encoder = ffprobe_tags.get("encoder", "").lower()
    handler = ffprobe_tags.get("handler_name", "").lower()

    if "apple" in make:
        return "iphone"
    if "gopro" in encoder or "gopro" in handler:
        return "gopro"

    # 3. iPhone naming patterns
    if re.match(r"img_\d+", base) or base.startswith("rpreplay"):
        return "iphone"

    return "unknown"


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------

def run_ffprobe(path: str) -> dict:
    """
    Run ffprobe on a file and return a dict with:
        streams, format, tags (merged from format + video stream)
    Returns empty dict on failure.
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    except Exception as e:
        print(f"  [warn] ffprobe failed on {os.path.basename(path)}: {e}", file=sys.stderr)
        return {}


def get_video_stream(probe: dict) -> dict:
    """Return the first video stream dict, or {}."""
    for s in probe.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    return {}


def parse_creation_time(tags: dict) -> str | None:
    """
    Extract an ISO-8601 UTC creation_time string from merged ffprobe tags.
    Returns None if not found or unparseable.
    """
    raw = tags.get("creation_time", "")
    if not raw:
        return None
    # ffprobe usually returns e.g. "2025-08-14T09:32:11.000000Z"
    raw = raw.rstrip("Z").split(".")[0]
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def parse_naive_creation_dt(tags: dict) -> datetime | None:
    """Parse the raw ffprobe creation_time tag into a *naive* datetime (no tz)."""
    raw = tags.get("creation_time", "")
    if not raw:
        return None
    raw = raw.rstrip("Z").split(".")[0]
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def clip_records_utc(tags: dict) -> bool:
    """
    Return True only if the file carries positive proof its creation_time is true UTC.

    iPhones write an Apple make plus com.apple.quicktime.creationdate (local time +
    explicit offset); GoPros stamp a 'GoPro …' handler/encoder. Both store
    creation_time in real UTC. Cameras like the Canon Vixia / 7D and iVue Rincon
    write naive local wall-clock time but still label it 'Z' — they leave no UTC
    proof, so we treat them as naive-local and correct them with a batch offset.
    Note we deliberately ignore any embedded per-camera timezone tag (e.g. the
    Vixia's [Canon] TimeZone): it is often misconfigured and would re-break order.
    """
    make = (
        tags.get("com.apple.quicktime.make", "")
        or tags.get("make", "")
    ).lower()
    if "apple" in make or tags.get("com.apple.quicktime.creationdate"):
        return True
    encoder = tags.get("encoder", "").lower()
    handler = tags.get("handler_name", "").lower()
    if "gopro" in encoder or "gopro" in handler:
        return True
    return False


def parse_apple_offset_minutes(tags: dict) -> int | None:
    """
    Extract the local UTC offset (minutes) from an Apple
    com.apple.quicktime.creationdate tag, e.g. '2026-05-25T14:36:59-0400' → -240.
    Returns None if the tag is absent or carries no offset.
    """
    raw = tags.get("com.apple.quicktime.creationdate", "")
    m = re.search(r"([+-])(\d{2}):?(\d{2})$", raw)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return sign * (int(m.group(2)) * 60 + int(m.group(3)))


def machine_utc_offset_minutes() -> int:
    """This machine's current local UTC offset, in minutes (respects current DST)."""
    off = datetime.now(timezone.utc).astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


def fmt_offset(minutes: int) -> str:
    """Render an offset in minutes as 'UTC-04:00'."""
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def parse_fps(stream: dict) -> float | None:
    """Parse avg_frame_rate or r_frame_rate into a float."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        val = stream.get(key, "")
        if val and "/" in val:
            try:
                num, den = val.split("/")
                if int(den) == 0:
                    continue
                return round(int(num) / int(den), 3)
            except (ValueError, ZeroDivisionError):
                pass
    return None


def detect_hdr(stream: dict) -> bool:
    """Heuristic: color_transfer contains 'smpte2084' (PQ) or 'arib-std-b67' (HLG)."""
    ct = stream.get("color_transfer", "").lower()
    return "smpte2084" in ct or "arib" in ct or "hlg" in ct


def detect_vfr(stream: dict) -> bool:
    """
    VFR if avg_frame_rate and r_frame_rate differ meaningfully,
    or if r_frame_rate has an unusual denominator.
    """
    avg = stream.get("avg_frame_rate", "")
    r = stream.get("r_frame_rate", "")
    if not avg or not r:
        return False
    if avg == r:
        return False
    try:
        def to_float(s):
            n, d = s.split("/")
            return int(n) / int(d)
        diff = abs(to_float(avg) - to_float(r))
        return diff > 0.5
    except Exception:
        return False


def get_rotation_from_probe(probe: dict) -> int:
    """
    Return the display rotation in degrees for the first video stream.
    Checks side_data_list for a Display Matrix entry (modern iPhones store
    rotation here, not in the tags dict).  Falls back to the legacy 'rotate'
    tag in stream tags.  Returns 0 if not found.
    """
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        for sd in stream.get("side_data_list", []):
            if "rotation" in sd:
                try:
                    return abs(int(sd["rotation"])) % 360
                except (ValueError, TypeError):
                    pass
        # Fallback: legacy rotate tag
        rotate = stream.get("tags", {}).get("rotate", "")
        try:
            return abs(int(rotate)) % 360
        except (ValueError, TypeError):
            pass
    return 0


# ---------------------------------------------------------------------------
# ImageMagick fallback for images
# ---------------------------------------------------------------------------

def image_dimensions_via_identify(path: str) -> tuple[int | None, int | None]:
    """Use ImageMagick `identify` to get width × height."""
    try:
        result = subprocess.run(
            ["identify", "-format", "%w %h", path],
            capture_output=True, text=True, timeout=15
        )
        parts = result.stdout.strip().split()
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return None, None


def image_orientation_via_exiftool(path: str) -> str | None:
    """
    Use exiftool to read the EXIF Orientation tag and return 'vertical',
    'landscape', or None if exiftool is unavailable or tag is missing.
    Rotate 90 CW / Rotate 270 CW → vertical; everything else → landscape.
    """
    try:
        result = subprocess.run(
            ["exiftool", "-Orientation", "-s3", path],
            capture_output=True, text=True, timeout=10
        )
        val = result.stdout.strip().lower()
        if not val:
            return None
        if "90" in val or "270" in val:
            return "vertical"
        return "landscape"
    except FileNotFoundError:
        pass  # exiftool not installed
    except Exception:
        pass
    return None


def image_creation_time_via_exiftool(path: str) -> str | None:
    """
    Read DateTimeOriginal + OffsetTimeOriginal from EXIF and return an
    ISO-8601 UTC timestamp string. Falls back gracefully if exiftool is
    not installed or tags are missing.
    """
    try:
        result = subprocess.run(
            ["exiftool", "-DateTimeOriginal", "-OffsetTimeOriginal", "-s3", path],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        if not lines:
            return None

        # First line is DateTimeOriginal: "2026:04:29 15:24:58"
        raw_dt = lines[0]
        # Second line (if present) is OffsetTimeOriginal: "-04:00"
        offset_str = lines[1] if len(lines) > 1 else None

        # Parse the datetime
        dt = datetime.strptime(raw_dt, "%Y:%m:%d %H:%M:%S")

        if offset_str:
            # Parse offset e.g. "-04:00" or "+05:30"
            sign = 1 if offset_str[0] == "+" else -1
            oh, om = offset_str[1:].split(":")
            offset_minutes = sign * (int(oh) * 60 + int(om))
            # Convert local → UTC
            dt = dt.replace(tzinfo=timezone.utc) - timedelta(minutes=offset_minutes)
        else:
            # No offset — assume UTC (best we can do)
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    except FileNotFoundError:
        pass  # exiftool not installed — not required
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-file inspection
# ---------------------------------------------------------------------------

def inspect_file(path: str) -> dict:
    """
    Inspect a single file and return a manifest entry dict.
    """
    ext = os.path.splitext(path)[1].lower()
    filename = os.path.basename(path)
    size_bytes = os.path.getsize(path)

    is_video = ext in VIDEO_EXTENSIONS
    is_image = ext in IMAGE_EXTENSIONS
    media_type = "video" if is_video else "image" if is_image else "unknown"

    probe = run_ffprobe(path)
    fmt = probe.get("format", {})
    video_stream = get_video_stream(probe)

    # Merge tags: format-level tags take priority, then stream-level
    tags = {}
    tags.update(video_stream.get("tags", {}))
    tags.update(fmt.get("tags", {}))

    source = detect_source(filename, tags)

    # Dimensions
    width = video_stream.get("width") or fmt.get("width")
    height = video_stream.get("height") or fmt.get("height")

    if (not width or not height) and is_image:
        width, height = image_dimensions_via_identify(path)

    width = int(width) if width else None
    height = int(height) if height else None

    # Check for rotation — modern iPhones store this in side_data_list
    # (Display Matrix), not in the tags dict. get_rotation_from_probe handles
    # both. If 90/270, the logical w/h are swapped vs the pixel dimensions.
    if is_video:
        rotate_deg = get_rotation_from_probe(probe)
    else:
        # Legacy tags fallback for non-video (shouldn't normally apply)
        rotate_tag = tags.get("rotate") or video_stream.get("tags", {}).get("rotate") or ""
        try:
            rotate_deg = abs(int(rotate_tag)) % 360
        except (ValueError, TypeError):
            rotate_deg = 0

    if rotate_deg in (90, 270) and width and height:
        width, height = height, width

    # Orientation — for images use exiftool directly since raw pixel dims
    # don't reflect EXIF rotation (iPhone always stores landscape pixel arrays)
    if is_image:
        orientation = image_orientation_via_exiftool(path) or (
            "vertical" if (height and width and height > width) else "landscape"
        )
    elif width and height:
        orientation = "vertical" if height > width else "landscape"
    else:
        orientation = "unknown"

    # Duration
    duration_s = None
    if is_video:
        raw_dur = fmt.get("duration") or video_stream.get("duration")
        if raw_dur:
            try:
                duration_s = round(float(raw_dur), 2)
            except ValueError:
                pass

    # FPS
    fps = parse_fps(video_stream) if is_video else None

    # HDR / VFR
    is_hdr = detect_hdr(video_stream) if is_video else False
    is_vfr = detect_vfr(video_stream) if is_video else False

    # Creation time.
    # iPhone/GoPro write true UTC; images carry their own EXIF offset (exiftool
    # resolves it to UTC). Cameras that leave no UTC proof (Canon Vixia / 7D,
    # iVue Rincon) write naive local time mislabeled 'Z' — they are flagged here
    # and corrected to true UTC in scan_directory once the batch offset is known.
    records_utc = True
    naive_local_iso = None
    apple_offset_min = None

    if is_video:
        records_utc = clip_records_utc(tags)
        apple_offset_min = parse_apple_offset_minutes(tags)
        if records_utc:
            creation_time = parse_creation_time(tags)
        else:
            creation_time = None  # filled in by the batch-offset pass
            naive_dt = parse_naive_creation_dt(tags)
            naive_local_iso = naive_dt.isoformat() if naive_dt else None
    else:
        creation_time = parse_creation_time(tags)
        if not creation_time and is_image:
            creation_time = image_creation_time_via_exiftool(path)

    return {
        "path": os.path.abspath(path),
        "filename": filename,
        "source": source,
        "media_type": media_type,
        "orientation": orientation,
        "width": width,
        "height": height,
        "duration_s": duration_s,
        "fps": fps,
        "is_hdr": is_hdr,
        "is_vfr": is_vfr,
        "creation_time": creation_time,
        "creation_time_local": None,   # wall-clock display value, set in scan_directory
        "size_bytes": size_bytes,
        "_records_utc": records_utc,
        "_naive_local_iso": naive_local_iso,
        "_apple_offset_min": apple_offset_min,
    }


# ---------------------------------------------------------------------------
# Directory scan
# ---------------------------------------------------------------------------

def scan_directory(input_dir: str, local_offset_min: int | None = None,
                   output_dir: str | None = None) -> tuple[list[dict], dict]:
    """
    Recursively scan input_dir for media files.

    Returns (entries, tz_summary):
      entries     — manifest entry dicts, sorted by creation_time (true UTC);
                    files with no creation_time sort to the end, then by filename.
      tz_summary  — details of the timezone normalization, for the ingest report.

    local_offset_min, if given (from --local-offset), overrides offset detection.
    output_dir, if given, is pruned from the walk so re-runs don't re-ingest our
    own transcoded copies (which live in <output_dir>/transcoded).
    """
    all_extensions = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
    found_paths = []
    output_abs = os.path.abspath(output_dir) if output_dir else None

    for root, dirs, files in os.walk(input_dir):
        # Skip hidden dirs, bl_proxy folders, and our own output directory.
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d != "bl_proxy"
            and not (output_abs and os.path.abspath(os.path.join(root, d)) == output_abs)
        ]
        for fname in files:
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext in all_extensions:
                found_paths.append(os.path.join(root, fname))

    found_paths.sort()  # alphabetical pre-sort for stability

    entries = []
    total = len(found_paths)
    for i, path in enumerate(found_paths, 1):
        print(f"  [{i}/{total}] Inspecting {os.path.basename(path)} …")
        entries.append(inspect_file(path))

    # ── Timezone normalization ────────────────────────────────────────────────
    # Some cameras (Canon Vixia / 7D, iVue Rincon) record naive local wall-clock
    # time but label it UTC, so their clips sort hours away from iPhone/GoPro
    # clips that record true UTC. Pick ONE batch-wide local offset and rewrite
    # the naive clips to true UTC so every clip shares one clock.
    apple_offsets = [e["_apple_offset_min"] for e in entries if e["_apple_offset_min"] is not None]
    needs_fix = [e for e in entries if not e["_records_utc"] and e["_naive_local_iso"]]

    fallback_used = False
    if local_offset_min is not None:
        batch_offset = local_offset_min
        offset_source = f"--local-offset flag ({fmt_offset(batch_offset)})"
    elif apple_offsets:
        batch_offset = Counter(apple_offsets).most_common(1)[0][0]
        offset_source = f"iPhone/GoPro clip in this batch ({fmt_offset(batch_offset)})"
    else:
        batch_offset = machine_utc_offset_minutes()
        offset_source = f"this machine's local timezone ({fmt_offset(batch_offset)})"
        fallback_used = bool(needs_fix)

    if needs_fix:
        print(f"\n  Normalizing {len(needs_fix)} naive-local clip(s) to UTC "
              f"— offset from {offset_source}.")
        if fallback_used:
            print("  [warn] No iPhone/GoPro clip in this batch to read the true offset from.")
            print("         Falling back to this machine's current timezone. If the footage")
            print("         was shot in a different zone, re-run with --local-offset ±HH:MM.")

    fix_tz = timezone(timedelta(minutes=batch_offset))
    for e in needs_fix:
        naive = datetime.fromisoformat(e["_naive_local_iso"])
        utc = naive.replace(tzinfo=fix_tz).astimezone(timezone.utc)
        e["creation_time"] = utc.isoformat().replace("+00:00", "Z")

    # Local (wall-clock) display value for every timed clip, using the batch offset.
    for e in entries:
        if e["creation_time"]:
            utc = datetime.fromisoformat(e["creation_time"].rstrip("Z")).replace(tzinfo=timezone.utc)
            e["creation_time_local"] = utc.astimezone(fix_tz).isoformat()
        else:
            e["creation_time_local"] = None

    corrected_files = [e["filename"] for e in needs_fix]

    # Drop internal helper keys before the entries get serialized to the manifest.
    for e in entries:
        for k in ("_records_utc", "_naive_local_iso", "_apple_offset_min"):
            e.pop(k, None)

    # Sort: timed clips chronologically (by true UTC), untimed by filename at the end.
    def sort_key(e):
        ct = e["creation_time"] or ""
        return (0 if ct else 1, ct, e["filename"])

    entries.sort(key=sort_key)

    tz_summary = {
        "batch_offset_min": batch_offset,
        "offset_label": fmt_offset(batch_offset),
        "offset_source": offset_source,
        "corrected_files": corrected_files,
        "fallback_used": fallback_used,
    }
    return entries, tz_summary


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def write_manifest(entries: list[dict], input_dir: str, output_dir: str) -> str:
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "input_dir": os.path.abspath(input_dir),
        "file_count": len(entries),
        "files": entries,
    }
    path = os.path.join(output_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return path


def fmt_duration(duration_s: float | None) -> str:
    if duration_s is None:
        return "—"
    s = int(duration_s)
    if s < 60:
        return f"{duration_s:.1f}s"
    m, sec = divmod(s, 60)
    return f"{m}m{sec:02d}s"


def fmt_datetime(iso: str | None) -> str:
    if not iso:
        return "NO TIMESTAMP     "
    # Accept UTC ("…T09:32:11Z") or local ("…T14:47:50-04:00") and render the
    # wall clock only: "2026-05-25 14:47:50"
    date_part, _, time_part = iso.partition("T")
    time_part = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", time_part.split(".")[0])
    return f"{date_part} {time_part}"


def disp_time(entry: dict) -> str:
    """Human-readable wall-clock time for an entry (prefers the local value)."""
    return fmt_datetime(entry.get("creation_time_local") or entry.get("creation_time"))


def write_clips_ordered(entries: list[dict], output_dir: str) -> str:
    lines = []
    header = (
        f"{'#':<4}  "
        f"{'DATE/TIME (local)':<20}  "
        f"{'SOURCE':<7}  "
        f"{'TYPE':<6}  "
        f"{'ORIENT':<10}  "
        f"{'DUR':<8}  "
        f"FILENAME"
    )
    separator = "-" * (len(header) + 10)
    lines.append(separator)
    lines.append(header)
    lines.append(separator)

    for i, e in enumerate(entries, 1):
        line = (
            f"{i:<4}  "
            f"{disp_time(e):<20}  "
            f"{e['source']:<7}  "
            f"{e['media_type']:<6}  "
            f"{e['orientation']:<10}  "
            f"{fmt_duration(e['duration_s']):<8}  "
            f"{e['filename']}"
        )
        lines.append(line)

    lines.append(separator)
    lines.append(f"Total: {len(entries)} files")

    path = os.path.join(output_dir, "clips_ordered.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def seconds_to_hms(total_s: float) -> str:
    total_s = int(total_s)
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def find_timeline_gaps(entries: list[dict], threshold_s: int = GAP_THRESHOLD_S) -> list[dict]:
    """
    Find gaps larger than threshold_s between consecutive creation_times.
    Only considers entries that have a creation_time.
    """
    timed = [e for e in entries if e["creation_time"]]
    gaps = []
    for i in range(1, len(timed)):
        prev = timed[i - 1]
        curr = timed[i]
        try:
            t0 = datetime.fromisoformat(prev["creation_time"].rstrip("Z"))
            t1 = datetime.fromisoformat(curr["creation_time"].rstrip("Z"))
            # Add duration of previous clip so gap starts at clip end
            dur = prev["duration_s"] or 0
            gap_s = (t1 - t0).total_seconds() - dur
            if gap_s > threshold_s:
                gaps.append({
                    "from": prev.get("creation_time_local") or prev["creation_time"],
                    "to": curr.get("creation_time_local") or curr["creation_time"],
                    "gap_s": gap_s,
                    "from_file": prev["filename"],
                    "to_file": curr["filename"],
                })
        except Exception:
            pass
    return gaps


def write_ingest_report(entries: list[dict], input_dir: str, output_dir: str,
                        tz_summary: dict | None = None) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    tz_summary = tz_summary or {}

    # --- Counts ---
    videos = [e for e in entries if e["media_type"] == "video"]
    images = [e for e in entries if e["media_type"] == "image"]
    gopro_v = [e for e in videos if e["source"] == "gopro"]
    iphone_v = [e for e in videos if e["source"] == "iphone"]
    unknown_v = [e for e in videos if e["source"] == "unknown"]
    iphone_i = [e for e in images if e["source"] == "iphone"]
    other_i = [e for e in images if e["source"] != "iphone"]

    total_dur_s = sum(e["duration_s"] for e in videos if e["duration_s"])
    gopro_dur_s = sum(e["duration_s"] for e in gopro_v if e["duration_s"])
    iphone_dur_s = sum(e["duration_s"] for e in iphone_v if e["duration_s"])

    # --- Flags ---
    hdr_clips = [e for e in videos if e["is_hdr"]]
    vfr_clips = [e for e in videos if e["is_vfr"]]
    vertical_clips = [e for e in videos if e["orientation"] == "vertical"]
    no_timestamp = [e for e in entries if not e["creation_time"]]

    # --- Date range (wall-clock / local) ---
    timed = [e for e in entries if e["creation_time"]]
    date_range = "unknown"
    if timed:
        date_range = f"{disp_time(timed[0])}  →  {disp_time(timed[-1])}"

    # --- Gaps ---
    gaps = find_timeline_gaps(entries)

    # --- Build markdown ---
    lines = []
    a = lines.append

    a(f"# Ingest Report")
    a(f"")
    a(f"**Generated:** {now_str}  ")
    a(f"**Input folder:** `{os.path.abspath(input_dir)}`  ")
    a(f"**Total files:** {len(entries)}  ")
    a(f"**Total video duration:** {seconds_to_hms(total_dur_s)}  ")
    a(f"**Date range:** {date_range}  _(local wall-clock times)_  ")
    if tz_summary.get("offset_label"):
        a(f"**Local offset:** {tz_summary['offset_label']} — from {tz_summary.get('offset_source', 'unknown')}  ")
    a(f"")
    a(f"---")
    a(f"")
    a(f"## Breakdown")
    a(f"")
    a(f"| Category | Count | Duration |")
    a(f"|---|---|---|")
    a(f"| GoPro video | {len(gopro_v)} | {seconds_to_hms(gopro_dur_s)} |")
    a(f"| iPhone video | {len(iphone_v)} | {seconds_to_hms(iphone_dur_s)} |")
    if unknown_v:
        unk_dur = sum(e["duration_s"] for e in unknown_v if e["duration_s"])
        a(f"| Unknown video | {len(unknown_v)} | {seconds_to_hms(unk_dur)} |")
    a(f"| iPhone image | {len(iphone_i)} | — |")
    if other_i:
        a(f"| Other image | {len(other_i)} | — |")
    a(f"| **Total** | **{len(entries)}** | **{seconds_to_hms(total_dur_s)}** |")
    a(f"")
    a(f"---")
    a(f"")
    a(f"## Flags — items needing pipeline attention")
    a(f"")

    flags_found = False

    corrected = tz_summary.get("corrected_files") or []
    if corrected:
        flags_found = True
        a(f"- **{len(corrected)} clip(s) had naive local timestamps normalized to UTC** "
          f"→ offset {tz_summary.get('offset_label', '?')} applied "
          f"(from {tz_summary.get('offset_source', 'unknown')})")
        for fn in corrected:
            a(f"  - `{fn}`")
        if tz_summary.get("fallback_used"):
            a(f"  - ⚠️  No iPhone/GoPro clip was present to confirm the offset — "
              f"used this machine's timezone. Re-run with `--local-offset ±HH:MM` if wrong.")

    if hdr_clips:
        flags_found = True
        a(f"- **{len(hdr_clips)} HDR clip(s)** → will be tone-mapped SDR in transcode step")
        for e in hdr_clips:
            a(f"  - `{e['filename']}`")

    if vfr_clips:
        flags_found = True
        a(f"- **{len(vfr_clips)} VFR clip(s)** → will be converted to CFR 29.97")
        for e in vfr_clips:
            a(f"  - `{e['filename']}`")

    if vertical_clips:
        flags_found = True
        a(f"- **{len(vertical_clips)} vertical clip(s)** → will get blurred 16:9 letterbox")
        for e in vertical_clips:
            a(f"  - `{e['filename']}`")

    if no_timestamp:
        flags_found = True
        a(f"- ⚠️  **{len(no_timestamp)} file(s) have no creation_time metadata** → will sort to end of timeline")
        for e in no_timestamp:
            a(f"  - `{e['filename']}`")

    if not flags_found:
        a(f"- ✅  No flags — all files look clean")

    a(f"")
    a(f"---")
    a(f"")
    a(f"## Timeline gaps  (> {GAP_THRESHOLD_S // 60} min between clips)")
    a(f"")

    if gaps:
        for g in gaps:
            gap_hms = seconds_to_hms(g["gap_s"])
            from_dt = fmt_datetime(g["from"])
            to_dt = fmt_datetime(g["to"])
            a(f"- **{gap_hms} gap**  `{from_dt}` → `{to_dt}`")
            a(f"  - After: `{g['from_file']}`")
            a(f"  - Before: `{g['to_file']}`")
    else:
        a(f"- No significant gaps found")

    a(f"")
    a(f"---")
    a(f"")
    a(f"## All files in chronological order")
    a(f"")
    a(f"| # | Date/Time (local) | Source | Type | Orient | Duration | Filename |")
    a(f"|---|---|---|---|---|---|---|")
    for i, e in enumerate(entries, 1):
        a(
            f"| {i} "
            f"| {disp_time(e)} "
            f"| {e['source']} "
            f"| {e['media_type']} "
            f"| {e['orientation']} "
            f"| {fmt_duration(e['duration_s'])} "
            f"| `{e['filename']}` |"
        )

    path = os.path.join(output_dir, "ingest_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prompt_for_audio_pairs(entries: list[dict]) -> dict[str, str]:
    """
    Show video clips to the user and ask if any have a separate external audio
    file to sync. Returns {abs_video_path: abs_audio_path}.
    """
    videos = [e for e in entries if e["media_type"] == "video"]
    if not videos:
        return {}

    print("  Video clips found:")
    for i, e in enumerate(videos, 1):
        print(f"    {i:>3}.  {e['filename']}")
    print()

    ans = input("Any of these clips have a separate external audio file? [y/N]: ").strip().lower()
    if ans != "y":
        return {}

    pairs: dict[str, str] = {}
    while True:
        raw_vid = input("\n  Video clip (drag or paste path): ").strip()
        if not raw_vid:
            break
        vid_path = os.path.abspath(sanitize_path(raw_vid))
        if not os.path.isfile(vid_path):
            print(f"  [warn] Not found: {vid_path} — skipping")
        else:
            raw_aud = input("  External audio file (drag or paste path): ").strip()
            aud_path = os.path.abspath(sanitize_path(raw_aud))
            if not os.path.isfile(aud_path):
                print(f"  [warn] Not found: {aud_path} — skipping this pair")
            else:
                pairs[vid_path] = aud_path
                print(f"  Paired: {os.path.basename(vid_path)}  ←audio—  {os.path.basename(aud_path)}")

        again = input("  Another pair? [y/N]: ").strip().lower()
        if again != "y":
            break

    return pairs


def prompt_for_camera_syncs(entries: list[dict]) -> int:
    """
    Pair two video clips that recorded the same take from different cameras
    (e.g. an OBS screen capture + a DSLR) so they can be synced onto separate
    Blender VSE tracks.

    Both clips must already be scanned project files — unlike external audio,
    both are preserved as separate videos. Stamps a shared `sync_group` id on
    both entries and `sync_base = True` on the base (the camera whose audio
    anchors the timeline, typically OBS). transcode.py then measures the audio
    offset and import-vse.py overlaps them on different channels.

    Returns the number of sync groups created.
    """
    videos = [e for e in entries if e["media_type"] == "video"]
    if len(videos) < 2:
        return 0

    print("  Video clips found:")
    for i, e in enumerate(videos, 1):
        print(f"    {i:>3}.  {e['filename']}")
    print()

    ans = input("Any clips from two cameras recording the same take, to sync "
                "onto separate tracks? [y/N]: ").strip().lower()
    if ans != "y":
        return 0

    by_path = {e["path"]: e for e in entries}
    groups = 0
    while True:
        raw_base = input("\n  Base clip — its audio anchors the sync, e.g. OBS "
                         "(drag or paste path): ").strip()
        if not raw_base:
            break
        base_path = os.path.abspath(sanitize_path(raw_base))
        base = by_path.get(base_path)
        if not base or base["media_type"] != "video":
            print(f"  [warn] Not a scanned video clip: {base_path} — skipping")
        else:
            raw_angle = input("  Second camera clip, e.g. DSLR (drag or paste path): ").strip()
            angle_path = os.path.abspath(sanitize_path(raw_angle))
            angle = by_path.get(angle_path)
            if angle_path == base_path:
                print("  [warn] Base and second clip are the same file — skipping")
            elif not angle or angle["media_type"] != "video":
                print(f"  [warn] Not a scanned video clip: {angle_path} — skipping")
            else:
                groups += 1
                gid = f"sync-{groups}"
                base["sync_group"]  = gid
                base["sync_base"]   = True
                angle["sync_group"] = gid
                print(f"  Synced: {os.path.basename(base_path)} (base) ⇄ "
                      f"{os.path.basename(angle_path)}  [{gid}]")

        again = input("  Another camera pair? [y/N]: ").strip().lower()
        if again != "y":
            break

    return groups


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


def parse_offset_arg(raw: str) -> int:
    """Parse a --local-offset value like '-04:00', '+0530', or '-4' into minutes."""
    m = re.fullmatch(r"\s*([+-])(\d{1,2})(?::?(\d{2}))?\s*", raw)
    if not m:
        raise argparse.ArgumentTypeError(
            f"invalid offset '{raw}' — expected form like -04:00, +0530, or -4"
        )
    sign = 1 if m.group(1) == "+" else -1
    return sign * (int(m.group(2)) * 60 + int(m.group(3) or 0))


def prompt_for_dir() -> str:
    """Interactively ask the user for a directory path, supporting drag-and-drop."""
    print()
    print("  Drag-and-drop your footage folder here, or paste/type the path:")
    print("  > ", end="", flush=True)
    raw = input()
    return sanitize_path(raw)


def main():
    parser = argparse.ArgumentParser(
        description="Ingest step: scan a footage folder and produce manifest + human-readable reports."
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        default=None,
        help="Path to the folder containing raw footage (videos and/or images). "
             "If omitted, you will be prompted to enter or drag-and-drop the path.",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Directory to write output files (default: <input_dir>/_ingest)",
    )
    parser.add_argument(
        "--local-offset",
        type=parse_offset_arg,
        default=None,
        metavar="±HH:MM",
        help="UTC offset of the footage's local time, e.g. -04:00. Used to normalize "
             "cameras that record naive local time with no timezone (Canon Vixia / 7D, "
             "iVue Rincon). Default: read the offset from an iPhone/GoPro clip in the "
             "batch, else fall back to this machine's timezone.",
    )
    args = parser.parse_args()

    # Resolve input directory — prompt if not provided as an argument
    if args.input_dir:
        raw_dir = sanitize_path(args.input_dir)
    else:
        raw_dir = prompt_for_dir()

    input_dir = os.path.abspath(os.path.expanduser(raw_dir))
    if not os.path.isdir(input_dir):
        print(f"\nError: '{input_dir}' is not a directory.", file=sys.stderr)
        print("Tip: try drag-and-dropping the folder directly into the terminal.", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output or os.path.join(input_dir, "_ingest")
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  INGEST — Video Intake Pipeline")
    print(f"{'='*60}")
    print(f"  Input : {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"{'='*60}\n")

    # Scan
    print("Scanning files …")
    entries, tz_summary = scan_directory(
        input_dir, local_offset_min=args.local_offset, output_dir=output_dir
    )
    print(f"\nFound {len(entries)} media file(s).\n")

    if not entries:
        print("Nothing to do — no media files found.")
        sys.exit(0)

    # Optional: pair video clips with external audio files.
    # The pairing is recorded in the manifest; transcode.py does the actual sync.
    audio_pairs = prompt_for_audio_pairs(entries)
    if audio_pairs:
        for entry in entries:
            if entry["path"] in audio_pairs:
                entry["external_audio"] = audio_pairs[entry["path"]]
        print()

    # Optional: sync two camera angles of the same take onto separate tracks.
    # Stamps sync_group/sync_base on entries; transcode.py measures the offset
    # and import-vse.py overlaps the pair on different channels.
    if prompt_for_camera_syncs(entries):
        print()

    # Write outputs
    print("Writing outputs …")
    p1 = write_manifest(entries, input_dir, output_dir)
    print(f"  ✓  {os.path.basename(p1)}")

    p2 = write_clips_ordered(entries, output_dir)
    print(f"  ✓  {os.path.basename(p2)}")

    p3 = write_ingest_report(entries, input_dir, output_dir, tz_summary)
    print(f"  ✓  {os.path.basename(p3)}")

    print(f"\nDone. All outputs in: {output_dir}\n")


if __name__ == "__main__":
    main()
