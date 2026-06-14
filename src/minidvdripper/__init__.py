"""MiniDvdRipper — lossless, bit-rot-aware archival ripper for Sony Handycam MiniDVDs.

Pipeline per disc:
  dvd+rw-mediainfo  -> identify disc, check finalization
  ddrescue          -> bit-exact ISO + mapfile (records every unreadable sector)
  bsdtar            -> extract VIDEO_TS / DCIM from the ISO (rootless, keeps mtimes)
  ffmpeg -c copy    -> one lossless MKV per recording session (no re-encode)
  exiftool/os.utime -> stamp per-session capture dates for Synology Photos
  report            -> JSON + human summary incl. exact bit-rot map
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
