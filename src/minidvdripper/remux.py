"""Lossless remux of a recording session (VOB parts) into one MKV.

Strictly no re-encoding: the original MPEG-2 video and AC-3/LPCM audio are stream-
copied (`-c copy`) into a Matroska container. We:
  * concatenate a title's VOB parts via ffmpeg's `concat:` protocol (valid for
    MPEG program streams),
  * regenerate timestamps (`-fflags +genpts+igndts`) — DVD VOBs carry broken/looping
    PTS that otherwise make ffmpeg refuse to mux,
  * embed the recording date as `creation_time` (Matroska DateUTC) so Synology
    Photos sorts the clip on its real capture date,
  * optionally write chapter markers,
  * set the file mtime to the capture date as a belt-and-braces fallback.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from . import tools
from .titles import Title


@dataclass
class RemuxResult:
    out_path: str
    duration: float = 0.0
    decode_errors: int = 0
    streams: str = ""
    ok: bool = True
    note: str = ""


def _concat_input(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return "concat:" + "|".join(parts)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def _write_chapters(title: Title, path: str) -> str | None:
    if not title.chapters or len(title.chapters) < 2:
        return None
    lines = [";FFMETADATA1"]
    chs = title.chapters
    for i, ch in enumerate(chs):
        start = int(ch.start * 1000)
        end = int((chs[i + 1].start if i + 1 < len(chs) else (ch.start + 3600)) * 1000)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={start}", f"END={end}", f"title={ch.title}"]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return path


def remux_title(title: Title, out_path: str, disc_label: str,
                map_subtitles: bool = True, verify: bool = True,
                on_line=None, cancel=None) -> RemuxResult:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    meta_path = out_path + ".ffmeta"
    chapters_file = _write_chapters(title, meta_path)
    iso_dt = _iso(title.datetime)

    cmd = ["ffmpeg", "-y", "-hide_banner",
           "-fflags", "+genpts+igndts",
           "-analyzeduration", "200M", "-probesize", "200M",
           "-i", _concat_input(title.parts)]
    if chapters_file:
        cmd += ["-f", "ffmetadata", "-i", chapters_file, "-map_chapters", "1"]
    cmd += ["-map", "0:v?", "-map", "0:a?"]
    if map_subtitles:
        cmd += ["-map", "0:s?"]
    cmd += ["-c", "copy"]   # copies every mapped stream; unmapped subs are excluded
    # metadata
    cmd += ["-metadata", f"title=Session {title.number:02d} ({title.date_tag})",
            "-metadata", f"comment=Ripped from MiniDVD '{disc_label}' (lossless remux)"]
    if iso_dt:
        cmd += ["-metadata", f"creation_time={iso_dt}",
                "-metadata", f"date={title.datetime.strftime('%Y-%m-%d')}",
                "-metadata", f"year={title.datetime.year}"]
    cmd += [out_path]

    res = tools.run(cmd, on_line=on_line, check=False, cancel=cancel)
    if chapters_file and os.path.exists(chapters_file):
        try:
            os.remove(chapters_file)
        except OSError:
            pass

    result = RemuxResult(out_path=out_path, ok=(res.returncode == 0 and os.path.exists(out_path)))
    if not result.ok:
        # Surface ffmpeg's own diagnosis instead of a blind "failed" — the last
        # few stderr lines almost always name the real cause.
        tail = [ln.strip() for ln in res.output.splitlines() if ln.strip()][-3:]
        result.note = "ffmpeg remux failed" + (": " + " | ".join(tail) if tail else "")
        return result

    result.duration = probe_duration(out_path)
    result.streams = probe_streams(out_path)
    if verify:
        result.decode_errors = decode_error_count(out_path, cancel=cancel)

    # stamp the file mtime to the capture date (Synology sorting fallback)
    if title.datetime:
        ts = title.datetime.timestamp()
        try:
            os.utime(out_path, (ts, ts))
        except OSError:
            pass
    return result


def probe_duration(path: str) -> float:
    out = tools.capture(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path], check=False)
    for line in out.splitlines():
        line = line.strip()
        try:
            return float(line)
        except ValueError:
            continue
    return 0.0


def probe_streams(path: str) -> str:
    """Compact "video:mpeg2video, audio:ac3" description of the MKV's streams."""
    out = tools.capture(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name", "-of", "csv=p=0", path], check=False)
    parts = []
    for line in out.splitlines():
        cols = [c for c in line.strip().split(",") if c]
        if len(cols) >= 2:
            parts.append(f"{cols[0]}:{cols[1]}")
        elif cols:
            parts.append(cols[0])
    return ", ".join(parts)


# Benign ffmpeg chatter on DVD/VOB material — NOT corruption. The null muxer
# whines about DTS order, and MPEG-2 from DVDs routinely emits timestamp/VBV
# notes. Only messages that aren't on this list count as real decode damage.
_BENIGN = (
    "non monotonically increasing dts",
    "Application provided invalid",
    "Last message repeated",
    "Invalid timestamp",
    "timestamp discontinuity",
    "VBV buffer",
    "Estimating duration",
    "first pts value must",
    "packet with invalid duration",
)


def decode_error_count(path: str, on_line=None, cancel=None) -> int:
    """Full decode pass; count only GENUINE decoder errors (corrupt frames),
    not muxer/timestamp chatter. Regenerates timestamps to silence the null
    muxer's DTS complaints at the source."""
    res = tools.run(
        ["ffmpeg", "-v", "error", "-fflags", "+genpts+igndts",
         "-i", path, "-map", "0:v?", "-f", "null", "-"],
        on_line=None, check=False, cancel=cancel)
    real = 0
    for line in res.output.splitlines():
        s = line.strip()
        if s and not any(b in s for b in _BENIGN):
            real += 1
    return real
