"""Command-line entry point. Launches the TUI by default; --cli runs headless."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, tools
from .config import Config
from .pipeline import Events, Pipeline


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mdvdrip",
        description="Lossless, bit-rot-aware ripper for Sony Handycam MiniDVDs.")
    p.add_argument("--parent", metavar="DIR",
                   help="parent folder where a per-disc folder is created")
    p.add_argument("--device", default=None, help="optical device (default /dev/sr0)")
    p.add_argument("--from-iso", metavar="ISO",
                   help="skip ddrescue; rip from an existing ISO (re-extract/testing)")
    p.add_argument("--cli", action="store_true", help="headless mode (no TUI)")
    p.add_argument("--loop", action="store_true",
                   help="(cli) after each disc, prompt to insert the next one")
    p.add_argument("--no-eject", action="store_true")
    p.add_argument("--no-iso", action="store_true", help="don't keep the ISO master")
    p.add_argument("--keep-extracted", action="store_true")
    p.add_argument("--no-verify", action="store_true", help="skip decode verification pass")
    p.add_argument("--no-contact", action="store_true",
                   help="skip the per-movie storyboard JPEG")
    p.add_argument("--retries", type=int, default=None, help="ddrescue retries (default 3)")
    p.add_argument("--finalize", action="store_true",
                   help="write a lead-out to an UNFINALIZED DVD-R so it becomes "
                        "readable (replaces the camcorder's Finalize). IRREVERSIBLE; "
                        "asks for typed confirmation.")
    p.add_argument("--scan", action="store_true",
                   help="triage mode: report each disc (blank / has-data-unfinalized / "
                        "ready) and eject, without ripping. Loops for a stack of discs.")
    p.add_argument("--check", action="store_true", help="print tool preflight and exit")
    p.add_argument("--version", action="version", version=f"minidvdripper {__version__}")
    return p


def cfg_from_args(args) -> Config:
    cfg = Config.load()
    if args.parent:
        cfg.parent_dir = str(Path(args.parent).expanduser().resolve())
    if args.device:
        cfg.device = args.device
    if args.no_eject:
        cfg.eject_when_done = False
    if args.no_iso:
        cfg.keep_iso = False
    if args.keep_extracted:
        cfg.keep_extracted = True
    if args.no_verify:
        cfg.verify_decode = False
    if args.no_contact:
        cfg.contact_sheets = False
    if args.retries is not None:
        cfg.ddrescue_retries = args.retries
    return cfg


def _headless_events() -> Events:
    icons = {"run": "…", "done": "✓", "warn": "!", "fail": "✗", "skip": "·"}
    levelc = {"warn": "\033[33m", "fail": "\033[31m", "ok": "\033[32m",
              "dim": "\033[2m", "info": ""}

    def log(msg, level="info"):
        c = levelc.get(level, "")
        end = "\033[0m" if c else ""
        print(f"{c}{msg}{end}")

    def step(name, state):
        print(f"\033[1m[{icons.get(state,'?')}] {name}\033[0m")

    def progress(label, frac):
        if frac is not None:
            bar = int(frac * 30)
            sys.stdout.write(f"\r    {label} [{'#'*bar}{'.'*(30-bar)}] {frac*100:5.1f}%")
            sys.stdout.flush()
            if frac >= 1.0:
                sys.stdout.write("\n")

    return Events(log=log, step=step, progress=progress)


def run_headless(cfg: Config, from_iso=None, loop=False) -> int:
    pf = tools.preflight()
    if not pf.ok:
        print(pf.summary())
        return 2
    if not cfg.parent_dir:
        print("error: no parent folder. Pass --parent DIR.", file=sys.stderr)
        return 2
    if not from_iso:
        from . import disc as disc_mod
        cfg.device = disc_mod.detect_device(cfg.device)
        print(f"\033[2musing device {cfg.device}\033[0m")
    ev = _headless_events()
    from . import power
    while True:
        try:
            with power.Inhibitor(f"MiniDvdRipper ripping {cfg.device}",
                                 enabled=cfg.inhibit_sleep):
                rep = Pipeline(cfg, ev).run_disc(from_iso=from_iso)
        except Exception as e:
            print(f"\033[31mRIP FAILED: {e}\033[0m", file=sys.stderr)
            return 1
        rot = rep["bit_rot"]
        print("\n" + ("\033[32mClean rip.\033[0m" if rot["clean"]
                      else f"\033[33mBit rot: {rot['bad_bytes']:,} bytes lost.\033[0m"))
        if not loop or from_iso:
            return 0
        try:
            input("\nInsert next disc and press Enter (Ctrl-C to stop)… ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


def run_scan(cfg: Config) -> int:
    """Triage a stack of discs: identify + report each, eject, next. No ripping."""
    from . import disc as disc_mod
    C = {"ok": "\033[32m", "warn": "\033[33m", "dim": "\033[2m"}
    n = 0
    while True:
        dev = disc_mod.detect_device(cfg.device)
        info = disc_mod.identify(dev)
        n += 1
        c = C.get(info.scan_level(), "")
        print(f"{c}#{n:02d}  {dev}  {info.label or '(no label)':22s}  "
              f"{info.scan_line()}\033[0m")
        if info.media_present and tools.has("eject") and cfg.eject_when_done:
            tools.run(["eject", dev], check=False)
        try:
            input("Insert next disc and press Enter (Ctrl-C to stop)… ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


def run_finalize(cfg: Config) -> int:
    """Close an unfinalized DVD-R on the PC burner (IRREVERSIBLE write)."""
    from . import disc as disc_mod
    from . import finalize as fin
    dev = disc_mod.detect_device(cfg.device)
    info = disc_mod.identify(dev)
    chk = fin.can_finalize(info)
    print(f"Device : {dev}")
    print(f"Disc   : {info.scan_line()}")
    if not chk.ok:
        print(f"\033[33mCannot finalize: {chk.reason}\033[0m")
        return 2
    print("\n\033[1;31m*** WARNING — IRREVERSIBLE ***\033[0m")
    print(f"This WRITES a lead-out to {dev}. The disc is irreplaceable. If the")
    print("write fails it could be damaged. It does NOT add/erase your recordings;")
    print(f"it closes the disc so it can be read. {chk.reason}")
    try:
        typed = input(f"\nType the device path '{dev}' to proceed (anything else aborts): ")
    except (EOFError, KeyboardInterrupt):
        print("\naborted.")
        return 1
    if typed.strip() != dev:
        print("aborted (confirmation did not match).")
        return 1
    print("\nFinalizing… (do not remove the disc)")
    ok = fin.finalize_disc(dev, on_line=lambda line: print(f"  {line}"))
    if not ok:
        print("\033[31mFinalize failed. Try a different drive, or the disc may be "
              "unrecoverable here.\033[0m")
        return 1
    info2 = disc_mod.identify(dev)
    print(f"\n\033[32mDone. Disc now: {info2.scan_line()}\033[0m")
    if info2.kind == "ready":
        print("Rip it with:  ./mdvdrip --cli --parent <DIR>   (or run the TUI)")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        print(tools.preflight().summary())
        return 0
    cfg = cfg_from_args(args)
    if args.finalize:
        return run_finalize(cfg)
    if args.scan:
        return run_scan(cfg)
    if args.parent:                 # remember last parent folder
        cfg.save()
    if args.cli or args.from_iso:
        return run_headless(cfg, from_iso=args.from_iso, loop=args.loop)
    # default: TUI
    from .tui import run_tui
    return run_tui(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
