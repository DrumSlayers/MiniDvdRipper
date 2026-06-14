"""Copy the camcorder's still photos (DCIM/*.JPG) into the disc folder.

Handycams store stills exactly like a digital camera: DCIM/100MSDCF/DSC#####.JPG,
each already carrying EXIF (incl. DateTimeOriginal). We copy them verbatim
(shutil.copy2 keeps mtime), and if exiftool is present we make sure the file mtime
matches EXIF DateTimeOriginal so Synology Photos dates them correctly even if it
ignores the container date.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import tools

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
# Some Handycams also drop short MPEG clips next to stills:
CLIP_EXT = {".mpg", ".mpeg", ".mov", ".3gp", ".thm"}


@dataclass
class PhotoResult:
    copied: list = field(default_factory=list)   # destination paths
    count: int = 0


def find_dcim(root: str) -> Path | None:
    rp = Path(root)
    if (rp / "DCIM").is_dir():
        return rp / "DCIM"
    for p in rp.rglob("DCIM"):
        if p.is_dir():
            return p
    return None


def copy_photos(extracted_root: str, dest_dir: str,
                fallback_dt: datetime | None = None) -> PhotoResult:
    res = PhotoResult()
    dcim = find_dcim(extracted_root)
    if not dcim:
        return res
    os.makedirs(dest_dir, exist_ok=True)
    for src in sorted(dcim.rglob("*")):
        if not src.is_file():
            continue
        ext = src.suffix.lower()
        if ext not in PHOTO_EXT and ext not in CLIP_EXT:
            continue
        dst = Path(dest_dir) / src.name
        # avoid clobbering same-named files from different subfolders
        i = 1
        while dst.exists():
            dst = Path(dest_dir) / f"{src.stem}_{i}{src.suffix}"
            i += 1
        shutil.copy2(src, dst)            # preserves mtime (= capture date from ISO)
        if fallback_dt and _needs_mtime_fix(dst):
            ts = fallback_dt.timestamp()
            os.utime(dst, (ts, ts))
        res.copied.append(str(dst))
    res.count = len(res.copied)
    _sync_exif_to_mtime(res.copied)
    return res


def _needs_mtime_fix(path: Path) -> bool:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).year < 1990
    except OSError:
        return True


def _sync_exif_to_mtime(paths: list[str]) -> None:
    """If exiftool exists, set each file's mtime from its EXIF DateTimeOriginal.
    Falls back silently to the copy2 mtime when EXIF is absent."""
    if not paths or not tools.has("exiftool"):
        return
    # -overwrite_original not needed (we don't change pixels); just realign FileModifyDate.
    cmd = ["exiftool", "-q", "-m", "-overwrite_original",
           "-FileModifyDate<DateTimeOriginal", *paths]
    try:
        tools.run(cmd, check=False)
    except Exception:
        pass
