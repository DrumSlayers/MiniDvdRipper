"""Human descriptions for ripped clips.

You watch a folder's clips and jot what each one is (an anniversary, a football
match, …). This stores that in a per-folder `descriptions.tsv` you fill in a
spreadsheet or text editor, then applies it to BOTH:

  * the filename — `01__2005-08-21__Anniversaire-Mamie-70ans__12m30s.mkv` — which
    is what Google Photos / gofile / a file browser actually show; and
  * the file metadata — Matroska/MP4 `title`, `description`, `date`,
    `creation_time` — which is what Plex / VLC / Jellyfin show, and what drives a
    photo library's timeline.

The duration is filled automatically (ffprobe); the date defaults to the date we
already have and is yours to correct (handy for undated `_recovered` discs). The
whole thing is re-runnable: run `apply` again after a re-encode and it re-stamps
the new files. Metadata is written with a stream-copy remux — no re-encoding.
"""
from __future__ import annotations

import csv
import glob
import os
import re
import shutil
from datetime import datetime

from . import contact, tools

TSV = "descriptions.tsv"
HEADER = ["session", "date", "duration", "description"]
_VIDEO_GLOBS = ("*.mkv", "*.mp4", "*.MKV", "*.MP4")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TIME_RE = re.compile(r"_(\d{2})h(\d{2})")
_SESSION_RE = re.compile(r"^(\d+)__")


# ---- small pure helpers --------------------------------------------------
def _clips(video_dir: str) -> list[str]:
    out: list[str] = []
    for g in _VIDEO_GLOBS:
        out += glob.glob(os.path.join(video_dir, g))
    return sorted(set(out))


def _session(name: str) -> str | None:
    m = _SESSION_RE.match(os.path.basename(name))
    return m.group(1) if m else None


def _date_in_name(name: str) -> str | None:
    m = _DATE_RE.search(os.path.basename(name))
    return m.group(1) if m else None


def _time_in_name(name: str) -> tuple[int, int] | None:
    m = _TIME_RE.search(os.path.basename(name))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _dur_tag(secs: float) -> str:
    s = int(secs)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s"


def _slug(text: str) -> str:
    """Filename-safe, human-readable slug. Keeps letters (incl. accents) and
    digits, spaces/underscores become dashes, punctuation is dropped."""
    s = re.sub(r"[\s_]+", "-", text.strip())
    s = re.sub(r"[^\w\-]", "", s, flags=re.UNICODE)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


# ---- ffprobe / ffmpeg side-effecting helpers -----------------------------
def _probe_creation(path: str) -> datetime | None:
    out = tools.capture(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
         "-of", "default=nw=1:nk=1", path], check=False).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(out, fmt)
        except ValueError:
            continue
    return None


def _event_datetime(clip: str, date_str: str) -> datetime | None:
    """Date from the sidecar (date-only) + time of day from the filename, else the
    file's existing creation_time, else noon."""
    try:
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    except ValueError:
        return None
    t = _time_in_name(clip)
    if t:
        return d.replace(hour=t[0], minute=t[1])
    c = _probe_creation(clip)
    if c:
        return d.replace(hour=c.hour, minute=c.minute, second=c.second)
    return d.replace(hour=12)


def _embed(path: str, title: str, desc: str, dt: datetime | None) -> bool:
    """Write title/description/date into the container via a stream-copy remux
    (no re-encode). Replaces the file in place on success."""
    ext = os.path.splitext(path)[1] or ".mkv"
    tmp = path + ".desc.tmp" + ext
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
           "-map", "0", "-c", "copy",
           "-metadata", f"title={title}",
           "-metadata", f"description={desc}",
           "-metadata", f"comment={desc}"]
    if dt:
        cmd += ["-metadata", f"date={dt.strftime('%Y-%m-%d')}",
                "-metadata", f"creation_time={dt.strftime('%Y-%m-%dT%H:%M:%S')}"]
    cmd += [tmp]
    res = tools.run(cmd, check=False)
    if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, path)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


# ---- TSV read/write ------------------------------------------------------
def _read_tsv(path: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            s = (row.get("session") or "").strip()
            if s:
                rows[s] = row
    return rows


def _write_tsv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, delimiter="\t")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HEADER})


# ---- public API ----------------------------------------------------------
def scaffold_folder(folder: str) -> str | None:
    """Create/refresh descriptions.tsv for one disc folder, preserving any
    descriptions/dates already typed. Returns the TSV path, or None if no clips."""
    vdir = os.path.join(folder, "video")
    clips = _clips(vdir)
    if not clips:
        return None
    tsv = os.path.join(folder, TSV)
    prev = _read_tsv(tsv)
    rows = []
    for c in clips:
        sess = _session(c) or os.path.splitext(os.path.basename(c))[0]
        old = prev.get(sess, {})
        date = (old.get("date") or "").strip() or _date_in_name(c) or \
            datetime.fromtimestamp(os.path.getmtime(c)).strftime("%Y-%m-%d")
        rows.append({"session": sess, "date": date,
                     "duration": _dur_tag(contact._probe_duration(c)),
                     "description": (old.get("description") or "").strip()})
    _write_tsv(tsv, rows)
    return tsv


def folder_status(folder: str) -> tuple[int, int]:
    """(clips with a non-empty description, total clips) for a disc folder.
    (0, 0) when there are no clips."""
    clips = _clips(os.path.join(folder, "video"))
    if not clips:
        return (0, 0)
    rows = _read_tsv(os.path.join(folder, TSV))
    filled = sum(1 for c in clips
                 if (rows.get(_session(c) or "", {}).get("description") or "").strip())
    return (filled, len(clips))


def apply_folder(folder: str, parent: str | None = None) -> list[tuple[str, str]]:
    """Apply descriptions.tsv: embed metadata, rename clips (+ their contact
    sheets), rebuild the folder montage. Returns [(old_name, new_name), …]."""
    vdir = os.path.join(folder, "video")
    cdir = os.path.join(folder, "contact")
    rows = _read_tsv(os.path.join(folder, TSV))
    if not rows:
        return []
    fname = os.path.basename(os.path.normpath(folder))
    renames: list[tuple[str, str]] = []
    for c in _clips(vdir):
        sess = _session(c)
        row = rows.get(sess or "")
        if not row:
            continue
        ext = os.path.splitext(c)[1]
        date = (row.get("date") or "").strip()
        desc = (row.get("description") or "").strip()
        dt = _event_datetime(c, date)
        dur = _dur_tag(contact._probe_duration(c))
        title = desc or f"Session {sess}"
        _embed(c, title, desc, dt)
        # build the new name: session [+ date] [+ description] + duration
        parts = [sess or "00"]
        if date:
            parts.append(date)
        if desc:
            parts.append(_slug(desc))
        parts.append(dur)
        new_name = "__".join(parts) + ext
        new_path = os.path.join(vdir, new_name)
        old_name = os.path.basename(c)
        if os.path.abspath(new_path) != os.path.abspath(c):
            os.rename(c, new_path)
            old_sheet = os.path.join(cdir, os.path.splitext(old_name)[0] + ".jpg")
            new_sheet = os.path.join(cdir, os.path.splitext(new_name)[0] + ".jpg")
            if os.path.exists(old_sheet):
                os.rename(old_sheet, new_sheet)
            renames.append((old_name, new_name))
        if dt:
            ts = dt.timestamp()
            try:
                os.utime(new_path, (ts, ts))
            except OSError:
                pass
    # refresh the overview montage (+ parent copy) to reflect the new names
    dest = os.path.join(folder, f"{fname}_thumbnails.jpg")
    if contact.folder_montage(fname, vdir, os.path.join(folder, "photos"), dest):
        if parent:
            try:
                shutil.copy2(dest, os.path.join(parent, os.path.basename(dest)))
            except OSError:
                pass
    return renames
