"""External-tool discovery and subprocess helpers.

Everything heavy (ddrescue, ffmpeg, bsdtar...) runs as a subprocess. This module
locates the binaries, reports what's missing, and streams their output line-by-line
so the TUI can show live progress. ddrescue/ffmpeg update a single status line with
carriage returns, so the reader splits on both \\r and \\n.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

# name -> (purpose, required?)
REQUIRED = {
    "ddrescue": "bit-rot-safe disc imaging",
    "ffmpeg": "lossless remux to MKV",
    "ffprobe": "stream/duration inspection",
    "dvd+rw-mediainfo": "disc identification + finalization check",
    "isoinfo": "ISO directory/extent listing (sector->file map)",
    "bsdtar": "rootless ISO extraction (preserves timestamps)",
}
OPTIONAL = {
    "eject": "open the tray between discs",
    "lsdvd": "chapter/title metadata (auto 5-min camcorder chapters)",
    "exiftool": "stamp/verify EXIF capture dates on photos",
}

LineCb = Callable[[str], None] | None


@dataclass
class Preflight:
    found: dict = field(default_factory=dict)      # name -> path
    missing_required: list = field(default_factory=list)
    missing_optional: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_required

    def summary(self) -> str:
        lines = []
        for name in {**REQUIRED, **OPTIONAL}:
            mark = "OK " if name in self.found else "-- "
            lines.append(f"  [{mark}] {name}")
        if self.missing_required:
            lines.append("")
            lines.append("MISSING (required): " + ", ".join(self.missing_required))
            lines.append("  install: sudo pacman -S --needed gddrescue ffmpeg "
                         "dvd+rw-tools cdrtools libarchive")
        if self.missing_optional:
            lines.append("MISSING (optional): " + ", ".join(self.missing_optional))
        return "\n".join(lines)


def which(name: str) -> str | None:
    return shutil.which(name)


def preflight() -> Preflight:
    pf = Preflight()
    for name in REQUIRED:
        p = which(name)
        if p:
            pf.found[name] = p
        else:
            pf.missing_required.append(name)
    for name in OPTIONAL:
        p = which(name)
        if p:
            pf.found[name] = p
        else:
            pf.missing_optional.append(name)
    return pf


def has(name: str) -> bool:
    return which(name) is not None


@dataclass
class RunResult:
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _pump(stream, on_segment: Callable[[str], None]) -> None:
    """Read raw bytes, split on \\r and \\n, hand each non-empty segment to cb."""
    buf = bytearray()
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        if chunk in (b"\n", b"\r"):
            if buf:
                on_segment(buf.decode("utf-8", "replace"))
                buf.clear()
        else:
            buf += chunk
    if buf:
        on_segment(buf.decode("utf-8", "replace"))


class Cancelled(Exception):
    """Raised when a run is aborted via its cancel event."""


def run(cmd: list[str], on_line: LineCb = None, check: bool = True,
        env: dict | None = None, cwd: str | None = None,
        cancel: threading.Event | None = None) -> RunResult:
    """Run cmd, merge stdout+stderr, stream segments to on_line, capture all.

    If `cancel` (a threading.Event) is supplied and gets set, the subprocess is
    killed and Cancelled is raised."""
    captured: list[str] = []

    def sink(seg: str) -> None:
        captured.append(seg)
        if on_line:
            on_line(seg)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0, env=env, cwd=cwd,
    )
    stop = threading.Event()
    if cancel is not None:
        def _watch():
            while not stop.wait(0.2):
                if cancel.is_set():
                    for _ in range(2):
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    return
        threading.Thread(target=_watch, daemon=True).start()
    _pump(proc.stdout, sink)
    proc.wait()
    stop.set()
    res = RunResult(proc.returncode, "\n".join(captured))
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, res.output)
    return res


def capture(cmd: list[str], check: bool = True) -> str:
    """Run cmd quietly and return combined output (no streaming)."""
    return run(cmd, on_line=None, check=check).output


def tool_versions() -> dict:
    """Best-effort version strings for the report."""
    out = {}
    probes = {
        "ddrescue": ["ddrescue", "--version"],
        "ffmpeg": ["ffmpeg", "-version"],
        "lsdvd": ["lsdvd", "-V"],
        "exiftool": ["exiftool", "-ver"],
    }
    for name, cmd in probes.items():
        if not has(name):
            continue
        try:
            txt = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
            out[name] = txt.strip().splitlines()[0] if txt.strip() else "?"
        except Exception:
            out[name] = "?"
    return out
