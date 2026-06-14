"""Fallback recovery: carve recorded tracks straight out of the disc image.

Used when a disc has video but no usable VIDEO_TS — e.g. a DVD-R we finalized on
the PC (which writes only the lead-out, not the camcorder's VIDEO_TS.IFO), or a
Sony +VR / VIDEO_RM disc whose filesystem index is a placeholder. The drive's
track table (from dvd+rw-mediainfo) tells us where each finished recording lives:
start LBA + size. We slice that byte range out of the ISO into a .vob, and the
normal lossless remux turns it into an MKV. MPEG-2 program streams need no
container/IFO to be demuxable, so this recovers the video with zero re-encode.

One wrinkle: a random-access disc (DVD+RW / DVD-RAM) often reports a SINGLE track
covering the whole disc from LBA 0, so the slice would begin in the UDF/ISO
filesystem header, not the video — ffmpeg then rejects it ("Invalid data found").
So before slicing we seek forward to the first MPEG-2 pack header (00 00 01 BA)
and start there, skipping whatever non-video prefix sits at the front. For a real
VOB the pack header is already at the track's first byte, so this is a no-op.
"""
from __future__ import annotations

import os
from datetime import datetime

from . import tools
from .titles import Title

SECTOR = 2048
_PACK = b"\x00\x00\x01\xba"   # MPEG-2 program-stream pack header (sector-aligned on DVD)


def _first_pack_lba(iso_path: str, start_lba: int, end_lba: int,
                    chunk: int = 8 << 20) -> int:
    """LBA of the first MPEG-2 pack header at/after start_lba (exclusive of
    end_lba), or -1 if none. DVD payload is sector-aligned, so we return the
    sector containing the header."""
    try:
        with open(iso_path, "rb") as f:
            pos = start_lba * SECTOR
            limit = end_lba * SECTOR
            f.seek(pos)
            prev = b""
            while pos < limit:
                buf = f.read(min(chunk, limit - pos))
                if not buf:
                    break
                data = prev + buf
                i = data.find(_PACK)
                if i >= 0:
                    return (pos - len(prev) + i) // SECTOR
                prev = data[-3:]            # carry a 3-byte tail across the boundary
                pos += len(buf)
    except OSError:
        pass
    return -1


def slice_tracks(iso_path: str, tracks: list, tmp_dir: str,
                 min_bytes: int = 512 * 1024,
                 fallback_dt: datetime | None = None) -> list[Title]:
    """Slice each COMPLETE, substantial track out of the image into a .vob and
    return Title objects pointing at the slices (kind='carved')."""
    os.makedirs(tmp_dir, exist_ok=True)
    iso_size = os.path.getsize(iso_path) if os.path.exists(iso_path) else 0
    titles: list[Title] = []
    n = 0
    for tr in tracks:
        if "complete" not in tr.get("state", "") or tr.get("bytes", 0) < min_bytes:
            continue
        start_lba = tr["start_lba"]
        start = start_lba * SECTOR
        if start >= iso_size:
            continue                      # track lies beyond what we imaged
        sectors = tr["sectors"]
        if start + sectors * SECTOR > iso_size:    # clamp to imaged region
            sectors = max(0, (iso_size - start) // SECTOR)
        if sectors == 0:
            continue
        # Skip any non-video prefix (filesystem header on a whole-disc track):
        # start at the first MPEG-2 pack header within the track's range.
        pack_lba = _first_pack_lba(iso_path, start_lba, start_lba + sectors)
        if pack_lba < 0:
            continue                      # no MPEG-2 program stream in this track
        sectors -= (pack_lba - start_lba)
        start_lba = pack_lba
        if sectors <= 0:
            continue
        n += 1
        out = os.path.join(tmp_dir, f"track_{n:02d}.vob")
        tools.run(["dd", f"if={iso_path}", f"of={out}", "bs=2048",
                   f"skip={start_lba}", f"count={sectors}",
                   "conv=noerror"], check=False)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            titles.append(Title(number=n, parts=[out], datetime=fallback_dt,
                                size_bytes=os.path.getsize(out), kind="carved"))
    return titles
