"""Keep the machine awake while a rip runs (a sleep "wakelock").

Same idea as a browser holding the screen awake during video playback: we take a
systemd-logind inhibitor lock. The simplest, dependency-free way is to spawn
`systemd-inhibit … sleep infinity` and hold it for the rip's duration — the lock
exists as long as that helper process lives, and is released the instant we kill
it. We block idle + sleep (so a long ddrescue/dd doesn't get suspended), but not
the lid switch by default.

Degrades silently: if systemd-inhibit is missing, ripping still works, the machine
just isn't prevented from sleeping.
"""
from __future__ import annotations

import shutil
import subprocess


class Inhibitor:
    """Context manager. `with Inhibitor("why", enabled=True): ...`"""

    def __init__(self, why: str = "MiniDvdRipper is ripping a disc",
                 enabled: bool = True):
        self.why = why
        self.enabled = enabled
        self.proc = None

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def __enter__(self) -> Inhibitor:
        if self.enabled and shutil.which("systemd-inhibit"):
            try:
                self.proc = subprocess.Popen(
                    ["systemd-inhibit",
                     "--what=idle:sleep",
                     "--who=MiniDvdRipper",
                     f"--why={self.why}",
                     "--mode=block",
                     "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.proc = None
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
