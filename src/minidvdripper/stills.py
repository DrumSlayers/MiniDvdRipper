"""Carve still photos (JPEG) out of raw track bytes — for unfinalized discs.

On an unfinalized disc the DCIM filesystem entries don't exist, so we can't open
the photos by name. But the JPEG *bytes* were written to the disc when each photo
was taken, and they sit in a readable 'complete' track. So we do what PhotoRec
does: scan the raw bytes for JPEG signatures and cut each image out, filesystem or
not.

The one subtlety is finding the true end of a JPEG. A camera JPEG embeds a smaller
EXIF *thumbnail* that is itself a complete JPEG (its own FFD8…FFD9). Naively
searching for the next FFD9 would stop at the thumbnail and truncate the real
image. We avoid that by walking the JPEG's marker segments properly: segments with
a length field are skipped by that length (so we step over the APP1/EXIF block,
thumbnail and all), and only the entropy-coded scan after SOS is byte-scanned for
the real EOI.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime

from . import tools

SOI = b"\xff\xd8\xff"


def _jpeg_end(data: bytes, start: int) -> int:
    """Return the index just past the real EOI of the JPEG that begins at `start`
    (which points at FFD8), or -1 if it doesn't parse as one."""
    n = len(data)
    i = start + 2                      # past SOI
    while i + 1 < n:
        if data[i] != 0xFF:
            return -1                  # markers must be FF-aligned; not a clean JPEG
        m = data[i + 1]
        if m == 0xFF:                  # fill byte, advance one
            i += 1
            continue
        if m == 0xD9:                  # EOI
            return i + 2
        if m == 0xD8 or m == 0x01 or 0xD0 <= m <= 0xD7:   # SOI/TEM/RSTn: no length
            i += 2
            continue
        if i + 3 >= n:
            return -1
        seg_len = (data[i + 2] << 8) | data[i + 3]
        if seg_len < 2:
            return -1
        if m == 0xDA:                  # SOS: skip its header, then scan scan-data
            i += 2 + seg_len
            while i + 1 < n:
                if data[i] == 0xFF:
                    nb = data[i + 1]
                    if nb == 0x00 or (0xD0 <= nb <= 0xD7):   # byte-stuffing / restart
                        i += 2
                        continue
                    break              # a real marker ends the scan
                i += 1
            continue
        i += 2 + seg_len               # any other segment: skip by its length
    return -1


def find_jpegs(data: bytes, min_size: int = 4096,
               max_size: int = 24 * 1024 * 1024) -> list[bytes]:
    """All JPEGs embedded in `data`. Requires an APPn marker right after SOI to
    avoid matching the countless stray FFD8s inside MPEG-2 video."""
    out: list[bytes] = []
    i, n = 0, len(data)
    while True:
        s = data.find(SOI, i)
        if s < 0 or s + 4 >= n:
            break
        if not (0xE0 <= data[s + 3] <= 0xEF):     # FF D8 FF E0..EF  → APP0..APP15
            i = s + 3
            continue
        end = _jpeg_end(data, s)
        if end > 0 and min_size <= (end - s) <= max_size:
            out.append(data[s:end])
            i = end
        else:
            i = s + 3
    return out


def _valid_jpeg(path: str) -> bool:
    """Confirm it actually decodes to an image (rejects false positives)."""
    out = tools.capture(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        check=False).strip()
    parts = [p for p in out.replace("\n", ",").split(",") if p]
    return len(parts) >= 2 and all(p.isdigit() and int(p) > 0 for p in parts[:2])


def carve_stills(blob_paths: list[str], dest_dir: str,
                 fallback_dt: datetime | None = None) -> list[str]:
    """Carve, de-dupe, validate and save JPEGs found in the given raw files.
    Returns the list of written photo paths. Dates come from EXIF (via exiftool)
    where present, else the fallback."""
    os.makedirs(dest_dir, exist_ok=True)
    saved: list[str] = []
    seen: set[bytes] = set()
    idx = 0
    for bp in blob_paths:
        try:
            data = open(bp, "rb").read()
        except OSError:
            continue
        for jpg in find_jpegs(data):
            digest = hashlib.sha1(jpg).digest()
            if digest in seen:
                continue
            seen.add(digest)
            idx += 1
            out = os.path.join(dest_dir, f"DSC_carved_{idx:04d}.jpg")
            with open(out, "wb") as f:
                f.write(jpg)
            if _valid_jpeg(out):
                saved.append(out)
            else:
                os.remove(out)
                idx -= 1
    _apply_dates(saved, fallback_dt)
    return saved


def _apply_dates(paths: list[str], fallback_dt: datetime | None) -> None:
    if not paths:
        return
    if tools.has("exiftool"):
        # align mtime to EXIF capture date where present (silent if absent)
        try:
            tools.run(["exiftool", "-q", "-m", "-overwrite_original",
                       "-FileModifyDate<DateTimeOriginal", *paths], check=False)
        except Exception:
            pass
    if fallback_dt:
        ts = fallback_dt.timestamp()
        for p in paths:
            try:
                if datetime.fromtimestamp(os.path.getmtime(p)).year < 1990:
                    os.utime(p, (ts, ts))
            except OSError:
                pass
