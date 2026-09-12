#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix-camera-clock.py — repair timestamps from a camera whose clock was wrong.

A camera that was never set (or was reset by a dead battery) stamps every clip
with a bogus date. Downstream that is not cosmetic: 1-ingest.py trusts GoPro,
iPhone and DJI clips as true UTC and never corrects them, so the whole card
sorts years away from everything else and stacks at the front of the timeline.

This shifts the embedded timestamps by a fixed amount, so the clips keep their
true *relative* spacing and land in the right place in the batch.

Give it an anchor: one clip, plus when that clip was really recorded.

    # see what would change — writes nothing
    ./fix-camera-clock.py /path/to/100GOPRO \
        --anchor GX010346.MP4 --actual "2026-09-05 10:05"

    # do it
    ./fix-camera-clock.py /path/to/100GOPRO \
        --anchor GX010346.MP4 --actual "2026-09-05 10:05" --apply

Dry-run is the default. Nothing is written without --apply.

── On timezones ──────────────────────────────────────────────────────────────
MP4 stores creation time as UTC, and Finder renders it in *your* timezone using
the rules for that date — so a clip whose fake date is in January is displayed
with winter-time rules even though it was really shot in summer. Reading the
anchor time off Finder can therefore be an hour out.

--actual is interpreted as local wall-clock in --tz (default: this machine's
current offset), which is what you actually remember. The offset is computed in
UTC, so the shift is correct regardless of how anything is displayed.

── Undo ──────────────────────────────────────────────────────────────────────
The shift is exactly invertible. To undo, re-run with the negated offset:

    ./fix-camera-clock.py <dir> --offset "-0:0:3897 20:20:0" --apply
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Tags that must move together. -AllDates is NOT enough on an MP4: it covers
# CreateDate/ModifyDate only and leaves every Track*/Media* date behind, which
# leaves the file internally inconsistent.
DATE_TAGS = [
    "QuickTime:CreateDate", "QuickTime:ModifyDate",
    "QuickTime:TrackCreateDate", "QuickTime:TrackModifyDate",
    "QuickTime:MediaCreateDate", "QuickTime:MediaModifyDate",
]

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}

USE_COLOR = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if USE_COLOR else s
def ok(s):   print(_c("0;32", f"✔ {s}"))
def warn(s): print(_c("1;33", f"⚠ {s}"))
def die(s):  print(_c("0;31", f"✖ {s}"), file=sys.stderr); sys.exit(1)
def head(s): print(_c("1;36", f"\n── {s} ──\n"))


# ---------------------------------------------------------------------------
# exiftool / ffprobe
# ---------------------------------------------------------------------------

def require(tool):
    from shutil import which
    if not which(tool):
        die(f"{tool} not found on PATH.")
    return tool


def read_dates(paths):
    """{abspath: naive datetime} of QuickTime:CreateDate, in one exiftool call.

    QuickTimeUTC=0 keeps the stored value raw — no timezone interpretation —
    so the arithmetic below is a pure offset on the number in the file.
    """
    if not paths:
        return {}
    r = subprocess.run(
        ["exiftool", "-api", "QuickTimeUTC=0", "-QuickTime:CreateDate", "-j", "-@", "-"],
        input="\n".join(paths), capture_output=True, text=True,
    )
    out = {}
    for item in json.loads(r.stdout or "[]"):
        raw = item.get("CreateDate")
        src = item.get("SourceFile")
        if not raw or not src:
            continue
        try:
            out[os.path.abspath(src)] = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            pass
    return out


def probe_streams(path):
    """[(index, codec_tag_string, handler_name)] — the structural fingerprint."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=index,codec_tag_string:stream_tags=handler_name",
         "-of", "json", path],
        capture_output=True, text=True,
    )
    try:
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        return []
    return [(s.get("index"), s.get("codec_tag_string"),
             (s.get("tags") or {}).get("handler_name")) for s in streams]


def telemetry_digest(path, streams):
    """md5 of the GoPro GPMF telemetry payload, or None if the clip has none.

    This is the part most at risk from a container rewrite, and the only way to
    know it survived byte-for-byte is to hash it.
    """
    idx = next((i for i, tag, _ in streams if tag == "gpmd"), None)
    if idx is None:
        return None
    p = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{idx}", "-c", "copy", "-f", "data", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    h = hashlib.md5()
    for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
        h.update(chunk)
    p.wait()
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Offset handling
# ---------------------------------------------------------------------------

SHIFT_RE = re.compile(r"^\s*(-)?(\d+):(\d+):(\d+)\s+(\d+):(\d+):(\d+)\s*$")


def shift_to_timedelta(s):
    """Parse exiftool's 'Y:M:D h:m:s' shift string. Only D h:m:s is supported —
    years and months are not fixed-length, so they cannot express an exact
    offset and are rejected rather than silently approximated."""
    m = SHIFT_RE.match(s)
    if not m:
        die(f"Bad offset {s!r}. Expected 'Y:M:D h:m:s', e.g. '0:0:3897 20:20:0'.")
    neg, y, mo, d, hh, mm, ss = m.groups()
    if int(y) or int(mo):
        die("Years/months in an offset are ambiguous — express it in days, e.g. '0:0:3897 20:20:0'.")
    td = timedelta(days=int(d), hours=int(hh), minutes=int(mm), seconds=int(ss))
    return -td if neg else td


def timedelta_to_shift(td):
    neg = td < timedelta(0)
    td = abs(td)
    h, rem = divmod(td.seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{'-' if neg else ''}0:0:{td.days} {h}:{m}:{s}"


def shift_operand(td):
    """exiftool's shift operator and magnitude for a delta.

    A negative value passed to '+=' is silently ignored — exiftool expects the
    '-=' operator with a positive magnitude — so direction must be carried by
    the operator, never by a minus sign inside the value.
    """
    op = "+=" if td >= timedelta(0) else "-="
    return op, timedelta_to_shift(abs(td))


def parse_tz(raw):
    m = re.fullmatch(r"\s*([+-])(\d{1,2}):?(\d{2})\s*", raw)
    if not m:
        die(f"Bad --tz {raw!r}. Expected ±HH:MM, e.g. -04:00.")
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def parse_datetime(raw, what):
    """Accept 'YYYY-MM-DD HH:MM[:SS]' or a bare 'YYYY-MM-DD' (= midnight)."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y:%m:%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    die(f"Bad {what} {raw!r}. Expected 'YYYY-MM-DD HH:MM[:SS]' or 'YYYY-MM-DD'.")


# ---------------------------------------------------------------------------

def gather(root, exts):
    found = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for f in sorted(fn):
            if f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() in exts:
                found.append(os.path.abspath(os.path.join(dp, f)))
    return sorted(found)


def looks_synced(path):
    p = os.path.abspath(path)
    return any(k in p for k in ("/CloudStorage/", "/Dropbox/", "/Google Drive/",
                               "/OneDrive", "/iCloud Drive/"))


def main():
    ap = argparse.ArgumentParser(
        description="Shift embedded timestamps of clips shot with a wrong camera clock.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("── On timezones")[0].split("Give it an anchor:")[1],
    )
    ap.add_argument("directory", help="Folder of clips (searched recursively).")
    ap.add_argument("--anchor", help="Filename of the reference clip, e.g. GX010346.MP4")
    ap.add_argument("--actual", help="When the anchor was REALLY recorded: 'YYYY-MM-DD HH:MM[:SS]' local time.")
    ap.add_argument("--offset", help="Use an explicit shift instead of an anchor, e.g. '0:0:3897 20:20:0'.")
    ap.add_argument("--tz", default=None, metavar="±HH:MM",
                    help="Timezone --actual is expressed in. Default: this machine's current offset.")
    ap.add_argument("--before", metavar="YYYY-MM-DD", default=None,
                    help="Only shift clips whose stored date is before this. "
                         "Default: one year after the anchor's stored date, which "
                         "selects the bad-clock clips and leaves correctly-dated ones alone.")
    ap.add_argument("--ext", action="append", default=None,
                    help="File extension to include (repeatable). Default: mp4, mov, m4v.")
    ap.add_argument("--apply", action="store_true", help="Actually write. Default is a dry run.")
    ap.add_argument("--allow-synced", action="store_true",
                    help="Permit running inside a cloud-synced folder (Synology Drive, Dropbox, …). "
                         "Refused by default: rewriting there re-uploads every file and edits the master copy.")
    ap.add_argument("--skip-telemetry-check", action="store_true",
                    help="Skip hashing the GPMF telemetry before/after. Faster, but drops the "
                         "strongest evidence that the rewrite was lossless.")
    args = ap.parse_args()

    require("exiftool"); require("ffprobe")
    exts = {e if e.startswith(".") else f".{e}" for e in (args.ext or [])} or VIDEO_EXTS
    exts = {e.lower() for e in exts}

    root = os.path.abspath(os.path.expanduser(args.directory))
    if not os.path.isdir(root):
        die(f"Not a directory: {root}")

    if looks_synced(root) and args.apply and not args.allow_synced:
        die(f"{root}\n  is inside a cloud-synced folder. Rewriting there edits the master copy and "
            f"re-uploads every file.\n  Work on a local copy, or pass --allow-synced if you mean it.")

    files = gather(root, exts)
    if not files:
        die(f"No {'/'.join(sorted(exts))} files under {root}")

    dates = read_dates(files)
    undated = [f for f in files if f not in dates]

    # ── Work out the offset ──────────────────────────────────────────────────
    if args.offset:
        delta = shift_to_timedelta(args.offset)
        anchor_path = None
        basis = f"explicit --offset {args.offset}"
    else:
        if not (args.anchor and args.actual):
            die("Give either --offset, or both --anchor and --actual.")
        matches = [f for f in files if os.path.basename(f).lower() == args.anchor.lower()]
        if not matches:
            die(f"Anchor {args.anchor!r} not found under {root}")
        if len(matches) > 1:
            die(f"Anchor {args.anchor!r} is ambiguous — {len(matches)} files share that name.")
        anchor_path = matches[0]
        if anchor_path not in dates:
            die(f"Anchor {args.anchor} has no readable QuickTime:CreateDate.")

        tz = parse_tz(args.tz) if args.tz else datetime.now(timezone.utc).astimezone().tzinfo
        stored_utc = dates[anchor_path].replace(tzinfo=timezone.utc)
        true_utc = parse_datetime(args.actual, "--actual").replace(tzinfo=tz).astimezone(timezone.utc)
        delta = true_utc - stored_utc
        basis = f"anchor {os.path.basename(anchor_path)}"

    if delta == timedelta(0):
        die("Offset is zero — nothing to do.")

    # ── Decide which clips are in scope ──────────────────────────────────────
    if args.before:
        cutoff = parse_datetime(args.before, "--before")
    elif anchor_path:
        a = dates[anchor_path]
        cutoff = a.replace(year=a.year + 1)
    else:
        cutoff = None

    targets = [f for f in files if f in dates and (cutoff is None or dates[f] < cutoff)]
    excluded = [f for f in files if f in dates and f not in targets]

    head("Plan")
    print(f"  Folder     : {root}")
    print(f"  Offset     : {timedelta_to_shift(delta)}   ({delta.days} days, {delta.seconds // 3600}h "
          f"{(delta.seconds % 3600) // 60}m {delta.seconds % 60}s)  — from {basis}")
    if cutoff:
        print(f"  In scope   : stored date before {cutoff:%Y-%m-%d}  "
              f"(clips already dated later are left alone)")
    print(f"  Files      : {len(targets)} to shift, {len(excluded)} skipped, {len(undated)} unreadable")
    print()

    # The stored value is UTC; the wall-clock column is what you actually
    # remember shooting, and is the one worth eyeballing for sanity.
    disp_tz = parse_tz(args.tz) if args.tz else datetime.now(timezone.utc).astimezone().tzinfo
    def local(dt):
        return dt.replace(tzinfo=timezone.utc).astimezone(disp_tz)

    w = max((len(os.path.basename(f)) for f in files), default=12)
    print(f"  {'file'.ljust(w)}  {'stored (UTC)':19}  {'corrected (UTC)':19}  {'corrected (local)':22}")
    print(f"  {'-' * w}  {'-' * 19}  {'-' * 19}  {'-' * 22}")
    for f in files:
        name = os.path.basename(f).ljust(w)
        if f in undated:
            print(f"  {name}  {'— unreadable —':19}  {'SKIP':19}  {'':22}")
        elif f in targets:
            new = dates[f] + delta
            print(f"  {name}  {dates[f]:%Y-%m-%d %H:%M:%S}  {new:%Y-%m-%d %H:%M:%S}  "
                  f"{local(new):%Y-%m-%d %I:%M:%S %p}")
        else:
            print(f"  {name}  {dates[f]:%Y-%m-%d %H:%M:%S}  {'unchanged':19}  "
                  f"{local(dates[f]):%Y-%m-%d %I:%M:%S %p}")
    print()

    if undated:
        warn(f"{len(undated)} file(s) have no readable timestamp and will not be touched:")
        for f in undated:
            print(f"    {os.path.basename(f)}")
        print()

    if not args.apply:
        print(_c("1;33", "  DRY RUN — nothing written. Re-run with --apply to make these changes."))
        return

    # ── Baseline, for verification ───────────────────────────────────────────
    baseline = {}
    head("Reading baseline")
    for i, f in enumerate(targets, 1):
        streams = probe_streams(f)
        digest = None if args.skip_telemetry_check else telemetry_digest(f, streams)
        baseline[f] = {"size": os.path.getsize(f), "streams": streams, "gpmd": digest}
        print(f"  [{i}/{len(targets)}] {os.path.basename(f)}"
              + (f"  telemetry {digest[:8]}…" if digest else ""))

    # ── Apply ────────────────────────────────────────────────────────────────
    head("Shifting timestamps")
    op, magnitude = shift_operand(delta)
    cmd = ["exiftool", "-api", "QuickTimeUTC=0", "-overwrite_original", "-q", "-@", "-"]
    for tag in DATE_TAGS:
        cmd[4:4] = [f"-{tag}{op}{magnitude}"]
    r = subprocess.run(cmd, input="\n".join(targets), capture_output=True, text=True)
    if r.returncode != 0:
        warn(f"exiftool exited {r.returncode}")
    if r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            warn(f"  {line}")

    # ── Verify ───────────────────────────────────────────────────────────────
    head("Verifying")
    after = read_dates(targets)
    problems = []
    for i, f in enumerate(targets, 1):
        name = os.path.basename(f)
        want = dates[f] + delta
        got = after.get(f)
        issues = []

        if got != want:
            issues.append(f"timestamp is {got}, expected {want}")
        if os.path.getsize(f) != baseline[f]["size"]:
            issues.append("file size changed")
        streams = probe_streams(f)
        if streams != baseline[f]["streams"]:
            issues.append("stream layout changed")
        if not streams:
            issues.append("file no longer parses")
        if baseline[f]["gpmd"] is not None:
            if telemetry_digest(f, streams) != baseline[f]["gpmd"]:
                issues.append("GPMF telemetry changed")

        if issues:
            problems.append((name, issues))
            print(_c("0;31", f"  [{i}/{len(targets)}] ✖ {name}: {'; '.join(issues)}"))
        else:
            print(f"  [{i}/{len(targets)}] ✔ {name}  →  {got:%Y-%m-%d %H:%M:%S}")

    print()
    if problems:
        warn(f"{len(problems)} file(s) failed verification:")
        for name, issues in problems:
            print(f"    {name}: {'; '.join(issues)}")
        print(f"\n  To undo: re-run with --offset \"{timedelta_to_shift(-delta)}\" --apply")
        sys.exit(1)

    ok(f"{len(targets)} file(s) shifted and verified — timestamps, size, stream layout"
       + ("" if args.skip_telemetry_check else ", and telemetry") + " all check out.")
    print(f"  To undo: re-run with --offset \"{timedelta_to_shift(-delta)}\" --apply")


if __name__ == "__main__":
    main()
