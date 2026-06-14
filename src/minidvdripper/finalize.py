"""Finalize (close) an unfinalized camcorder DVD-R on a PC burner.

The Sony Handycam's "Finalize" just writes a lead-out/TOC so other drives can read
the disc. If you no longer have the camcorder, a DVD writer can do the same job:
`cdrecord -fix` fixates (closes) the disc without adding data. After that the disc
exposes its recorded sectors and rips like any finalized DVD.

THIS WRITES TO THE DISC and is irreversible — callers must get explicit user
confirmation first. We refuse anything that isn't an unfinalized disc with data.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import tools
from .disc import DiscInfo


@dataclass
class FinalizeCheck:
    ok: bool
    reason: str


def can_finalize(info: DiscInfo) -> FinalizeCheck:
    """Only proceed for an unfinalized disc that actually has recordings."""
    k = info.kind
    if k == "no_media":
        return FinalizeCheck(False, "No disc in the drive.")
    if k == "ready":
        return FinalizeCheck(False, "Disc is already finalized — just rip it.")
    if k == "blank":
        return FinalizeCheck(False, "Disc is blank — nothing to finalize.")
    if "DVD-R" not in info.media and "DVD-RW" not in info.media:
        return FinalizeCheck(False, f"Don't know how to finalize media {info.media!r}.")
    return FinalizeCheck(True, f"~{info.recorded_bytes/1e6:.0f} MB in "
                               f"{info.recorded_tracks} recording(s) ready to close.")


def finalize_disc(device: str, on_line=None) -> bool:
    """Write the lead-out (close/fixate the disc). Returns True on success.
    IRREVERSIBLE — only call after explicit confirmation."""
    # -fix = fixate only (no data written); -v = progress; gracetime to allow abort.
    res = tools.run(["cdrecord", "-v", "gracetime=5", "-fix", f"dev={device}"],
                    on_line=on_line, check=False)
    return res.returncode == 0
