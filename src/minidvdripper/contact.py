"""Contact sheets and a folder overview montage.

Two products:
  * contact_sheet()   — one storyboard JPEG per movie: N frames sampled evenly and
                        tiled, with a header band showing the clip's metadata
                        (filename · codec · resolution · duration · size), drawn in
                        the same ffmpeg pass via the drawtext filter.
  * folder_montage()  — one overview image for the whole disc folder: a mid-frame
                        from every movie plus every recovered photo, tiled and
                        labelled with ImageMagick `montage`, so the folder's whole
                        contents are visible in a single picture.

ffmpeg is a hard dependency (so the per-movie sheet always works); the folder
montage additionally needs ImageMagick and is skipped with a note if absent.
"""
from __future__ import annotations

import glob
import os
import shutil
import tempfile

from . import tools

_FONT_CANDIDATES = (
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
)
_PHOTO_GLOBS = ("*.jpg", "*.JPG", "*.jpeg", "*.JPEG", "*.png", "*.PNG")
_VIDEO_GLOBS = ("*.mkv", "*.mp4", "*.MKV", "*.MP4")


def _fontfile() -> str | None:
    for c in _FONT_CANDIDATES:
        if os.path.exists(c):
            return c
    out = tools.capture(["fc-match", "-f", "%{file}", "DejaVu Sans"], check=False).strip()
    return out if out and os.path.exists(out) else None


def _hms(secs: float) -> str:
    s = int(secs)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _probe_duration(path: str) -> float:
    out = tools.capture(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path], check=False).strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def _probe_meta(path: str) -> tuple[str, int, int]:
    """(codec, width, height) of the first video stream."""
    out = tools.capture(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height", "-of", "csv=p=0", path],
        check=False).strip()
    parts = out.split(",")
    codec = parts[0] if parts and parts[0] else "?"
    try:
        w, h = int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        w = h = 0
    return codec, w, h


def contact_sheet(video_path: str, out_path: str, duration: float,
                  cols: int = 5, rows: int = 4, thumb_w: int = 320,
                  on_line=None, cancel=None) -> bool:
    """Make a cols x rows storyboard JPEG with a metadata header. Returns True on
    success. Needs a known duration > 0 to space the samples."""
    if duration <= 0:
        return False
    n = cols * rows
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    codec, w, h = _probe_meta(video_path)
    size_mb = os.path.getsize(video_path) / 1e6 if os.path.exists(video_path) else 0
    title = os.path.basename(video_path)
    res = f"{w}×{h}" if w and h else "?"
    detail = f"{codec}   •   {res}   •   {_hms(duration)}   •   {size_mb:.0f} MB"

    font = _fontfile()
    header = 66 if font else 0
    # Slight overshoot (n+1) so rounding never leaves the grid one frame short.
    fps = (n + 1) / duration
    vf = (f"fps={fps:.6f},scale={thumb_w}:-1:flags=bicubic,"
          f"tile={cols}x{rows}:padding=4:margin=6:color=0x2b2b2b")

    tmpdir = None
    if font:
        tmpdir = tempfile.mkdtemp(prefix="mdvd_cs_")
        tf_title = os.path.join(tmpdir, "title.txt")
        tf_det = os.path.join(tmpdir, "detail.txt")
        with open(tf_title, "w") as f:
            f.write(title)
        with open(tf_det, "w") as f:
            f.write(detail)
        vf += (f",pad=iw:ih+{header}:0:{header}:color=0x1b1b1b"
               f",drawtext=fontfile={font}:textfile={tf_title}:x=16:y=12:"
               f"fontsize=22:fontcolor=white"
               f",drawtext=fontfile={font}:textfile={tf_det}:x=16:y=42:"
               f"fontsize=16:fontcolor=0xb8b8b8")

    def _attempt(skip_nokey: bool) -> bool:
        cmd = ["ffmpeg", "-y", "-hide_banner"]
        if skip_nokey:
            cmd += ["-skip_frame", "nokey"]
        cmd += ["-i", video_path, "-frames:v", "1", "-vf", vf, "-q:v", "5", out_path]
        res = tools.run(cmd, on_line=on_line, check=False, cancel=cancel)
        return res.returncode == 0 and os.path.exists(out_path)

    try:
        ok = _attempt(True) or _attempt(False)   # retry without -skip_frame (sparse keyframes)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return ok


def _gather(video_dir: str, photos_dir: str) -> tuple[list[str], list[str]]:
    vids: list[str] = []
    for g in _VIDEO_GLOBS:
        vids += glob.glob(os.path.join(video_dir, g))
    photos: list[str] = []
    for g in _PHOTO_GLOBS:
        photos += glob.glob(os.path.join(photos_dir, g))
    return sorted(set(vids)), sorted(set(photos))


def folder_montage(title: str, video_dir: str, photos_dir: str, dest_path: str,
                   cols: int = 4, cell_w: int = 320, cancel=None) -> bool:
    """One labelled overview image of the whole folder: a mid-frame per movie plus
    every photo. Needs ImageMagick `montage`; returns False (skipped) without it."""
    if not tools.has("montage"):
        return False
    vids, photos = _gather(video_dir, photos_dir)
    if not vids and not photos:
        return False

    tmp = tempfile.mkdtemp(prefix="mdvd_montage_")
    items: list[tuple[str, str]] = []   # (image path, label)
    try:
        for i, v in enumerate(vids):
            dur = _probe_duration(v)
            frame = os.path.join(tmp, f"v{i:03d}.png")
            cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", f"{max(0.0, dur / 2):.3f}",
                   "-i", v, "-frames:v", "1", "-vf", f"scale={cell_w}:-1", frame]
            tools.run(cmd, check=False, cancel=cancel)
            if os.path.exists(frame):
                items.append((frame, os.path.basename(v)))
        for p in photos:
            items.append((p, os.path.basename(p)))
        if not items:
            return False

        font = _fontfile()
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
        cmd = ["montage"]
        for img, label in items:
            cmd += ["-label", label, img]
        cmd += ["-tile", f"{cols}x", "-geometry", f"{cell_w}x{cell_w}+8+8",
                "-auto-orient", "-background", "#1b1b1b", "-fill", "#dddddd",
                "-pointsize", "13", "-title", title]
        if font:
            cmd += ["-font", font]
        cmd += [dest_path]
        res = tools.run(cmd, check=False, cancel=cancel)
        return res.returncode == 0 and os.path.exists(dest_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
