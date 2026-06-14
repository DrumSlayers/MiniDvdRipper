"""Persistent settings (parent output folder, device, knobs).

Stored as JSON at ~/.config/minidvdripper/config.json — editable by hand, set
through the TUI, or overridden per-run by CLI flags.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "minidvdripper"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class Config:
    parent_dir: str = ""              # where per-disc folders are created
    device: str = "/dev/sr0"
    keep_iso: bool = True             # keep the bit-exact ISO master (recommended)
    keep_mapfile: bool = True         # keep ddrescue mapfile (rot proof)
    keep_extracted: bool = False      # keep raw VIDEO_TS/DCIM extraction
    ddrescue_retries: int = 3
    map_subtitles: bool = True        # carry DVD subtitle streams into the MKV
    stamp_exif: bool = True           # use exiftool to ensure photo capture dates
    eject_when_done: bool = True
    verify_decode: bool = True        # ffmpeg null-decode pass to catch corrupt frames
    contact_sheets: bool = True       # one storyboard JPEG per movie (quick preview)
    folder_thumbnails: bool = True    # one overview montage of the whole disc folder
    montage_in_root: bool = True      # also copy each folder's overview into the parent
    inhibit_sleep: bool = True        # keep the machine awake during a rip
    nerd_icons: bool = True           # Nerd Font glyphs in the TUI (off => unicode)
    notify_on_done: bool = True       # desktop notification when a rip finishes
    sound_on_done: bool = True        # play a chime when a rip finishes

    @classmethod
    def load(cls) -> Config:
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                known = {f.name for f in fields(cls)}
                return cls(**{k: v for k, v in data.items() if k in known})
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))
