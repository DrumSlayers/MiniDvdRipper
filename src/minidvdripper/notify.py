"""Desktop notification + completion chime, using whatever the desktop provides.

Notifications go through `notify-send` (libnotify) — the cross-desktop standard
GNOME, KDE, XFCE and the rest all honour. The chime tries freedesktop event
sounds via `canberra-gtk-play`, then a PipeWire/PulseAudio player on a themed
sound file; if none are present the TUI falls back to the terminal bell.

Everything here is best-effort and fire-and-forget: each step is guarded by a
tool-exists check and a non-blocking `Popen`, so a missing player or notifier is
silently skipped — never an error popped in the middle of (or at the end of) a rip.
"""
from __future__ import annotations

import os
import subprocess

from . import tools

APP = "MiniDvdRipper"

_SOUND_DIRS = (
    "/usr/share/sounds/freedesktop/stereo",
    "/usr/share/sounds/Oxygen/stereo",        # KDE
    "/usr/share/sounds/ubuntu/stereo",
)

# event -> (canberra event id, candidate themed file names)
_EVENTS = {
    "complete": ("complete", ("complete.oga", "complete.wav", "service-login.oga")),
    "warning":  ("dialog-warning", ("dialog-warning.oga", "bell.oga")),
    "error":    ("dialog-error", ("dialog-error.oga", "suspend-error.oga")),
}


def desktop_notify(title: str, body: str = "", urgency: str = "normal",
                   icon: str = "media-optical") -> bool:
    """Pop a desktop notification. urgency: low | normal | critical."""
    if not tools.has("notify-send"):
        return False
    cmd = ["notify-send", "-a", APP, "-u", urgency, "-i", icon, title]
    if body:
        cmd.append(body)
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def _find_sound(names, dirs=_SOUND_DIRS) -> str | None:
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    return None


def play_sound(event: str = "complete") -> bool:
    """Play the themed event sound. Returns False if nothing could play it (the
    caller then rings the terminal bell)."""
    cid, files = _EVENTS.get(event, _EVENTS["complete"])
    # 1. canberra knows the freedesktop event sounds by name — preferred.
    if tools.has("canberra-gtk-play"):
        try:
            subprocess.Popen(["canberra-gtk-play", "-i", cid],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass
    # 2. otherwise find a themed file and feed it to whatever player exists
    #    (ffplay is guaranteed — ffmpeg is a hard dependency).
    path = _find_sound(files)
    if path:
        for player in ("pw-play", "paplay", "ffplay"):
            if not tools.has(player):
                continue
            cmd = (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]
                   if player == "ffplay" else [player, path])
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                pass
    return False
