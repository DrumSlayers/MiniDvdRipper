"""Group the extracted DVD into 'titles' = recording sessions.

Sony Handycam writes one Video Title Set (VTS_NN) per recording session. The
playable video lives in the numbered parts VTS_NN_1.VOB, VTS_NN_2.VOB, ...
(VTS_NN_0.VOB, if present, is the menu/PGC and is skipped). One title -> one MKV.

The capture date of a session is the mtime of its first VOB part, which bsdtar
restored from the ISO directory record (the camcorder's recording timestamp).

VR-mode discs (unfinalized RW/RAM) have no VIDEO_TS; their content is in
DVD_RTAV/VR_MOVIE.VRO — handled as a fallback so those discs still rip.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import tools

_VTS = re.compile(r"^VTS_(\d{2})_(\d+)\.VOB$", re.IGNORECASE)


@dataclass
class Chapter:
    start: float       # seconds
    title: str = ""


@dataclass
class Title:
    number: int                       # session number (1-based)
    parts: list = field(default_factory=list)    # ordered VOB file paths
    datetime: datetime | None = None
    size_bytes: int = 0
    duration: float = 0.0             # seconds (filled after probe)
    chapters: list = field(default_factory=list)  # list[Chapter]
    kind: str = "vts"                 # "vts" or "vro"

    @property
    def date_tag(self) -> str:
        if self.datetime:
            return self.datetime.strftime("%Y-%m-%d_%Hh%M")
        return "undated"

    def out_name(self) -> str:
        return f"{self.number:02d}__{self.date_tag}.mkv"


def _mtime_dt(path: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return None


# A real Handycam recording is megabytes; tiny title sets are finalization
# placeholders (an empty VTS the camcorder writes when closing the disc).
MIN_TITLE_BYTES = 512 * 1024


def scan_titles(root: str, fallback_dt: datetime | None = None,
                min_bytes: int = MIN_TITLE_BYTES, on_skip=None) -> list[Title]:
    """Find real recording sessions under an extracted disc tree (VIDEO_TS, then
    VR). Placeholder title sets below `min_bytes` are dropped (reported via
    on_skip, never silently). Survivors are renumbered 1..N."""
    root_p = Path(root)
    video_ts = _find_dir(root_p, "VIDEO_TS")
    if video_ts:
        titles = _scan_vts(video_ts, fallback_dt)
    else:
        vros = sorted(list(root_p.rglob("*.VRO")) + list(root_p.rglob("*.vro")))
        titles = _scan_vro(vros, fallback_dt) if vros else []

    kept = []
    for t in titles:
        if t.size_bytes < min_bytes:
            if on_skip:
                on_skip(t)
            continue
        kept.append(t)
    for i, t in enumerate(kept, 1):     # contiguous output numbering
        t.number = i
    return kept


def _find_dir(root: Path, name: str) -> Path | None:
    if (root / name).is_dir():
        return root / name
    for p in root.rglob(name):
        if p.is_dir():
            return p
    return None


def _scan_vts(video_ts: Path, fallback_dt) -> list[Title]:
    groups: dict[int, list[tuple[int, Path]]] = {}
    for entry in os.scandir(video_ts):
        m = _VTS.match(entry.name)
        if not m:
            continue
        vts_no, part = int(m.group(1)), int(m.group(2))
        if part == 0:           # menu/PGC VOB, no actual program video
            continue
        groups.setdefault(vts_no, []).append((part, Path(entry.path)))

    titles: list[Title] = []
    for n, parts in sorted(groups.items()):
        ordered = [p for _, p in sorted(parts, key=lambda x: x[0])]
        size = sum(p.stat().st_size for p in ordered)
        dt = _mtime_dt(str(ordered[0])) or fallback_dt
        titles.append(Title(number=n, parts=[str(p) for p in ordered],
                            datetime=dt, size_bytes=size, kind="vts"))
    return titles


def _scan_vro(vros: list[Path], fallback_dt) -> list[Title]:
    titles = []
    for i, p in enumerate(vros, 1):
        titles.append(Title(number=i, parts=[str(p)],
                            datetime=_mtime_dt(str(p)) or fallback_dt,
                            size_bytes=p.stat().st_size, kind="vro"))
    return titles


# ---- optional chapter enrichment via lsdvd -------------------------------

def enrich_chapters(titles: list[Title], extracted_root: str) -> None:
    """Best-effort: pull per-title chapter offsets from lsdvd. Never raises."""
    if not tools.has("lsdvd"):
        return
    video_ts = _find_dir(Path(extracted_root), "VIDEO_TS")
    if not video_ts:
        return
    dvd_root = str(video_ts.parent)
    try:
        # -Oy => python-dict dump; -c => chapter info
        out = tools.capture(["lsdvd", "-Oy", "-c", dvd_root], check=False)
    except Exception:
        return
    data = _safe_lsdvd_eval(out)
    if not data:
        return
    by_no = {t.number: t for t in titles}
    for tr in data.get("track", []):
        ix = tr.get("ix")
        t = by_no.get(ix)
        if not t:
            continue
        acc, chaps = 0.0, []
        for ch in tr.get("chapter", []):
            chaps.append(Chapter(start=acc, title=f"Chapter {ch.get('ix', len(chaps)+1)}"))
            acc += float(ch.get("length", 0) or 0)
        if len(chaps) > 1:
            t.chapters = chaps
        if not t.duration:
            t.duration = float(tr.get("length", 0) or 0)


def _safe_lsdvd_eval(out: str):
    """lsdvd -Oy prints `lsdvd = {..}`. Parse the dict literal safely."""
    import ast
    i = out.find("{")
    j = out.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        return ast.literal_eval(out[i:j + 1])
    except Exception:
        return None
