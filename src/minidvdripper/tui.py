"""Textual TUI: set the parent folder, insert a disc, press Rip, repeat.

The heavy pipeline runs in a thread worker; its Events callbacks marshal back to
the UI thread via App.call_from_thread. Bit-rot and decode warnings are surfaced
loudly because that's the whole point of the tool.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
)

from . import describe as describe_mod
from . import disc as disc_mod
from . import finalize as fin
from . import icons, notify, power, tools
from .config import Config
from .pipeline import STEPS, Events, Pipeline


class FinalizeModal(ModalScreen):
    """Typed-confirmation dialog before the irreversible lead-out write."""
    CSS = """
    FinalizeModal { align: center middle; }
    #dlg { width: 80; height: auto; border: thick $error; background: $surface;
           padding: 1 2; }
    #dlg Label { width: 100%; }
    #dlg Input { margin: 1 0; }
    #dlgbtns { height: auto; align-horizontal: right; }
    #dlgbtns Button { margin-left: 2; }
    """

    def __init__(self, device: str, info_line: str):
        super().__init__()
        self.device = device
        self.info_line = info_line

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg"):
            yield Label("[b red]Finalize disc — IRREVERSIBLE WRITE[/b red]")
            yield Label(self.info_line)
            yield Label("Writes a lead-out so the disc becomes readable. Does not "
                        "erase recordings. If it fails the disc may be harmed.")
            yield Label(f"Type [b]{self.device}[/b] to confirm:")
            yield Input(id="confirm", placeholder=self.device)
            with Horizontal(id="dlgbtns"):
                yield Button("Cancel", id="cancel")
                yield Button("Finalize", id="go", variant="error")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "cancel":
            self.dismiss(False)
        else:
            self.dismiss(self.query_one("#confirm", Input).value.strip() == self.device)

    def on_input_submitted(self, e: Input.Submitted) -> None:
        self.dismiss(e.value.strip() == self.device)


class SettingsModal(ModalScreen):
    """Toggle ripper options; returns a dict of changes (or None on cancel)."""
    CSS = """
    SettingsModal { align: center middle; }
    #setdlg { width: 72; height: auto; border: thick $accent; background: $surface;
              padding: 1 2; }
    #setdlg Checkbox { width: 100%; }
    #setbtns { height: auto; align-horizontal: right; margin-top: 1; }
    #setbtns Button { margin-left: 2; }
    #set_retries { width: 8; }
    """
    # (config key, label)
    FIELDS = [
        ("eject_when_done", "Eject disc when rip finishes"),
        ("notify_on_done",  "Desktop notification when a rip finishes"),
        ("sound_on_done",   "Play a chime when a rip finishes"),
        ("inhibit_sleep",   "Prevent the computer sleeping during a rip"),
        ("nerd_icons",      "Nerd Font icons (off = plain unicode)"),
        ("contact_sheets",  "Make contact-sheet preview per movie"),
        ("folder_thumbnails", "Make folder overview montage (needs ImageMagick)"),
        ("montage_in_root", "Also copy each overview into the parent folder"),
        ("verify_decode",   "Verify decode (catch corrupt frames)"),
        ("map_subtitles",   "Include DVD subtitle streams"),
        ("stamp_exif",      "Stamp photo EXIF dates (exiftool)"),
        ("keep_iso",        "Keep ISO master (finalized discs)"),
        ("keep_mapfile",    "Keep ddrescue mapfile (rot proof)"),
        ("keep_extracted",  "Keep raw extracted VIDEO_TS/DCIM"),
    ]

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="setdlg"):
            yield Label("[b]Settings[/b]  (saved to ~/.config/minidvdripper/config.json)")
            for key, lbl in self.FIELDS:
                yield Checkbox(lbl, getattr(self.cfg, key), id=f"cb_{key}")
            with Horizontal():
                yield Label("ddrescue retries: ")
                yield Input(str(self.cfg.ddrescue_retries), id="set_retries")
            with Horizontal(id="setbtns"):
                yield Button("Cancel", id="set_cancel")
                yield Button("Save", id="set_save", variant="success")

    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "set_cancel":
            self.dismiss(None)
            return
        changes = {k: self.query_one(f"#cb_{k}", Checkbox).value for k, _ in self.FIELDS}
        try:
            changes["ddrescue_retries"] = max(0, int(self.query_one("#set_retries", Input).value))
        except ValueError:
            pass
        self.dismiss(changes)

class DescribeScreen(ModalScreen):
    """Edit per-clip descriptions/dates for an existing disc folder, then Save the
    sidecar or Apply (embed metadata + rename) — no spreadsheet needed."""
    CSS = """
    DescribeScreen { align: center middle; }
    #dbox { width: 92%; height: 90%; border: thick $accent; background: $surface;
            padding: 1 2; }
    #dhead { height: auto; margin-bottom: 1; }
    #folder_sel { width: 1fr; }
    #rows { height: 1fr; border: round $accent; padding: 0 1; }
    .crow { height: auto; }
    .crow .sess { width: 16; color: $text-muted; }
    .crow .date { width: 14; }
    .crow .desc { width: 1fr; }
    .crow .openbtn { width: 5; }
    #dstatus { height: 1; color: $text-muted; }
    #dbtns { height: auto; align-horizontal: right; margin-top: 1; }
    #dbtns Button { margin-left: 2; }
    """
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._folder = ""
        self._rowsdata: dict = {}
        self._clip: dict = {}

    def _folders(self) -> list[tuple[str, str]]:
        out = []
        parent = Path(self.cfg.parent_dir) if self.cfg.parent_dir else None
        if parent and parent.is_dir():
            for d in sorted(parent.iterdir()):
                if d.is_dir() and (d / "video").is_dir():
                    out.append((d.name, str(d)))
        return out

    def compose(self) -> ComposeResult:
        folders = self._folders()
        with Vertical(id="dbox"):
            yield Label("[b]Describe clips[/b]  ·  pick a folder, type a description, "
                        "press ▶ to watch a clip, then Save or Apply")
            with Horizontal(id="dhead"):
                yield Label("Folder: ")
                if folders:
                    yield Select(folders, id="folder_sel", allow_blank=False,
                                 value=folders[0][1])
                else:
                    yield Label("[dark_orange]No ripped folders under the parent "
                                "folder.[/dark_orange]")
            yield VerticalScroll(id="rows")
            yield Label("", id="dstatus")
            with Horizontal(id="dbtns"):
                yield Button("Open folder", id="open_dir")
                yield Button("Save", id="save_desc")
                yield Button("Apply (rename + tag)", id="apply_desc", variant="success")
                yield Button("Close", id="close_desc")

    def on_mount(self) -> None:
        folders = self._folders()
        if folders:
            self._kick(folders[0][1])

    def _kick(self, folder: str) -> None:
        """Start a (re)load — safe to call from the UI thread."""
        self.run_worker(self._reload(folder), exclusive=True)

    def _status(self, msg: str, level: str = "info") -> None:
        color = _LOGCOLOR.get(level, "white")
        self.query_one("#dstatus", Label).update(f"[{color}]{msg}[/{color}]")

    def _make_row(self, sess: str, dur: str, date: str, desc: str):
        return Horizontal(
            Static(f"{sess} · {dur}", classes="sess"),
            Input(value=date, classes="date", id=f"date_{sess}"),
            Input(value=desc, placeholder="what is this clip? (e.g. Anniversaire Mamie)",
                  classes="desc", id=f"desc_{sess}"),
            Button("▶", id=f"open_{sess}", classes="openbtn"),
            classes="crow")

    async def _reload(self, folder: str) -> None:
        """Scaffold + read the TSV off-thread, then render the rows."""
        self._folder = folder

        def load():
            describe_mod.scaffold_folder(folder)
            data = describe_mod._read_tsv(os.path.join(folder, describe_mod.TSV))
            clips = {describe_mod._session(c) or "": c
                     for c in describe_mod._clips(os.path.join(folder, "video"))}
            return data, clips

        data, clips = await asyncio.to_thread(load)
        await self._populate(data, clips)

    async def _populate(self, data: dict, clips: dict) -> None:
        self._rowsdata, self._clip = data, clips
        cont = self.query_one("#rows", VerticalScroll)
        await cont.remove_children()
        rows = [self._make_row(s, data[s].get("duration", ""),
                               data[s].get("date", ""), data[s].get("description", ""))
                for s in sorted(data)]
        await cont.mount(*(rows or [Static("No clips in this folder.")]))
        self._status(f"{len(data)} clip(s) — {Path(self._folder).name}")

    def _collect(self) -> None:
        rows = []
        for sess in sorted(self._rowsdata):
            old = self._rowsdata[sess]
            try:
                date = self.query_one(f"#date_{sess}", Input).value.strip()
                desc = self.query_one(f"#desc_{sess}", Input).value.strip()
            except Exception:                       # noqa: BLE001
                date, desc = old.get("date", ""), old.get("description", "")
            rows.append({"session": sess, "date": date,
                         "duration": old.get("duration", ""), "description": desc})
        describe_mod._write_tsv(os.path.join(self._folder, describe_mod.TSV), rows)

    def _open(self, path: str) -> None:
        if tools.has("xdg-open") and os.path.exists(path):
            subprocess.Popen(["xdg-open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @work(thread=True)
    def _apply_worker(self) -> None:
        self.app.call_from_thread(self._status, "Applying — embedding metadata + "
                                  "renaming (no re-encode)…", "warn")
        try:
            n = len(describe_mod.apply_folder(self._folder, str(Path(self._folder).parent)))
        except Exception as ex:                     # noqa: BLE001
            self.app.call_from_thread(self._status, f"Apply failed: {ex}", "fail")
            return
        self.app.call_from_thread(self._status, f"Applied — {n} clip(s) renamed + "
                                  "tagged. Montage refreshed.", "ok")
        self.app.call_from_thread(self._kick, self._folder)

    def on_select_changed(self, e: Select.Changed) -> None:
        if e.value and e.value != Select.BLANK:
            self._kick(str(e.value))

    def on_button_pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id or ""
        if bid == "close_desc":
            self.dismiss()
        elif bid == "save_desc":
            self._collect()
            self._status("Saved descriptions.tsv.", "ok")
        elif bid == "apply_desc":
            self._collect()
            self._apply_worker()
        elif bid == "open_dir":
            self._open(self._folder)
        elif bid.startswith("open_"):
            self._open(self._clip.get(bid[5:], ""))

    def action_close(self) -> None:
        self.dismiss()


_LOGCOLOR = {"warn": "dark_orange", "fail": "red", "ok": "green",
             "dim": "grey50", "info": "white"}
# Highlight key=value tokens (media=DVD-ROM, finalized=yes, eject=True…) so the
# headers read as neat labels rather than a run-on line.
_KV = re.compile(r"\b([A-Za-z_][\w-]*)=([^\s,;)]+)")


class MiniDvdApp(App):
    CSS = """
    Screen { layout: vertical; }
    #settings { height: auto; padding: 0 1; }
    #settings Input { width: 1fr; }
    #settings Button { width: auto; }
    #body { height: 1fr; }
    #steps { width: 32; border: round $accent; padding: 0 1; }
    #right { width: 1fr; }
    #log { height: 1fr; border: round $accent; }
    #sessions { height: 12; border: round $accent; }
    .stepline { height: 1; }
    #status { height: auto; padding: 0 1; }
    """
    BINDINGS = [
        ("r", "rip", "Rip disc"),
        ("e", "eject", "Eject"),
        ("d", "detect", "Detect drive"),
        ("f", "finalize", "Finalize DVD-R"),
        ("s", "settings", "Settings"),
        ("x", "cancel", "Cancel rip"),
        ("c", "check", "Check tools"),
        ("n", "describe", "Describe"),
        ("y", "copy_log", "Copy log"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._busy = False
        self._active_pipeline = None
        self._log_buf: list[str] = []     # plain-text mirror of the log, for copy
        self._last_disc = ""              # label of the disc being ripped (for notifications)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="settings"):
            yield Input(value=self.cfg.parent_dir, placeholder="Parent output folder…",
                        id="parent")
            yield Button("Browse", id="browse", variant="primary")
            yield Input(value=self.cfg.device, id="device")
            yield Button("Settings", id="settings")
            yield Button("Describe", id="describe_btn")
            yield Button("RIP", id="rip", variant="success")
            yield Button("Cancel", id="cancel_rip", variant="error", disabled=True)
        with Horizontal(id="body"):
            with Vertical(id="steps"):
                yield Label("[b]Steps[/b]")
                idle_glyph, _ = icons.step_icon("idle", self.cfg.nerd_icons)
                for s in STEPS:
                    yield Static(f"{idle_glyph} {s}", id=f"step-{s}", classes="stepline")
                yield Static("", id="rotline")
            with Vertical(id="right"):
                yield ProgressBar(total=100, show_eta=False, id="prog")
                yield Label("idle", id="proglabel")
                yield RichLog(id="log", highlight=False, markup=True, wrap=True)
                yield DataTable(id="sessions")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "MiniDvdRipper"
        self.sub_title = "Sony Handycam MiniDVD archiver"
        tbl = self.query_one("#sessions", DataTable)
        tbl.add_columns("#", "Date", "Parts", "Size (MB)", "Output")
        self._check_tools()
        self._detect_worker()      # auto-find the drive + report disc on startup

    # ---- logging / ui helpers (called on UI thread) --------------------
    def _log(self, msg: str, level: str = "info") -> None:
        color = _LOGCOLOR.get(level, "white")
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_buf.append(f"{ts}  {msg}")                 # plain text for copy
        if len(self._log_buf) > 5000:
            del self._log_buf[:1000]
        glyph, gcolor = icons.level_icon(level, self.cfg.nerd_icons)
        pretty = _KV.sub(r"[grey62]\1[/grey62]=[b]\2[/b]", msg)
        self.query_one("#log", RichLog).write(
            f"[grey42]{ts}[/grey42] [{gcolor}]{glyph}[/{gcolor}] "
            f"[{color}]{pretty}[/{color}]")

    def _disc_card(self, card: dict) -> None:
        """Render the disc summary as labelled, background-coloured chips:
        device · media · finalized/unfinalized · size · label."""
        self._last_disc = card.get("label", "")
        nerd = self.cfg.nerd_icons

        def fi(name: str) -> str:
            return icons.field_icon(name, nerd)

        chips = [icons.chip(card["device"], "white", "#3a3a3a", fi("device")),
                 icons.chip(card["media"], "white", "#1f4e79", fi("media"))]
        plain = [icons.chip_plain(card["device"]), icons.chip_plain(card["media"])]
        if card.get("finalized"):
            chips.append(icons.chip("finalized", "black", "#2e7d32", fi("ok")))
            plain.append(icons.chip_plain("finalized"))
        elif card.get("kind") == "unfinalized":
            chips.append(icons.chip("unfinalized", "black", "#c25e00", fi("warn")))
            plain.append(icons.chip_plain("unfinalized"))
        if card.get("size_mb"):
            sz = f"{card['size_mb']:.0f} MB"
            chips.append(icons.chip(sz, "white", "#3a3a3a", fi("size")))
            plain.append(icons.chip_plain(sz))
        if card.get("label"):
            chips.append(icons.chip(card["label"], "white", "#3a3a3a", fi("label")))
            plain.append(icons.chip_plain(card["label"]))
        ts = datetime.now().strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[grey42]{ts}[/grey42]  " + "  ".join(chips))
        self._log_buf.append(f"{ts}  " + "  ".join(plain))

    def action_copy_log(self) -> None:
        """Copy the whole log to the clipboard (OSC-52). For partial copy, drag to
        select text in the log with the mouse — Textual selection is on."""
        text = "\n".join(self._log_buf)
        if not text:
            return
        try:
            self.copy_to_clipboard(text)
            self._log(f"Copied {len(self._log_buf)} log line(s) to clipboard.", "ok")
        except Exception:                                    # noqa: BLE001
            self._log("Copy failed — terminal may not support OSC-52 clipboard.", "warn")

    def _set_step(self, name: str, state: str) -> None:
        try:
            w = self.query_one(f"#step-{name}", Static)
        except Exception:
            return
        glyph, color = icons.step_icon(state, self.cfg.nerd_icons)
        w.update(f"[{color}]{glyph} {name}[/{color}]")

    def _reset_steps(self) -> None:
        for s in STEPS:
            self._set_step(s, "idle")
        self.query_one("#sessions", DataTable).clear()
        self.query_one("#rotline", Static).update("")
        bar = self.query_one("#prog", ProgressBar)
        bar.update(total=100, progress=0)

    def _set_progress(self, label: str | None, frac) -> None:
        if label:
            self.query_one("#proglabel", Label).update(label)
        if frac is not None:
            self.query_one("#prog", ProgressBar).update(progress=frac * 100)

    def _set_titles(self, titles) -> None:
        tbl = self.query_one("#sessions", DataTable)
        tbl.clear()
        for t in titles:
            tbl.add_row(f"{t.number:02d}", t.date_tag, str(len(t.parts)),
                        f"{t.size_bytes/1e6:.0f}", t.out_name())

    def _set_rot(self, text: str, warn: bool) -> None:
        c = "dark_orange" if warn else "green"
        self.query_one("#rotline", Static).update(f"[{c}]{text}[/{c}]")

    # ---- actions -------------------------------------------------------
    def _check_tools(self) -> None:
        pf = tools.preflight()
        for line in pf.summary().splitlines():
            self._log(line, "warn" if "MISSING" in line else "dim")
        if not pf.ok:
            self._log("Required tools missing — install them before ripping.", "fail")

    def action_check(self) -> None:
        self._check_tools()

    def action_detect(self) -> None:
        """Scan /dev/sr* for a disc and report what's on it (blank / unfinalized
        with data / finalized & ready). Also fixes the device path after replug."""
        self._log("Scanning optical drives…", "dim")
        self._detect_worker()

    @work(thread=True)
    def _detect_worker(self) -> None:
        devs = disc_mod.list_optical()
        if not devs:
            self.call_from_thread(self._log, "No /dev/sr* optical drive found.", "fail")
            return
        lines = []
        chosen = None
        chosen_info = None
        for d in devs:
            info = disc_mod.identify(d)
            if not info.media_present:
                lines.append((f"{d}: empty / no disc", "dim"))
                continue
            if chosen is None:
                chosen, chosen_info = d, info
            lines.append((f"{d}: {info.scan_line()}", info.scan_level()))
        if chosen:
            self.call_from_thread(self._apply_device, chosen)
        for msg, lvl in lines:
            self.call_from_thread(self._log, msg, lvl)
        if chosen_info is not None and chosen_info.kind == "unfinalized":
            self.call_from_thread(self._log,
                "Disc is UNFINALIZED but RECOVERABLE — press RIP to read its tracks "
                "directly (no finalize needed). Still photos are carved from the raw "
                "tracks too, where present.", "warn")

    def _apply_device(self, dev: str) -> None:
        self.query_one("#device", Input).value = dev
        self.cfg.device = dev

    # ---- finalize (irreversible) --------------------------------------
    def action_finalize(self) -> None:
        if self._busy:
            return
        dev = self.query_one("#device", Input).value.strip() or "/dev/sr0"
        self._log(f"Checking {dev} for finalize eligibility…", "dim")
        self._finalize_check_worker(dev)

    @work(thread=True)
    def _finalize_check_worker(self, dev: str) -> None:
        info = disc_mod.identify(dev)
        chk = fin.can_finalize(info)
        if not chk.ok:
            self.call_from_thread(self._log, f"Cannot finalize: {chk.reason}", "warn")
            return
        self.call_from_thread(self._offer_finalize, dev, info.scan_line(), False)

    def _offer_finalize(self, dev: str, info_line: str, then_rip: bool = False) -> None:
        """Pop the typed-confirm finalize dialog. If then_rip, rip after success."""
        if self._busy or isinstance(self.screen, FinalizeModal):
            return
        def after(confirmed: bool) -> None:
            if confirmed:
                self._log(f"Finalizing {dev}… do not remove the disc.", "warn")
                self._busy = True
                self.query_one("#rip", Button).disabled = True
                self._finalize_worker(dev, then_rip)
            else:
                self._log("Finalize cancelled — disc left untouched.", "dim")
        self.push_screen(FinalizeModal(dev, info_line), after)

    @work(thread=True)
    def _finalize_worker(self, dev: str, then_rip: bool = False) -> None:
        ok = fin.finalize_disc(dev, on_line=lambda l: self.call_from_thread(self._log, l, "dim"))
        self.call_from_thread(self._finalize_done, dev, ok, then_rip)

    def _finalize_done(self, dev: str, ok: bool, then_rip: bool = False) -> None:
        self._busy = False
        self.query_one("#rip", Button).disabled = False
        if not ok:
            self._log("Finalize FAILED. Try another drive.", "fail")
            return
        info = disc_mod.identify(dev)
        self._log(f"Finalized. Disc now: {info.scan_line()}", "ok")
        if then_rip:
            self._log("Disc finalized — starting rip…", "ok")
            self._begin_rip()
        elif info.kind == "ready":
            self._log("Press RIP to archive it.", "ok")

    def action_eject(self) -> None:
        dev = self.query_one("#device", Input).value
        if self._busy and self._active_pipeline is not None:
            self._log("Eject — cancelling current job first…", "warn")
            self.query_one("#cancel_rip", Button).disabled = True
            self._active_pipeline.cancel()
        if tools.has("eject"):
            subprocess.Popen(["eject", dev])
            self._log(f"Ejecting {dev}…", "dim")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "browse":
            self._browse()
        elif event.button.id == "rip":
            self.action_rip()
        elif event.button.id == "settings":
            self.action_settings()
        elif event.button.id == "cancel_rip":
            self.action_cancel()
        elif event.button.id == "describe_btn":
            self.action_describe()

    def action_cancel(self) -> None:
        if not self._busy or self._active_pipeline is None:
            return
        self._log("Cancelling — killing current step…", "warn")
        self.query_one("#cancel_rip", Button).disabled = True
        self._active_pipeline.cancel()

    def action_settings(self) -> None:
        def saved(changes) -> None:
            if not changes:
                return
            for k, v in changes.items():
                setattr(self.cfg, k, v)
            self.cfg.save()
            self._log(f"Settings saved (eject={self.cfg.eject_when_done}, "
                      f"contact={self.cfg.contact_sheets}, verify={self.cfg.verify_decode}).", "ok")
        self.push_screen(SettingsModal(self.cfg), saved)

    def action_describe(self) -> None:
        if self._busy:
            self._log("Wait for the rip to finish before describing.", "warn")
            return
        parent = self.query_one("#parent", Input).value.strip()
        if parent and Path(parent).is_dir():
            self.cfg.parent_dir = str(Path(parent).resolve())
        if not self.cfg.parent_dir or not Path(self.cfg.parent_dir).is_dir():
            self._log("Set a valid parent output folder first.", "warn")
            return
        self.push_screen(DescribeScreen(self.cfg))

    @work(thread=True)
    def _browse(self) -> None:
        try:
            out = subprocess.run(
                ["zenity", "--file-selection", "--directory",
                 "--title=Select parent output folder"],
                capture_output=True, text=True)
            path = out.stdout.strip()
        except FileNotFoundError:
            path = ""
        if path:
            self.call_from_thread(self._apply_parent, path)

    def _apply_parent(self, path: str) -> None:
        self.query_one("#parent", Input).value = path

    def action_rip(self) -> None:
        if self._busy:
            self._log("Already ripping — wait for it to finish.", "warn")
            return
        pf = tools.preflight()
        if not pf.ok:
            self._log("Cannot rip: required tools missing.", "fail")
            return
        parent = self.query_one("#parent", Input).value.strip()
        if not parent or not Path(parent).is_dir():
            self._log(f"Parent folder not found: {parent!r}", "fail")
            return
        self.cfg.parent_dir = str(Path(parent).resolve())
        self.cfg.device = self.query_one("#device", Input).value.strip() or "/dev/sr0"
        self.cfg.save()
        # Pre-check the disc: if unfinalized, offer to finalize first, then rip.
        self._log(f"Checking disc in {self.cfg.device}…", "dim")
        self._rip_precheck_worker(self.cfg.device)

    @work(thread=True)
    def _rip_precheck_worker(self, dev: str) -> None:
        info = disc_mod.identify(dev)
        if info.kind in ("ready", "unfinalized"):
            # Unfinalized discs are recovered track-by-track inside the pipeline —
            # no finalize needed.
            if info.kind == "unfinalized":
                self.call_from_thread(self._log,
                    "Unfinalized disc — will recover recorded tracks directly "
                    "(no finalize needed).", "warn")
            self.call_from_thread(self._begin_rip)
        else:
            self.call_from_thread(self._log, info.blocker or "Cannot rip this disc.", "fail")

    def _begin_rip(self) -> None:
        if self._busy:
            return
        self._reset_steps()
        self._busy = True
        self.query_one("#rip", Button).disabled = True
        self.query_one("#cancel_rip", Button).disabled = False
        self._log(f"--- Ripping from {self.cfg.device} into {self.cfg.parent_dir} ---", "ok")
        self._rip_worker()

    @work(thread=True)
    def _rip_worker(self) -> None:
        ev = Events(
            log=lambda m, l="info": self.call_from_thread(self._log, m, l),
            step=lambda n, s: self.call_from_thread(self._set_step, n, s),
            progress=lambda lbl, f: self.call_from_thread(self._set_progress, lbl, f),
            titles_found=lambda ts: self.call_from_thread(self._set_titles, ts),
            disc_card=lambda c: self.call_from_thread(self._disc_card, c),
        )
        pipe = Pipeline(self.cfg, ev)
        self._active_pipeline = pipe
        try:
            with power.Inhibitor(f"MiniDvdRipper ripping {self.cfg.device}",
                                 enabled=self.cfg.inhibit_sleep) as inh:
                if inh.active:
                    self.call_from_thread(self._log, "Sleep inhibited for the rip "
                                          "(machine stays awake).", "dim")
                rep = pipe.run_disc()
            self.call_from_thread(self._done, rep, None)
        except tools.Cancelled:
            self.call_from_thread(self._done, None, "cancelled")
        except Exception as e:                       # noqa: BLE001
            self.call_from_thread(self._done, None, e)
        finally:
            self._active_pipeline = None

    def _done(self, rep, error) -> None:
        self._busy = False
        self.query_one("#rip", Button).disabled = False
        self.query_one("#cancel_rip", Button).disabled = True
        if error == "cancelled":
            self._log("RIP CANCELLED — partial files left in the disc folder.", "warn")
            self._notify_done("cancelled")
            return
        if error:
            self._log(f"RIP FAILED: {error}", "fail")
            self._notify_done("fail", str(error))
            return
        rot = rep["bit_rot"]
        if rot["clean"]:
            self._set_rot("✔ clean rip (100%)", warn=False)
            self._log("DONE — clean rip. Insert next disc and press RIP.", "ok")
            self._notify_done("ok")
        else:
            self._set_rot(f"▲ {rot['bad_bytes']:,} B lost", warn=True)
            self._log(f"DONE WITH BIT ROT — {rot['bad_bytes']:,} bytes unrecoverable. "
                      f"See _master/report.txt.", "warn")
            self._notify_done("rot", f"{rot['bad_bytes']:,} bytes unrecoverable")

    def _notify_done(self, kind: str, detail: str = "") -> None:
        """Desktop notification + chime when a rip ends (toggle in Settings).
        Falls back to the terminal bell if no sound player is available."""
        title, urgency, event = {
            "ok":        ("✓ Rip complete", "normal", "complete"),
            "rot":       ("Rip done — BIT ROT", "critical", "warning"),
            "fail":      ("Rip FAILED", "critical", "error"),
            "cancelled": ("Rip cancelled", "low", None),
        }[kind]
        body = " — ".join(p for p in (self._last_disc, detail) if p)
        if self.cfg.notify_on_done:
            notify.desktop_notify(title, body, urgency)
        if self.cfg.sound_on_done and event:
            if not notify.play_sound(event):
                self.bell()                       # terminal bell fallback


def run_tui(cfg: Config) -> int:
    MiniDvdApp(cfg).run()
    return 0
