"""Work with the ISO image: rootless extraction + sector->file mapping.

We never mount the disc (no root needed): bsdtar (libarchive) reads ISO9660 +
Rock Ridge directly and restores per-file timestamps — and those timestamps are
the original camcorder recording dates, which we reuse for the MKVs.

isoinfo -l gives each file's starting LBA + size, which lets us translate the
ddrescue bad-sector ranges into "which file is rotten".
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from . import tools
from .imaging import SECTOR, BadRange

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"


@dataclass
class IsoFile:
    path: str          # e.g. "/VIDEO_TS/VTS_01_1.VOB"
    start_lba: int
    size: int
    end_lba: int = 0   # exclusive
    bad_sectors: int = 0

    def __post_init__(self):
        blocks = max(1, math.ceil(self.size / SECTOR)) if self.size else 0
        self.end_lba = self.start_lba + blocks


def extract_iso(iso_path: str, dest: str, cancel=None) -> str:
    """Extract the whole ISO tree into dest, preserving timestamps (-p)."""
    os.makedirs(dest, exist_ok=True)
    tools.run(["bsdtar", "-x", "-p", "-f", iso_path, "-C", dest], check=True, cancel=cancel)
    return dest


def list_files(iso_path: str) -> list[IsoFile]:
    """Parse `isoinfo -l` into IsoFile entries with extent LBA + size."""
    out = tools.capture(["isoinfo", "-l", "-i", iso_path], check=False)
    return parse_isoinfo_listing(out)


def parse_isoinfo_listing(out: str) -> list[IsoFile]:
    """Pure parser for `isoinfo -l` text (separated out for testing)."""
    files: list[IsoFile] = []
    cur_dir = "/"
    dir_re = re.compile(r"^Directory listing of (.+?)\s*$")
    size_re = re.compile(rf"(\d+)\s+(?:{_MONTHS})\b")
    for line in out.splitlines():
        d = dir_re.match(line)
        if d:
            cur_dir = d.group(1)
            if not cur_dir.endswith("/"):
                cur_dir += "/"
            continue
        if "[" not in line or "]" not in line:
            continue
        if line.lstrip().startswith("d"):   # directory entry, skip
            continue
        left, _, right = line.partition("[")
        bracket, _, name = right.partition("]")
        name = name.strip()
        if name in (".", "..") or not name:
            continue
        name = re.sub(r";\d+$", "", name)   # strip ISO version ";1"
        mext = re.match(r"\s*(\d+)", bracket)
        msz = size_re.search(left)
        if not mext or not msz:
            continue
        files.append(IsoFile(path=cur_dir + name,
                             start_lba=int(mext.group(1)),
                             size=int(msz.group(1))))
    return files


def map_rot_to_files(files: list[IsoFile], bad_ranges: list[BadRange]) -> list[IsoFile]:
    """Annotate each IsoFile with how many bad sectors fall inside it.
    Returns only the affected files."""
    affected = []
    for f in files:
        bad = 0
        for r in bad_ranges:
            lo = max(f.start_lba, r.start_lba)
            hi = min(f.end_lba, r.end_lba)
            if hi > lo:
                bad += hi - lo
        if bad:
            f.bad_sectors = bad
            affected.append(f)
    return affected
