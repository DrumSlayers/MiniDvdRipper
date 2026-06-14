"""End-to-end rip of one disc, emitting events the TUI/CLI render.

Steps:  Identify -> Image(ddrescue) -> Extract(bsdtar) -> Remux(ffmpeg) ->
        Photos -> Report -> Eject
Each step reports state via the Events callbacks; heavy tools stream their output
to the log and, where possible, a 0..1 progress fraction.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import carve, contact, imaging, isofs, photos, recover, remux, report, stills, tools
from . import disc as disc_mod
from . import titles as titles_mod
from .config import Config

STEPS = ["Identify", "Image", "Extract", "Remux", "Photos", "Report", "Eject"]

_FFTIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

# ddrescue verbose status fields
_DD_PCT = re.compile(r"pct rescued:\s*([\d.]+)%")
_DD_RESCUED = re.compile(r"rescued:\s*([\d.]+\s*[kKMGTP]?i?B)\b")  # unit guard vs "pct rescued"
_DD_RATE = re.compile(r"current rate:\s*([\d.]+\s*\w+/s)")
_DD_BAD = re.compile(r"bad areas:\s*(\d+)")
_DD_REM = re.compile(r"remaining time:\s*([\dhms ]+?)\s*$")
_DD_NONTRIED = re.compile(r"non-tried:\s*([\d.]+\s*[kKMGTP]?i?B)\b")  # not-yet-read = skipped
_DD_PHASES = ("Copying non-tried", "Trimming", "Scraping",
              "Retrying", "Finished")

# If the rescued total stops growing this long, the drive is grinding an
# unreadable band — surfaced live and logged once so a stall reads as "stuck on
# rot, safe to cancel", not "frozen/hung".
STALL_SECS = 90


# step states: run / done / warn / fail / skip
@dataclass
class Events:
    log: Callable[[str, str], None] = lambda msg, level="info": None
    step: Callable[[str, str], None] = lambda name, state: None
    progress: Callable[[str, float | None], None] = lambda label, frac: None
    titles_found: Callable[[list], None] = lambda titles: None
    disc_card: Callable[[dict], None] = lambda card: None


class RipError(Exception):
    pass


class Pipeline:
    def __init__(self, cfg: Config, events: Events | None = None):
        self.cfg = cfg
        self.ev = events or Events()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Request abort: kills the running subprocess and stops between steps."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _ck(self) -> None:
        if self._cancel.is_set():
            raise tools.Cancelled()

    # -- helpers ----------------------------------------------------------
    def _log(self, msg, level="info"):
        self.ev.log(msg, level)

    def _ff_progress(self, label, total):
        def cb(line: str):
            if total > 0:
                m = _FFTIME.search(line)
                if m:
                    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    self.ev.progress(label, min(1.0, secs / total))
        return cb

    def _dd_progress(self, label):
        """Parse ddrescue's verbose blocks into ONE compact live line + a
        progress fraction. Don't spam the log with every status tick; only log
        when ddrescue switches phase (Copying -> Trimming -> Scraping ...).

        Surfaces two things the raw status hides:
          * skipped/unread bytes ("non-tried"), so you can see there's data left
            that hasn't been attempted yet;
          * a STALL — if the rescued total stops growing for STALL_SECS the drive
            is grinding an unreadable band. Shown inline (⚠ STALLED Nm) and logged
            once, so a long quiet stretch reads as "stuck on rot, safe to cancel",
            not "is it hung?".
        """
        st = {"rescued": "?", "rate": "?", "rem": "?", "bad": "0", "skip": ""}
        last_phase = [""]
        last_rescued = [""]
        last_grow = [time.monotonic()]
        stalled = [False]

        def cb(line: str):
            for ph in _DD_PHASES:
                if line.lstrip().startswith(ph) and ph != last_phase[0]:
                    last_phase[0] = ph
                    self._log(f"  ddrescue: {line.strip().rstrip('.')}", "dim")
                    break
            if (m := _DD_RESCUED.search(line)):
                st["rescued"] = m.group(1).replace("  ", " ")
            if (m := _DD_RATE.search(line)):
                st["rate"] = m.group(1).replace("  ", " ")
            if (m := _DD_BAD.search(line)):
                st["bad"] = m.group(1)
            if (m := _DD_REM.search(line)):
                st["rem"] = m.group(1).strip()
            if (m := _DD_NONTRIED.search(line)):
                st["skip"] = m.group(1).replace("  ", " ")
            if (m := _DD_PCT.search(line)):           # end of a status block
                pct = float(m.group(1))
                now = time.monotonic()
                if st["rescued"] != last_rescued[0]:   # rescued total grew -> alive
                    last_rescued[0] = st["rescued"]
                    last_grow[0] = now
                    if stalled[0]:
                        stalled[0] = False
                        self._log("  ddrescue: reading again — new data recovered.", "ok")
                stall_s = now - last_grow[0]
                tail = ""
                if st["skip"] and st["skip"][0] != "0":
                    tail += f"  ·  {st['skip']} unread"
                if stall_s >= STALL_SECS:
                    tail += f"  ·  ⚠ STALLED {stall_s/60:.0f}m"
                    if not stalled[0]:
                        stalled[0] = True
                        self._log(
                            f"No new data for {stall_s/60:.0f} min — the drive is grinding "
                            f"an unreadable band ({st['bad']} bad areas). It is NOT hung; "
                            "it's retrying rotted sectors. Safe to cancel (x) — resume "
                            "continues from here.", "warn")
                status = (f"{label}  {pct:5.1f}%  ·  {st['rescued']}  ·  "
                          f"{st['rate']}  ·  ~{st['rem']} left  ·  {st['bad']} bad{tail}")
                self.ev.progress(status, min(1.0, pct / 100.0))
        return cb

    # -- main -------------------------------------------------------------
    def run_disc(self, from_iso: str | None = None) -> dict:
        started = datetime.now().isoformat(timespec="seconds")
        dev = self.cfg.device

        # 1. Identify --------------------------------------------------
        self.ev.step("Identify", "run")
        if from_iso:
            info = disc_mod.DiscInfo()
            info.label = disc_mod.volume_label(from_iso)
            info.datetime = disc_mod.parse_label_datetime(info.label)
            self._log(f"Using existing ISO: {from_iso}")
        else:
            info = disc_mod.identify(dev, on_line=lambda l: self._log(l, "dim"))
            # Guard BEFORE creating any folder: no media / unfinalized-unreadable.
            blocker = info.blocker
            if blocker:
                self.ev.step("Identify", "fail")
                raise RipError(blocker)
        unfinalized = (not from_iso) and (not info.finalized) and info.recorded_tracks > 0
        # 24-hour folder/title name (Sony's label is 12h + AM/PM; clip files are
        # 24h — keep them consistent). An unfinalized disc carries no volume label,
        # so fall back to the rip date in the same format, and tag the folder
        # `_recovered` so it's clearly distinct from a normally-finalized disc.
        label = disc_mod.folder_label(info.label) or \
            datetime.now().strftime("%Y_%m_%d_%HH%MM")
        if unfinalized:
            label += "_recovered"
        self._log(f"Disc: {label!r}  media={info.media or '?'}  "
                  f"finalized={'yes' if info.finalized else 'NO'}")
        # Structured summary the TUI renders as labelled chips (device / media /
        # status / size / label) — same facts, neater than the run-on line above.
        size_b = info.capacity_bytes or getattr(info, "recorded_bytes", 0) or 0
        self.ev.disc_card({
            "device": (from_iso or dev),
            "media": info.media or "?",
            "finalized": bool(info.finalized),
            "kind": getattr(info, "kind", ""),
            "size_mb": size_b / 1e6,
            "label": label,
        })
        for w in info.warnings:
            self._log(w, "warn")
        self.ev.step("Identify", "warn" if info.warnings else "done")

        # output layout
        out_root = Path(self.cfg.parent_dir) / _safe(label)
        video_dir = out_root / "video"
        photos_dir = out_root / "photos"
        contact_dir = out_root / "contact"
        master_dir = out_root / "_master"
        extracted_dir = master_dir / "_extracted"
        dirs = [video_dir, photos_dir, master_dir]
        if self.cfg.contact_sheets:
            dirs.append(contact_dir)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        iso_path = from_iso or str(master_dir / "disc.iso")
        map_path = str(master_dir / "disc.map")
        self._log(f"Output folder: {out_root}")

        if not from_iso:
            (master_dir / "mediainfo.txt").write_text(info.raw)

        carve_dir = master_dir / "_carve"
        recover_dir = master_dir / "_recover"
        photos_possible = True
        self._ck()

        carved_stills = None
        if unfinalized:
            # 2'. Recover unfinalized disc by reading complete tracks directly.
            # No ddrescue-whole-disc (the empty reserved track reads at ~1 kB/s),
            # no finalize, no second drive.
            self.ev.step("Image", "run")
            self._log("Unfinalized disc — recovering recorded tracks directly off "
                      f"{dev} (no finalize needed).", "warn")
            if recover_dir.exists():
                shutil.rmtree(recover_dir, ignore_errors=True)
            ttls, bad_blocks = recover.recover_unfinalized(
                dev, info.track_list, str(recover_dir), fallback_dt=info.datetime,
                on_progress=lambda lbl, f: self.ev.progress(lbl, f),
                cancel=self._cancel)
            for i, t in enumerate(ttls, 1):
                t.number = i
            self.ev.progress("Recover", 1.0)
            total = sum(t.size_bytes for t in ttls) or 1
            rot = imaging.RotReport(total_bytes=total,
                                    recovered_bytes=max(0, total - bad_blocks * 2048),
                                    bad_bytes=bad_blocks * 2048)
            affected = []
            self._log(rot.summary(), "warn" if not rot.clean else "ok")
            self.ev.step("Image", "warn" if not rot.clean else "done")
            self.ev.step("Extract", "skip")
            photos_possible = False
            # No DCIM filesystem on an unfinalized disc, but the JPEG bytes are in a
            # small closed track — carve them out (PhotoRec-style).
            self._ck()
            blobs = recover.read_small_tracks(dev, info.track_list, str(recover_dir),
                                              cancel=self._cancel)
            carved_stills = stills.carve_stills(blobs, str(photos_dir),
                                                fallback_dt=info.datetime)
            if not ttls:
                self._log("No recordable tracks recovered from this disc.", "warn")
        else:
            # 2. Image (ddrescue) -------------------------------------
            self.ev.step("Image", "run")
            if from_iso:
                sidecar = from_iso + ".map"
                rot = imaging.parse_mapfile(sidecar if os.path.exists(sidecar) else map_path)
                self._log("Skipping ddrescue (using supplied ISO).")
            else:
                # Resume: a prior interrupted rip leaves disc.iso + disc.map here
                # (kept by default; cancel skips cleanup). ddrescue reads the map,
                # skips every recovered sector and continues. Announce it so it's
                # visibly a resume, not a restart-from-zero.
                if os.path.exists(iso_path) and os.path.exists(map_path):
                    prev = imaging.parse_mapfile(map_path)
                    extra = (f", {len(prev.bad_ranges)} bad area(s) still to retry"
                             if prev.bad_ranges else "")
                    self._log(
                        f"Resuming — found a previous image: {prev.recovered_pct:.1f}% "
                        f"already read{extra}. ddrescue continues from here "
                        "(good sectors are skipped).", "ok")
                self._log("ddrescue pass 1/2 (fast) then 2/2 (retry bad sectors)…")
                rot = imaging.ddrescue_image(dev, iso_path, map_path,
                                             retries=self.cfg.ddrescue_retries,
                                             on_line=self._dd_progress("Image"),
                                             cancel=self._cancel)
            self.ev.progress("Image", 1.0)
            iso_size = os.path.getsize(iso_path) if os.path.exists(iso_path) else 0
            if iso_size == 0:
                self.ev.step("Image", "fail")
                raise RipError(
                    f"ddrescue imaged 0 bytes from {dev} — disc unreadable "
                    "(empty tray, or unfinalized disc exposing no data).")
            self._log(rot.summary(), "warn" if not rot.clean else "ok")
            self.ev.step("Image", "warn" if not rot.clean else "done")

            # 3. Extract (bsdtar) -------------------------------------
            self.ev.step("Extract", "run")
            if extracted_dir.exists():
                shutil.rmtree(extracted_dir, ignore_errors=True)
            isofs.extract_iso(iso_path, str(extracted_dir), cancel=self._cancel)
            iso_files = isofs.list_files(iso_path)
            affected = isofs.map_rot_to_files(iso_files, rot.bad_ranges)
            for f in affected:
                self._log(f"ROT in {f.path}: {f.bad_sectors} sector(s) unreadable", "warn")
            self.ev.step("Extract", "warn" if affected else "done")

            # 4. Titles ----------------------------------------------
            ttls = titles_mod.scan_titles(
                str(extracted_dir), fallback_dt=info.datetime,
                on_skip=lambda t: self._log(
                    f"Skipped placeholder title ({t.size_bytes/1024:.0f} KB, "
                    f"{t.date_tag}) — disc-finalization artifact, not a recording", "dim"))
            titles_mod.enrich_chapters(ttls, str(extracted_dir))
            if not ttls and info.track_list:
                # No readable VIDEO_TS (e.g. a PC-finalized DVD-R): carve recorded
                # tracks straight out of the image using the drive's track table.
                self._log("No VIDEO_TS — carving recorded tracks directly from the image…",
                          "warn")
                ttls = carve.slice_tracks(iso_path, info.track_list, str(carve_dir),
                                          fallback_dt=info.datetime)
                for i, t in enumerate(ttls, 1):
                    t.number = i
                if ttls:
                    self._log(f"Carved {len(ttls)} recording(s) from raw tracks.", "ok")

        # 5. Remux -----------------------------------------------------
        self.ev.step("Remux", "run")
        for t in ttls:                       # fill duration so the table can show it
            if t.duration <= 0:
                t.duration = sum(remux.probe_duration(p) for p in t.parts)
        self.ev.titles_found(ttls)
        if not ttls:
            self._log("No VIDEO_TS titles, VR_MOVIE.VRO, or carvable tracks found.", "warn")
        remuxes = []
        for t in ttls:
            self._ck()
            out = str(video_dir / t.out_name())
            self._log(f"Session {t.number:02d} -> {t.out_name()} "
                      f"({len(t.parts)} VOB part(s), {t.size_bytes/1e6:.0f} MB)")
            total = remux.probe_duration(t.parts[0]) if len(t.parts) == 1 else 0.0
            r = remux.remux_title(
                t, out, label,
                map_subtitles=self.cfg.map_subtitles,
                verify=self.cfg.verify_decode,
                on_line=self._ff_progress("Remux", total),
                cancel=self._cancel,
            )
            if not r.ok:
                self._log(f"  FAILED: {r.note}", "fail")
            elif r.decode_errors:
                self._log(f"  remuxed but {r.decode_errors} decode error(s) "
                          f"(likely from rot)", "warn")
            else:
                self._log(f"  ok — {r.duration:.0f}s, {r.streams}", "ok")
            # storyboard preview JPEG
            if self.cfg.contact_sheets and r.ok and r.duration > 0:
                cj = str(contact_dir / (Path(t.out_name()).stem + ".jpg"))
                if contact.contact_sheet(out, cj, r.duration, cancel=self._cancel):
                    self._log(f"  contact sheet -> contact/{os.path.basename(cj)}", "dim")
            remuxes.append(r)
        self.ev.progress("Remux", 1.0)
        any_fail = any(not r.ok for r in remuxes)
        any_warn = any(r.decode_errors for r in remuxes if r.ok)
        self.ev.step("Remux", "fail" if any_fail else ("warn" if any_warn else "done"))

        # 6. Photos ----------------------------------------------------
        if not photos_possible:
            # unfinalized: stills were carved from the raw tracks (no filesystem)
            carved = carved_stills or []
            ph = photos.PhotoResult(copied=carved, count=len(carved))
            if carved:
                self._log(f"Carved {len(carved)} still photo(s) from raw tracks "
                          "-> photos/", "ok")
                self.ev.step("Photos", "done")
            else:
                self._log("No still photos found in the readable tracks "
                          "(none on this disc, or in an unreadable area).", "dim")
                self.ev.step("Photos", "skip")
        else:
            self.ev.step("Photos", "run")
            ph = photos.copy_photos(str(extracted_dir), str(photos_dir),
                                    fallback_dt=info.datetime)
            self._log(f"Copied {ph.count} photo(s)/clip(s) to photos/")
            self.ev.step("Photos", "done")

        # 6b. Folder overview montage (one image of every movie + photo) ----
        if self.cfg.folder_thumbnails:
            self._ck()
            dest = str(out_root / f"{_safe(label)}_thumbnails.jpg")
            if contact.folder_montage(label, str(video_dir), str(photos_dir), dest,
                                      cancel=self._cancel):
                self._log(f"Folder overview -> {os.path.basename(dest)}", "ok")
                # Also drop a copy in the parent so every disc's overview is
                # browsable in one place. A real copy (not a symlink) so it syncs
                # through Synology Drive and opens in any viewer.
                if self.cfg.montage_in_root:
                    root_copy = Path(self.cfg.parent_dir) / os.path.basename(dest)
                    try:
                        shutil.copy2(dest, root_copy)
                        self._log(f"Overview also copied to parent: {root_copy.name}", "dim")
                    except OSError as e:
                        self._log(f"Could not copy overview to parent: {e}", "warn")
            elif not tools.has("montage"):
                self._log("Folder overview skipped — install ImageMagick (montage) for it.",
                          "dim")

        # 6. Report ----------------------------------------------------
        self.ev.step("Report", "run")
        finished = datetime.now().isoformat(timespec="seconds")
        rep = report.build(info, rot, ttls, remuxes, ph, affected,
                           tools.tool_versions(), started, finished,
                           with_hashes=True)
        report.write(rep, str(master_dir / "report.json"), str(master_dir / "report.txt"))
        self._log(f"Report written to {master_dir/'report.txt'}", "ok")
        self.ev.step("Report", "done")

        # cleanup
        if not self.cfg.keep_extracted:
            shutil.rmtree(extracted_dir, ignore_errors=True)
            shutil.rmtree(carve_dir, ignore_errors=True)
            shutil.rmtree(recover_dir, ignore_errors=True)
        if not self.cfg.keep_iso and not from_iso:
            _rm(iso_path)
        if not self.cfg.keep_mapfile and not from_iso:
            _rm(map_path)

        # 7. Eject -----------------------------------------------------
        if self.cfg.eject_when_done and not from_iso and tools.has("eject"):
            self.ev.step("Eject", "run")
            tools.run(["eject", dev], check=False)
            self.ev.step("Eject", "done")
        else:
            self.ev.step("Eject", "skip")

        return rep


def _safe(name: str) -> str:
    name = name.strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^\w.\- ]", "_", name)
    return name or "DISC"


def _rm(path: str):
    try:
        os.remove(path)
    except OSError:
        pass
