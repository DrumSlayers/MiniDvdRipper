"""Bit-rot-safe disc imaging with GNU ddrescue, and mapfile -> rot report.

Why ddrescue and not dvdbackup/dd:
  * dvdbackup / libdvdread / plain dd abort or hang on the first unreadable sector.
  * ddrescue is built for failing media: it grabs all the good data first, then
    retries only the bad spots, and records EXACTLY which sectors never came back
    in a mapfile. That mapfile is our proof-of-rot — we can later say "VTS_02_1.VOB
    has 3 unreadable sectors" instead of silently shipping a corrupt file.

Two passes:
  1) fast, no-scrape  : -n   (rescue every easily-readable sector quickly)
  2) retry + scrape   : -rN  (hammer the few bad spots, direct disc access)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import tools

SECTOR = 2048  # DVD logical block size


@dataclass
class BadRange:
    start_lba: int
    end_lba: int   # exclusive
    status: str    # ddrescue status char: '-', '*', '/', '?'

    @property
    def sectors(self) -> int:
        return self.end_lba - self.start_lba


@dataclass
class RotReport:
    total_bytes: int = 0
    recovered_bytes: int = 0
    bad_bytes: int = 0
    bad_ranges: list = field(default_factory=list)  # list[BadRange]
    passes_done: int = 0

    @property
    def clean(self) -> bool:
        return self.bad_bytes == 0

    @property
    def recovered_pct(self) -> float:
        if self.total_bytes == 0:
            return 100.0
        return 100.0 * self.recovered_bytes / self.total_bytes

    def summary(self) -> str:
        if self.clean:
            return f"Clean rip — 100% recovered ({self.total_bytes:,} bytes)."
        return (f"BIT ROT: {self.bad_bytes:,} bytes unrecoverable "
                f"({len(self.bad_ranges)} bad region(s)), "
                f"{self.recovered_pct:.4f}% recovered.")


def ddrescue_image(device: str, iso_path: str, map_path: str,
                   retries: int = 3, on_line=None, cancel=None) -> RotReport:
    """Image `device` -> `iso_path`, tracking bad sectors in `map_path`.

    Resumable: if iso+mapfile already exist, ddrescue continues where it left off.
    """
    os.makedirs(os.path.dirname(os.path.abspath(iso_path)) or ".", exist_ok=True)

    # Pass 1: quick sweep, do not scrape.
    tools.run(
        ["ddrescue", "-b", str(SECTOR), "-d", "-n", "-v", device, iso_path, map_path],
        on_line=on_line, check=False, cancel=cancel,
    )
    # Pass 2: direct access, retry/scrape the holes.
    tools.run(
        ["ddrescue", "-b", str(SECTOR), "-d", f"-r{retries}", "-v", device, iso_path, map_path],
        on_line=on_line, check=False, cancel=cancel,
    )
    return parse_mapfile(map_path)


_BLOCK = re.compile(r"^\s*(0x[0-9A-Fa-f]+|\d+)\s+(0x[0-9A-Fa-f]+|\d+)\s+([+\-*/?])\s*$")


def _num(s: str) -> int:
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def parse_mapfile(map_path: str) -> RotReport:
    """Parse a ddrescue mapfile.

    Block lines: `<pos> <size> <status>`. status '+' = recovered; anything else
    ('-' bad, '*' failed-non-trimmed, '/' failed-non-scraped, '?' non-tried) =
    not recovered. The first non-comment line is the status line (skip it).
    """
    rep = RotReport()
    if not os.path.exists(map_path):
        return rep
    seen_status_line = False
    for line in open(map_path, encoding="utf-8", errors="replace"):
        if line.startswith("#") or not line.strip():
            continue
        m = _BLOCK.match(line)
        if not m:
            continue
        # The status line "<pos> <char> <pass>" also matches loosely; the real
        # block lines have a numeric size in column 2. Distinguish: status line's
        # 2nd field is a single status char, so _BLOCK (which needs a number in
        # col 2) won't match it. Good — but guard anyway.
        if not seen_status_line:
            seen_status_line = True
            # ddrescue 1.20+ always emits the status line first; it won't match
            # _BLOCK (col2 is a char), so if we got here it's already a block.
        pos, size, st = _num(m.group(1)), _num(m.group(2)), m.group(3)
        rep.total_bytes += size
        if st == "+":
            rep.recovered_bytes += size
        else:
            rep.bad_bytes += size
            rep.bad_ranges.append(BadRange(pos // SECTOR, (pos + size) // SECTOR, st))
    return rep
