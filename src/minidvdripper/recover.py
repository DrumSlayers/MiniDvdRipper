"""Direct recovery of UNFINALIZED DVD-R discs — no finalize, no second drive.

A camcorder DVD-R that was never finalized has no lead-out, so it reports
READ CAPACITY 0 and most software gives up. But the kernel still exposes the
recorded extent as the block device, and the *complete* tracks read fine through
the buffered block layer (raw SCSI passthrough is what gets rejected). So we read
each finished recording directly with `dd`, addressed by the drive's track table
(start LBA + sector count from dvd+rw-mediainfo), and skip the empty *reserved*
track — which otherwise reads at ~1 kB/s and would take an hour of nothing.

Each track is an MPEG-2 program stream and remuxes losslessly, exactly like a
finalized VOB. Still photos can't be opened by name (no filesystem directory), but
their JPEG bytes still live in a small 'complete' data track, so we dump those
small tracks too and let stills.py carve the photos out of them.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

from . import tools
from .titles import Title

SECTOR = 2048
# dd `status=progress` line (forced C locale): "<N> bytes (..) copied, <t> s, <rate>"
_BYTES = re.compile(r"(\d+)\s+bytes")
_RATE = re.compile(r",\s*([\d.]+\s*[kMG]?B/s)\s*$")


def recover_unfinalized(device: str, tracks: list, out_dir: str,
                        min_bytes: int = 512 * 1024,
                        fallback_dt: datetime | None = None,
                        on_progress=None, on_line=None, cancel=None):
    """dd each COMPLETE, substantial track off the device into a .vob.
    on_progress(label, frac) is called live. Returns (titles, bad_blocks)."""
    os.makedirs(out_dir, exist_ok=True)
    complete = [t for t in tracks
                if "complete" in t.get("state", "") and t.get("bytes", 0) >= min_bytes]
    n_total = len(complete)
    titles: list[Title] = []
    bad_blocks = 0
    cenv = dict(os.environ, LC_ALL="C")     # force "bytes"/"MB/s" so we can parse
    for idx, t in enumerate(complete, 1):
        out = os.path.join(out_dir, f"track_{idx:02d}.vob")
        total = t["sectors"] * SECTOR
        tot_mb = total / 1e6

        def prog(line: str, _tot=total, _i=idx, _tmb=tot_mb):
            m = _BYTES.search(line)
            if not m or not on_progress:
                return
            done = int(m.group(1))
            rate = (_RATE.search(line).group(1) if _RATE.search(line) else "")
            lbl = (f"Recover track {_i}/{n_total}  "
                   f"{done/1e6:.0f}/{_tmb:.0f} MB  {rate}").rstrip()
            on_progress(lbl, min(1.0, done / _tot) if _tot else None)

        res = tools.run(
            ["dd", f"if={device}", f"of={out}", "bs=2048",
             f"skip={t['start_lba']}", f"count={t['sectors']}",
             "conv=noerror,sync", "status=progress"],
            on_line=prog, check=False, env=cenv, cancel=cancel)
        bad_blocks += res.output.count("error reading") + res.output.count("Input/output error")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            titles.append(Title(number=idx, parts=[out], datetime=fallback_dt,
                                size_bytes=os.path.getsize(out), kind="recovered"))
    return titles, bad_blocks


def read_small_tracks(device: str, tracks: list, out_dir: str,
                      lo: int = 16 * 1024, hi: int = 512 * 1024, cancel=None) -> list[str]:
    """dd the small COMPLETE tracks (below the video threshold `hi`) into .bin
    blobs so still photos can be carved from them. Returns the blob paths.

    Handycams keep stills in a small closed data track; the big track is video."""
    os.makedirs(out_dir, exist_ok=True)
    cenv = dict(os.environ, LC_ALL="C")
    paths: list[str] = []
    cand = [t for t in tracks
            if "complete" in t.get("state", "") and lo <= t.get("bytes", 0) < hi]
    for idx, t in enumerate(cand, 1):
        out = os.path.join(out_dir, f"still_track_{idx:02d}.bin")
        tools.run(["dd", f"if={device}", f"of={out}", "bs=2048",
                   f"skip={t['start_lba']}", f"count={t['sectors']}",
                   "conv=noerror,sync"], check=False, env=cenv, cancel=cancel)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            paths.append(out)
    return paths
