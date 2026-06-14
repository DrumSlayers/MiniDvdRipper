"""Disc identification + finalization check via dvd+rw-mediainfo, plus volume label.

Sony Handycam DVD background (why finalization matters):
  * Camcorders write 8cm DVD-R / DVD-RW / DVD-RAM / DVD+RW.
  * DVD-R and DVD-RW-in-Video-mode must be *finalized* on the camcorder before any
    other drive can read the VIDEO_TS structure. An unfinalized Video-mode disc
    reads as blank/garbage elsewhere.
  * DVD-RW/DVD-RAM/DVD+RW in *VR mode* store DVD_RTAV/VR_MOVIE.VRO instead of
    VIDEO_TS and are readable without finalization.
  * dvd+rw-mediainfo reports "Disc status: complete" + "State of Last Session:
    complete" once the disc is closed/finalized — that's our green light.
  * Each press of record makes a new title (VTS_NN) -> one MKV per session.
"""
from __future__ import annotations

import glob
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import tools


@dataclass
class DiscInfo:
    raw: str = ""
    media: str = ""               # e.g. "DVD-RW Restricted Overwrite"
    media_id: str = ""
    disc_status: str = ""         # complete / incomplete / blank
    last_session_state: str = ""  # complete / incomplete / empty
    sessions: int = 0
    tracks: int = 0
    capacity_bytes: int = 0
    recorded_bytes: int = 0       # data in COMPLETE tracks (works even if unfinalized)
    recorded_tracks: int = 0      # number of completed recordings on the disc
    track_list: list = field(default_factory=list)  # [{start_lba,sectors,state,bytes}]
    label: str = ""
    datetime: datetime | None = None
    warnings: list = field(default_factory=list)

    @property
    def finalized(self) -> bool:
        ds = self.disc_status.lower()
        ls = self.last_session_state.lower()
        return ds == "complete" and (ls == "" or ls == "complete")

    @property
    def media_present(self) -> bool:
        """The drive sees a disc (vs empty tray / no drive)."""
        return bool(self.media) or "Mounted Media" in self.raw

    @property
    def imageable(self) -> bool:
        """Can ddrescue actually read a sized image from it?
        An unfinalized DVD-R reports READ CAPACITY 0 — nothing to image."""
        return self.capacity_bytes > 0

    @property
    def has_data(self) -> bool:
        """True if any finished recording is on the disc (even if unfinalized)."""
        return self.recorded_bytes > 0 or self.recorded_tracks > 0

    @property
    def kind(self) -> str:
        """Triage class: no_media | blank | unfinalized | ready."""
        if not self.media_present:
            return "no_media"
        if self.imageable:
            return "ready"
        return "unfinalized" if self.has_data else "blank"

    def scan_line(self) -> str:
        """One-line triage summary for collection scanning."""
        mb = self.recorded_bytes / 1e6
        k = self.kind
        if k == "no_media":
            return "empty / no disc"
        if k == "ready":
            return (f"READY — finalized, {self.capacity_bytes/1e6:.0f} MB, "
                    f"label {self.label or '?'} ({self.media})")
        if k == "unfinalized":
            return (f"UNFINALIZED but RECOVERABLE — ~{mb:.0f} MB in "
                    f"{self.recorded_tracks} recording(s) ({self.media}). "
                    "Rip reads the tracks directly (no finalize needed); still photos "
                    "are carved from the raw tracks where present.")
        return f"BLANK — no recordings ({self.media})"

    def scan_level(self) -> str:
        return {"ready": "ok", "unfinalized": "warn",
                "blank": "dim", "no_media": "dim"}[self.kind]

    @property
    def blocker(self) -> str | None:
        """Human reason the disc can't be ripped right now, or None if OK.
        Note: an unfinalized disc WITH recordings is NOT blocked — the ripper
        recovers its tracks directly off the device (see recover.py)."""
        k = self.kind
        if k == "no_media":
            return ("No disc detected (or wrong device). Press 'd' to detect the "
                    "drive, insert a disc, and try again.")
        if k == "blank":
            return f"Blank disc — no recordings to rip ({self.media})."
        return None


_KV = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 #/+().-]*?):\s*(.+?)\s*$")


# ---- optical device detection -------------------------------------------

def list_optical() -> list[str]:
    return sorted(glob.glob("/dev/sr*"))


def has_media(device: str) -> bool:
    return "Mounted Media" in read_mediainfo(device)


def detect_device(prefer: str | None = None) -> str:
    """Pick an optical device that currently has media; prefer `prefer`.
    Handles the USB-drive-moved-from-sr0-to-sr1 case."""
    devs = list_optical()
    if prefer and prefer in devs and has_media(prefer):
        return prefer
    for d in devs:
        if has_media(d):
            return d
    return prefer or (devs[0] if devs else "/dev/sr0")


def read_mediainfo(device: str, on_line=None) -> str:
    """Run dvd+rw-mediainfo on the device, return raw text."""
    return tools.run(["dvd+rw-mediainfo", device], on_line=on_line, check=False).output


def parse_mediainfo(raw: str) -> DiscInfo:
    info = DiscInfo(raw=raw)
    for line in raw.splitlines():
        m = _KV.match(line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        kl = key.lower()
        if kl == "mounted media":
            # "13h, DVD-RW Restricted Overwrite" -> drop the hex code
            info.media = val.split(",", 1)[-1].strip() if "," in val else val
        elif kl == "media id":
            info.media_id = val
        elif kl == "disc status":
            info.disc_status = val
        elif kl == "state of last session":
            info.last_session_state = val
        elif kl == "number of sessions":
            info.sessions = _int(val)
        elif kl == "number of tracks":
            info.tracks = _int(val)
        elif kl == "read capacity":
            # "537856*2048=1101529088"
            if "=" in val:
                info.capacity_bytes = _int(val.split("=")[-1])
    _parse_tracks(raw, info)
    if info.media_present and not info.finalized:
        if info.recorded_tracks:
            info.warnings.append(
                f"Disc not finalized (status={info.disc_status!r}). Recovering its "
                f"{info.recorded_tracks} recording(s) by reading the tracks directly — "
                "no VIDEO_TS, no chapters, no stills, recording dates unavailable.")
        else:
            info.warnings.append(
                f"Disc not finalized (status={info.disc_status!r}) and no complete "
                "recordings detected.")
    return info


_2KB = re.compile(r"(\d+)\s*\*\s*2KB")


def _parse_tracks(raw: str, info: DiscInfo) -> None:
    """Sum the size of COMPLETE tracks. On an unfinalized disc the drive reports
    READ CAPACITY 0, but each finished recording still shows up as a 'complete
    incremental' track with a Track Size — so we can tell 'has data' from 'blank'
    even when the disc was never finalized."""
    cur = None
    for line in raw.splitlines():
        if "READ TRACK INFORMATION" in line:
            cur = {"state": "", "start_lba": 0, "size": 0}
            continue
        if cur is None:
            continue
        m = _KV.match(line)
        if not m:
            continue
        k, v = m.group(1).strip().lower(), m.group(2).strip()
        if k == "track state":
            cur["state"] = v
        elif k == "track start address":
            mm = _2KB.search(v)
            cur["start_lba"] = int(mm.group(1)) if mm else 0
        elif k == "track size":
            mm = _2KB.search(v)
            cur["size"] = int(mm.group(1)) * 2048 if mm else 0
            info.track_list.append({
                "start_lba": cur["start_lba"],
                "sectors": cur["size"] // 2048,
                "state": cur["state"],
                "bytes": cur["size"],
            })
            # a 'complete' track holds a finished recording
            if "complete" in cur["state"]:
                info.recorded_bytes += cur["size"]
                info.recorded_tracks += 1
            cur = None


def _int(s: str) -> int:
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else 0


def volume_label(device_or_iso: str) -> str:
    """Volume id from isoinfo (works on /dev/srX or an .iso path)."""
    try:
        out = tools.capture(["isoinfo", "-d", "-i", device_or_iso], check=False)
    except Exception:
        return ""
    for line in out.splitlines():
        m = re.match(r"\s*Volume id:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip()
    return ""


# Sony labels look like 2005_08_21_04H13M_PM  (also tolerate 24h / missing AM-PM)
_LABEL_DT = re.compile(
    r"(?P<y>\d{4})[_-](?P<mo>\d{2})[_-](?P<d>\d{2})"
    r"[_-]?(?P<h>\d{2})[Hh](?P<mi>\d{2})[Mm]?"
    r"(?:[_-]?(?P<ap>AM|PM|am|pm))?"
)


def parse_label_datetime(label: str) -> datetime | None:
    if not label:
        return None
    m = _LABEL_DT.search(label)
    if not m:
        return None
    y, mo, d = int(m["y"]), int(m["mo"]), int(m["d"])
    h, mi = int(m["h"]), int(m["mi"])
    ap = (m["ap"] or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    elif ap == "AM" and h == 12:
        h = 0
    try:
        return datetime(y, mo, d, h, mi)
    except ValueError:
        return None


def folder_label(label: str | None) -> str | None:
    """Per-disc folder name. Sony burns the label in 12-hour form with AM/PM
    (`2007_04_08_01H10M_PM`); normalise it to 24-hour (`2007_04_08_13H10M`) so the
    folder matches the per-clip file names. Falls back to the raw label if it
    doesn't parse as a date, or None if there's no label at all."""
    if not label:
        return None
    dt = parse_label_datetime(label)
    return dt.strftime("%Y_%m_%d_%HH%MM") if dt else label


def identify(device: str, on_line=None) -> DiscInfo:
    raw = read_mediainfo(device, on_line=on_line)
    info = parse_mediainfo(raw)
    info.label = volume_label(device)
    info.datetime = parse_label_datetime(info.label)
    return info
