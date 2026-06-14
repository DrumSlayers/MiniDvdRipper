# MiniDvdRipper

[![PyPI](https://img.shields.io/pypi/v/minidvdripper.svg)](https://pypi.org/project/minidvdripper/)
[![Python](https://img.shields.io/pypi/pyversions/minidvdripper.svg)](https://pypi.org/project/minidvdripper/)
[![CI](https://github.com/drumslayer/MiniDvdRipper/actions/workflows/ci.yml/badge.svg)](https://github.com/drumslayer/MiniDvdRipper/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Linux](https://img.shields.io/badge/platform-Linux-333.svg?logo=linux&logoColor=white)](#requirements)
[![Lossless](https://img.shields.io/badge/remux-lossless-success.svg)](#lossless-remux-no-re-encoding)
[![Code style: ruff](https://img.shields.io/badge/lint-ruff-261230.svg)](https://github.com/astral-sh/ruff)

A terminal tool for archiving the **8 cm DVDs that Sony Handycam camcorders burned** (the DCR-DVD / "MiniDVD" line, roughly 2003–2011). It rips each disc into one lossless MKV per recording, copies the still photos, keeps the original dates so your photo library sorts them correctly, and — the part I actually built this for — **recovers discs that were never finalized**, without the camcorder and without writing anything to the disc.

It was written to clear a shoebox of family discs whose camcorder died years ago. Some discs were finalized and read fine. Others were stuck "open" and every off-the-shelf tool treated them as blank. They weren't.

---

## Contents

- [What it does](#what-it-does)
- [Install](#install)
- [Quick start](#quick-start)
- [Output layout](#output-layout)
- [The Sony Handycam MiniDVD format](#the-sony-handycam-minidvd-format) — the format, in detail
- [How a rip works, step by step](#how-a-rip-works-step-by-step)
- [Recovering unfinalized discs](#recovering-unfinalized-discs) — the interesting part
- [Bit rot](#bit-rot)
- [Why these tools and not others](#why-these-tools-and-not-others)
- [Settings, cancel, wakelock](#settings-cancel-wakelock)
- [Requirements](#requirements)
- [Limitations](#limitations)
- [Development](#development)
- [References](#references)

---

## What it does

- **Detects the disc** and reads its identity with `dvd+rw-mediainfo` (media type, finalization state, track table, volume label).
- **Images finalized discs** with GNU `ddrescue` to a bit-exact ISO plus a mapfile, so unreadable sectors are retried and then *recorded*, not silently dropped.
- **Recovers unfinalized discs** by reading the recorded tracks straight off the drive — no finalize step, no second drive, nothing written to the disc.
- **Remuxes losslessly**: one MKV per recording session, MPEG-2 video and AC-3/PCM audio stream-copied, zero re-encoding.
- **Keeps dates**: each clip gets its real recording date as the Matroska `creation_time` and the file mtime, so Synology Photos (or anything else) files it under the right year instead of "today".
- **Copies the stills** from `DCIM/` with their EXIF intact.
- **Builds a contact sheet** — a single lightweight JPEG of ~20 frames per clip, so you can see what's on a disc at a glance.
- **Flags bit rot** down to the file: "VTS_02_1.VOB has 3 unreadable sectors", not a shrug.
- **Triage mode** (`--scan`) tells you, per disc, whether it's blank, finalized-and-ready, or has-data-but-unfinalized — handy when you're sorting a whole box.

The interface is a full-screen TUI (built with [Textual](https://github.com/Textualize/textual)); there's also a headless CLI for scripting or batching a stack of discs.

---

## Install

From PyPI:

```bash
pip install minidvdripper
mdvdrip            # launch the TUI
```

From source:

```bash
git clone https://github.com/drumslayer/MiniDvdRipper
cd MiniDvdRipper
python -m venv .venv && . .venv/bin/activate
pip install -e .
mdvdrip
```

`minidvdripper` itself only depends on Textual. The real work is done by external tools you need on your `PATH` (see [Requirements](#requirements)). Run `mdvdrip --check` to see what's installed and what's missing.

---

## Quick start

**Rip a disc (TUI):** launch `mdvdrip`, point it at a parent output folder (type a path or hit **Browse**, which opens a `zenity` folder picker), pop a disc in, press **RIP**. When it's done it ejects; insert the next one and press RIP again.

Keys: `r` rip · `x` cancel · `e` eject · `d` detect drive · `f` finalize · `s` settings · `c` re-check tools · `q` quit.

**Sort a whole box first, without ripping anything:**

```bash
mdvdrip --scan
```

It reports each disc — `BLANK`, `READY — finalized`, or `HAS DATA but UNFINALIZED — ~476 MB in 2 recording(s)` — ejects, and waits for the next. Good for separating the discs worth ripping from the empties.

**Batch a stack headless:**

```bash
mdvdrip --cli --parent ~/Videos/MiniDVD --loop
```

**Re-process from an ISO you already pulled** (no disc needed — useful for re-running the extraction after a change):

```bash
mdvdrip --from-iso /path/to/disc.iso --parent /tmp/out --cli
```

Other flags: `--device /dev/sr1`, `--no-eject`, `--no-iso`, `--keep-extracted`, `--no-verify`, `--no-contact`, `--retries N`, `--finalize`. The drive is auto-detected, so a USB burner that lands on `/dev/sr1` instead of `/dev/sr0` is found without fuss; press `d` in the TUI to re-detect after replugging.

---

## Output layout

One folder per disc, named after the disc's volume label normalised to 24-hour (Handycams burn the label in 12-hour form with AM/PM — `2005_08_21_04H13M_PM` — which is rewritten to `2005_08_21_16H13M` so it matches the clip file names). An **unfinalized** disc has no label, so it's named from the rip date with a `_recovered` suffix (e.g. `2026_06_12_01H58M_recovered`).

```
2005_08_21_16H13M/
├── 2005_08_21_16H13M_thumbnails.jpg  # overview montage: a frame from every movie + every photo
├── video/
│   ├── 01__2005-08-21_17h30.mkv      # one lossless MKV per recording session
│   └── 02__2005-08-21_17h57.mkv
├── photos/
│   └── DSC00043.JPG                  # DCIM stills, EXIF preserved
├── contact/
│   ├── 01__2005-08-21_17h30.jpg      # 5×4 storyboard + metadata header — glance = contents
│   └── 02__2005-08-21_17h57.jpg
└── _master/
    ├── disc.iso                      # the bit-exact image (finalized discs)
    ├── disc.map                      # ddrescue mapfile — proof of what did/didn't read
    ├── mediainfo.txt                 # raw dvd+rw-mediainfo dump
    ├── report.txt                    # human-readable summary
    └── report.json                   # same, machine-readable, with SHA-256 of each MKV
```

The MKV filenames lead with the session number and the recording timestamp, so they sort chronologically on their own.

---

## The Sony Handycam MiniDVD format

If you just want to rip discs, skip this. If you want to know *why* unfinalized discs are a pain and how the recovery works, you need the format first.

### The physical disc

Sony's DVD Handycams (DCR-DVD models) record to **8 cm optical discs** — half the diameter of a normal DVD, about 1.4 GB single-layer (the small ones in the camera are ~30 min of video). Four media types show up in the wild:

| Media | Rewritable | Typical Handycam use |
|-------|------------|----------------------|
| DVD-R | no (write-once) | most common; **must be finalized** to play elsewhere |
| DVD-RW | yes | Video mode (needs finalize) or VR mode |
| DVD-RAM | yes | VR mode, defect-managed |
| DVD+RW | yes | usually readable without finalize |

`dvd+rw-mediainfo` reports the exact profile, e.g. `Mounted Media: 11h, DVD-R Sequential` or `13h, DVD-RW Restricted Overwrite`.

### Two recording modes

Handycams record in one of two on-disc layouts:

- **Video mode** → standard **DVD-Video** (`VIDEO_TS/`). Plays in any DVD player *once finalized*. This is what most of these discs are.
- **VR mode** (Video Recording) → `DVD_RTAV/VR_MOVIE.VRO`, an editable format. Readable without finalizing, but not playable in old set-top players.

MiniDvdRipper handles Video mode as the main case and falls back to the VR `.VRO` when that's what it finds.

### What's actually in `VIDEO_TS`

A finalized Video-mode disc has a `VIDEO_TS` directory with three kinds of files:

```
VIDEO_TS.IFO / .BUP / .VOB     ← VMG: the Video Manager (top-level menu + first-play)
VTS_01_0.IFO / .BUP            ← Title set #1: navigation/control (the IFO) + its backup (BUP)
VTS_01_0.VOB                   ← menu/PGC video for title #1 (often tiny or absent)
VTS_01_1.VOB                   ← the actual recording for title #1
VTS_01_2.VOB                   ← continued, if the recording crosses the 1 GB VOB boundary
VTS_02_0.IFO ...               ← title set #2, and so on
```

The key facts the ripper leans on:

- **One Video Title Set (`VTS_NN`) ≈ one recording session.** Each time you hit record-stop on the camcorder, you get a new title. So "one MKV per session" is just "one MKV per VTS".
- **The video lives in the numbered parts** `VTS_NN_1.VOB`, `VTS_NN_2.VOB`, … A VOB is an [MPEG-2 program stream](https://en.wikipedia.org/wiki/MPEG_program_stream) — multiplexed MPEG-2 video + AC-3 (or LPCM) audio + DVD navigation packets. The DVD spec caps a single VOB at 1 GB, so long recordings spill into `_2`, `_3`, … which are meant to be read back-to-back.
- **`VTS_NN_0.VOB` is the menu/PGC**, not your footage — the ripper skips it.
- **`.IFO` files are the navigation tables** (chapters, angles, audio/subtitle maps), with a `.BUP` byte-for-byte backup. For a straight remux we don't strictly need them, but they're where chapter offsets would come from.
- **The `VIDEO_TS.IFO` (VMG)** holds the disc's menu and is written *at finalization*. On an unfinalized disc it may be missing or stubbed — which matters later.

### Still photos

If you used the camcorder's photo mode, the JPEGs sit in a camera-style tree:

```
DCIM/100MSDCF/DSC00043.JPG
```

with normal EXIF (including `DateTimeOriginal`). These are **files in the disc's filesystem**, not DVD-Video tracks — a distinction that decides whether they survive on an unfinalized disc (they don't; more below).

### Finalization, and why it's the whole story

A DVD's readable area is bracketed by a **lead-in** (which carries the control data / table of contents, including the disc's usable capacity) and a **lead-out** (which marks the end). A standard DVD-ROM drive reads the lead-in, learns "this disc has N readable sectors", and refuses to read past that.

When you record without finalizing, the camcorder writes your video into the data area but **doesn't write a proper lead-out or close the filesystem**. The camcorder doesn't care — it keeps its own private bookkeeping on the disc (the **Recording Management Area / RMA**, sometimes called RMD) describing where every track starts and ends, and it reads its own discs back using that. The on-screen "thumbnail menu" you see on a Handycam is built from *that* index, not from the DVD-Video VMG menu.

**Finalizing** is the step that writes the lead-in control data (with the real capacity), the lead-out, and — on Sony Video-mode discs — the `VIDEO_TS.IFO`. After that, any drive can read the disc. Before that, only a device that understands the RMA (the camcorder, or a forgiving drive) can.

`dvd+rw-mediainfo` shows the state plainly:

```
Disc status:           complete        ← finalized
State of Last Session: complete
```
versus an unfinalized disc:
```
Disc status:           appendable      ← still "open"
State of Last Session: incomplete
READ CAPACITY:         0*2048=0        ← the drive won't admit to a readable size
```

### DVD-R sequential recording and the reserved track

DVD-R written by a Handycam uses **incremental / sequential recording**, and the track table tells a story. A real example from one of my discs:

```
Number of Tracks:      4
Track #1  State: reserved incremental    Start: 0       Size: 12272*2KB   ← empty, reserved
Track #2  State: complete incremental    Start: 12288   Size: 176*2KB     ← tiny (a still / first-play)
Track #3  State: complete incremental    Start: 12480   Size: 232032*2KB  ← ~475 MB: the video
Track #4  State: invisible incremental   Start: 244528  Size: 469408*2KB  ← reserved, unwritten
```

Two things to notice:

- The recordings live in tracks marked **`complete incremental`** — they're closed and intact, with a known start LBA and size. The *disc* is open, but those *tracks* are done.
- Track #1 is **`reserved`** and empty. The camcorder set aside ~24 MB at the very start of the disc for the VMG / `VIDEO_TS` it would have written at finalize. It never got there.

That reserved-but-empty first track is exactly what trips up generic "close the disc" tools, and the `complete` tracks are exactly what makes recovery possible. Both come up next.

---

## How a rip works, step by step

The pipeline runs in one place ([`pipeline.py`](src/minidvdripper/pipeline.py)) and emits events the TUI and CLI render. For a finalized disc it goes:

### 1. Identify

Run `dvd+rw-mediainfo` and parse it ([`disc.py`](src/minidvdripper/disc.py)): media type, disc status, session state, the full track table, and read capacity. A disc is "finalized" when status is `complete` and the last session is `complete`. The volume label (`isoinfo -d`) doubles as the folder name, and the Sony date-string label (`2005_08_21_04H13M_PM`) is parsed into a real datetime for metadata. If there's no disc, or the disc is blank, the run stops here with a clear message instead of creating an empty folder.

### 2. Image (finalized discs) — `ddrescue`

```
ddrescue -b 2048 -d -n      /dev/srX disc.iso disc.map     # pass 1: grab the easy sectors fast
ddrescue -b 2048 -d -r3     /dev/srX disc.iso disc.map     # pass 2: retry/scrape the holes
```

Two passes: a quick no-scrape sweep, then a retrying pass that hammers only the bad spots. The **mapfile** is the point — it records every sector as recovered or not, so the rip is resumable (see below) and, crucially, we know *exactly* what didn't read. ddrescue's progress is parsed into a single live line: percent, rescued size, rate, ETA, bad-sector count — plus how many bytes are still **unread** (not-yet-attempted, so you can see there's data left), and a **`⚠ STALLED Nm`** flag if the rescued total stops growing. A stall means the laser is grinding a rotted band: not hung, just retrying sectors that won't come back. It's logged once when it starts and once when reading resumes, so a long quiet stretch reads as "stuck on rot, safe to cancel" rather than "is this frozen?".

#### Resume / interrupted rips

A multi-hour rip of a damaged disc can be stopped (press `x`) or die mid-way, and the next run **continues instead of starting over**. The contract:

- The per-disc folder is keyed on the disc's **volume label**, not a timestamp — so the same disc always maps to the same `…/<label>/` folder and the same `_master/disc.iso` + `_master/disc.map`.
- `keep_iso` and `keep_mapfile` default to **on**, and cancelling skips the end-of-run cleanup — so the image and mapfile survive an interruption.
- On the next **Rip**, the ripper sees the existing pair, reads the mapfile, and logs e.g. `Resuming — found a previous image: 63.1% already read, 182 bad area(s) still to retry.` ddrescue then skips every already-recovered sector and only re-attacks the holes.

Because the state lives in the mapfile, you can even **move the disc to a different drive** and resume there — a second drive's optics often read marginal sectors the first one couldn't. (Resume needs *both* `disc.iso` and `disc.map`; with `keep_*` on, that's the default.)

### 3. Extract — `bsdtar`

Rather than mounting the ISO (which needs root), the files are pulled out with `bsdtar`, which reads ISO 9660 + Rock Ridge directly and **restores each file's original timestamp**. Those VOB timestamps are the camcorder's recording dates — we reuse them.

### 4. Map bit rot onto files

`isoinfo -l` gives every file's starting LBA and size. Cross-referencing that with the non-recovered ranges from the ddrescue mapfile, the ripper can say which *file* a bad sector falls in, e.g. `ROT in /VIDEO_TS/VTS_02_1.VOB: 3 sector(s) unreadable` — far more useful than a global error count.

### 5. Group into sessions

Scan `VIDEO_TS`, group `VTS_NN_*.VOB` by title number, order the parts, and drop the `_0` menu VOB ([`titles.py`](src/minidvdripper/titles.py)). Title sets smaller than 512 KB are skipped as finalization placeholders (a Handycam often writes a tiny empty VTS when it closes a disc) — and the skip is logged, never silent. If `lsdvd` is installed, chapter offsets are pulled in as a bonus.

### 6. Lossless remux — no re-encoding

Each session's VOB parts are concatenated with ffmpeg's `concat:` protocol (valid for MPEG program streams) and rewrapped into Matroska:

```bash
ffmpeg -fflags +genpts+igndts -analyzeduration 200M -probesize 200M \
       -i "concat:VTS_01_1.VOB|VTS_01_2.VOB" \
       -map 0:v? -map 0:a? -map 0:s? -c copy out.mkv
```

`-c copy` means the original MPEG-2 video and AC-3/PCM audio are copied bit-for-bit — no transcode, no generation loss, no deinterlace guesswork. The `+genpts+igndts` flags rebuild timestamps, because DVD VOBs carry discontinuous/looping PTS that otherwise make muxers refuse the stream. (This is also the honest reason the files stay MPEG-2: re-encoding to H.264 would be lossy, and "archive" and "lossy" don't belong in the same sentence.)

After muxing, an optional verify pass decodes the whole file to null and counts **genuine** decoder errors. The catch I hit: a clean disc reported dozens of "errors" that were really the null muxer complaining about DVD timestamps (`Application provided invalid, non monotonically increasing dts`). Those are benign and now filtered, so a clean rip reads as clean and a real corrupt-frame count means something.

### 7. Dates and metadata

Each MKV gets the session's recording date written as the Matroska `creation_time`, plus `title`, `date`, and a `comment` noting the source disc; the file's mtime is set to the same instant as a belt-and-braces fallback. Stills are copied with `shutil.copy2` (mtime preserved), and if `exiftool` is present their file mtime is realigned to EXIF `DateTimeOriginal`. The net effect: drop the folder on a Synology and **everything lands under its real year**, not the day you ripped it.

### 8. Contact sheet

For each clip, ffmpeg samples ~20 evenly spaced frames and tiles them 5×4 into one ~280 KB JPEG ([`contact.py`](src/minidvdripper/contact.py)):

```bash
ffmpeg -skip_frame nokey -i clip.mkv -frames:v 1 \
       -vf "fps=21/DURATION,scale=320:-1,tile=5x4:padding=4:margin=6" -q:v 5 sheet.jpg
```

A header band on each sheet carries the clip's metadata — filename, codec, resolution, duration and file size — drawn in the same pass with ffmpeg's `drawtext` filter (the text comes from an `ffprobe` of the finished MKV). One glance tells you whether a disc is a birthday, a holiday, or twelve minutes of someone's thumb.

### 8b. Folder overview montage

Beyond the per-clip sheets, the ripper builds **one image for the whole disc folder** — `<folder>_thumbnails.jpg` in the disc's root — tiling a mid-clip frame from every movie *and* every recovered photo, each labelled with its filename and titled with the disc name. It's made with ImageMagick's `montage`, so this one step needs ImageMagick (it's skipped, with a note, if `montage` isn't installed). The result is a single picture that shows everything the disc holds at a glance — handy when browsing an archive of dozens of folders. Toggle it in Settings (*folder overview montage*).

A copy of each folder's overview is also dropped in the **parent folder** (`<parent>/<folder>_thumbnails.jpg`), so you can flip through every disc's contents from one place without opening each folder. It's a real copy, not a symlink — symlinks don't sync reliably through Synology Drive and many viewers won't follow them, whereas a copy syncs and opens everywhere; it's only ~100 KB and is refreshed on every rip. Toggle it in Settings (*copy each overview into the parent folder*).

### 9. Report

A `report.json` and a readable `report.txt`: disc identity, finalization, the bit-rot map (bad sectors → files), per-session durations and stream info, decode-verification results, and a SHA-256 of every MKV so you can verify the archive years from now.

### 10. Eject, repeat

The tray opens (toggleable), and you load the next disc.

---

## Recovering unfinalized discs

This is the part I'm actually proud of, because the conventional answer is "you can't without the original camcorder", and that turns out to be wrong.

### The problem

An unfinalized Handycam DVD-R reports `READ CAPACITY 0`, and the drive I have (a Panasonic/Matsushita UJ8B0AW, the kind in a lot of laptops) refuses to read its sectors at all. A direct SCSI read comes straight back as:

```
sr 7:0:0:0: [sr1] Add. Sense: Logical block address out of range
critical target error, dev sr1, sector 4 op 0x0:(READ)
```

So `readcd`, `readom`, and raw `sg_read` all return nothing. By the letter of the spec, the disc has no readable area. Most guides stop here and tell you to finalize the disc on the camcorder (which I don't have) or send it to a recovery service.

### The discovery

The recordings are physically on the disc — the track table proves it (a `complete incremental` track of 475 MB doesn't lie). The drive's *firmware* is gatekeeping reads against the missing lead-out. But there's a gap between the firmware's raw-SCSI behaviour and what the **Linux block layer** exposes: the kernel computes a size for `/dev/sr1` from the recorded extent (here 244512 sectors ≈ 500 MB), and **a buffered `dd` read through the block device succeeds where raw SCSI fails**:

```bash
$ dd if=/dev/sr1 bs=2048 skip=12480 count=2048 conv=noerror,sync
2048+0 records in
$ ffprobe ...
dvd_nav_packet / mpeg2video / ac3      ← that's the video, reading fine
```

That's the whole trick. No finalize, no second drive, nothing written to the disc — just read it through the right interface.

### How the recovery actually runs

The one trap: the empty **reserved** track #1 reads at roughly 1 kB/s (the drive grinds on the unwritten area), so dumping the whole device would take an hour to get nothing. Instead the ripper reads **only the `complete` tracks**, addressed by the start-LBA and size from the track table ([`recover.py`](src/minidvdripper/recover.py)):

```bash
dd if=/dev/srX of=track_01.vob bs=2048 skip=<start_lba> count=<sectors> \
   conv=noerror,sync status=progress
```

Each extracted track is an MPEG-2 program stream and goes through the exact same lossless remux as a finalized VOB. (`status=progress` is parsed under a forced `C` locale, because on a French system `dd` prints `octets`, not `bytes`, and the progress bar was reading nothing — a small fix with an annoying root cause.)

On a real disc this pulled back **12 minutes of family video — a houseful of relatives around a lunch table — from a disc three different programs had called blank.** That was the moment the project justified itself.

### The carve fallback

If a disc *is* readable as a filesystem but somehow has no usable `VIDEO_TS` (for instance after a partial PC-side finalize), the ripper slices the recorded tracks straight out of the ISO image by their LBA ranges and remuxes those ([`carve.py`](src/minidvdripper/carve.py)). Same idea, different source.

### Finalizing on a PC (and why it usually doesn't work)

There's also a `--finalize` command (TUI key `f`) that tries to write a lead-out with `cdrecord -fix`. It exists, it asks you to type the device path to confirm (it's an **irreversible write** to a one-of-a-kind disc), and on most camcorder DVD-Rs **it fails** — `cdrecord` drops into CD-ROM/TAO mode and no-ops, because it doesn't know how to close a disc with that reserved VMG track. I left it in as a last resort, but direct recovery is the path that actually works, so that's what RIP does automatically now.

### Stills: carved, not opened

The photos are a separate problem from the video. There's no filesystem directory on an unfinalized disc, so you can't open `DCIM/.../DSC*.JPG` by name — but the JPEG *bytes* were written to a small closed data track when each photo was taken. So the ripper reads that small track and **carves the photos out by signature** ([`stills.py`](src/minidvdripper/stills.py)): scan the raw bytes for JPEG markers, walk the segments to find each image's true end (skipping the embedded EXIF thumbnail so the main image isn't truncated), validate that it decodes, and pull the EXIF date. It's the same approach PhotoRec uses, built in. Whether anything comes out depends on the disc — if the stills sit in a readable `complete` track, they're recovered to `photos/`; if there were none, you get an honest "no stills found".

### What's still missing on an unfinalized disc

- **Chapters** — no IFO navigation.
- **Recording dates for the video** — there's no volume label, so clips come out named generically (`DISC_<timestamp>`, `01__undated.mkv`). The video itself is complete and lossless; rename to taste. (Carved photos keep their own EXIF dates.)

---

## Bit rot

These discs are old, and dye-based recordable media degrades. The strategy is two-layered:

1. **Read layer** — `ddrescue` retries weak sectors and writes down which ones never came back, so partial damage yields a partial-but-honest rip instead of a hard failure. The mapfile is kept (`_master/disc.map`) as evidence.
2. **Decode layer** — the post-remux verify pass actually decodes the video and counts genuine corrupt frames, catching rot that read back as bytes but isn't valid MPEG-2.

The report lists both, and the bad sectors are mapped to filenames so you know *which clip* is affected, not just that something is.

---

## Why these tools and not others

- **`ddrescue`, not `dvdbackup` / plain `dd`** — libdvdread-based tools and naïve `dd` give up at the first read error. ddrescue is built for failing media, retries intelligently, and logs what it couldn't get. For aging discs that's the difference between "recovered 99.6%" and "I/O error, aborting".
- **No `vamps` / no transcoding** — vamps (and HandBrake, and friends) re-encode. This is an archival tool; the master stays the camcorder's original MPEG-2 in an MKV wrapper. If you later want a small H.264 "play copy", that's a separate, deliberate, lossy step — not something done to your master.
- **`bsdtar`, not loop-mount** — extracting the ISO with libarchive needs no root and preserves the Rock Ridge timestamps we rely on for dates.
- **MKV, not MP4** — Matroska wraps MPEG-2, multi-track audio, and DVD subtitles cleanly. The codec on disc stays MPEG-2 either way, so MKV is the honest, robust container. (Synology will transcode MPEG-2 on the fly for browser playback regardless of container.)

---

## Settings, cancel, wakelock

**Settings** (`s`, or the button) toggles, and persists to `~/.config/minidvdripper/config.json`:

- eject when a rip finishes
- prevent the computer sleeping during a rip
- make contact sheets
- decode-verify
- carry DVD subtitle streams
- stamp photo EXIF dates
- keep the ISO master / mapfile / raw extracted files
- ddrescue retry count

**Cancel** (`x`, or the red button) aborts a running rip — it kills the active subprocess and stops between steps. **Eject** also cancels a running job before opening the tray. A cancelled rip leaves whatever it had finished in the disc folder; just re-run it.

**Wakelock** — while a rip runs, the machine is held awake with a `systemd-inhibit` lock (`idle:sleep`), released the instant the rip ends — the same mechanism a browser uses during video playback. A long ddrescue pass won't get suspended out from under you. Toggle it off in Settings if you'd rather the box sleep.

**Finish notification + chime** — when a rip ends (a multi-hour job on a damaged disc, so you've wandered off), the tool pops a desktop notification via `notify-send` and plays a completion sound, so you don't have to babysit it. Both go through standard desktop plumbing: the notification is libnotify (honoured by GNOME, KDE, XFCE, …), and the chime is a freedesktop event sound played by `canberra-gtk-play` (falling back to `pw-play`/`paplay`/`ffplay` on a themed sound file, and to the terminal bell if none are installed). The notification is `normal` urgency on success, `critical` on bit-rot or failure. Toggle either off in Settings (*Desktop notification* / *Play a chime*).

**Reading and copying the log** — every line in the central log is timestamped (`HH:MM:SS`) and carries a status glyph (✓ done · ⚠ warning · ✗ error), and `key=value` headers (`media=DVD-ROM`, `finalized=yes`, …) are highlighted so the disc summary reads as labels rather than a run-on line. When a disc is identified the ripper also draws a one-line **summary card** of background-coloured chips — `device · media · finalized/unfinalized · size · label` — so the essentials are scannable at a glance. To grab a few lines, drag to **select with the mouse** and copy. To grab the whole log at once, press **`y`** (*Copy log*) — it copies the full plain-text log to the clipboard via OSC-52, which also works over SSH.

**Icons** — the status glyphs and step markers use [Nerd Font](https://www.nerdfonts.com/) icons when your terminal is set to a Nerd-Font-patched font (most are). If you see tofu boxes (□) instead, turn **Nerd Font icons** off in Settings and the UI falls back to plain unicode (✓ ⚠ ✗ ○ ◐ ●) that renders in any terminal — the coloured chips stay either way.

---

## Requirements

Linux, Python ≥ 3.10, and these on your `PATH`:

| Tool | Package (Arch / CachyOS) | Needed for |
|------|--------------------------|------------|
| `ddrescue` | `gddrescue` | bit-rot-safe imaging (**required**) |
| `ffmpeg` / `ffprobe` | `ffmpeg` | remux, verify, contact sheets (**required**) |
| `dvd+rw-mediainfo` | `dvd+rw-tools` | disc identity + finalization (**required**) |
| `isoinfo` | `cdrtools` | ISO listing / sector→file map (**required**) |
| `bsdtar` | `libarchive` | rootless ISO extraction (**required**) |
| `dd` | `coreutils` | unfinalized track recovery (**required**) |
| `montage` (ImageMagick) | `imagemagick` | folder overview montage (optional) |
| `eject` | `util-linux` | open the tray (optional) |
| `lsdvd` | `lsdvd` | chapter metadata (optional) |
| `exiftool` | `perl-image-exiftool` | photo date alignment (optional) |
| `cdrecord` | `cdrtools` | PC-side finalize fallback (optional) |
| `systemd-inhibit` | `systemd` | sleep wakelock (optional) |
| `zenity` | `zenity` | the Browse folder picker (optional) |

On Arch / CachyOS:

```bash
sudo pacman -S --needed gddrescue ffmpeg dvd+rw-tools cdrtools libarchive \
                        lsdvd perl-image-exiftool imagemagick libcanberra
```

Your user needs read access to the optical device — being in the `optical` group is enough; recovery and ripping do **not** need root.

---

## Limitations

- **Linux only.** It shells out to Linux tools and reads `/dev/sr*` and `/sys/class/block`.
- **Unfinalized recovery depends on your drive.** Reading through the block layer works on the drives I tested, but firmware varies — some refuse even that, in which case a different drive (or the original camcorder) is the only option. The tool tells you what it found either way.
- **Unfinalized discs lose chapters and per-clip dates** — there's no filesystem index, so chapter offsets and the volume-label recording date are gone (clips come out generically named). Still photos *are* recovered, by carving them out of the raw tracks (see above), though that's best-effort: they come back only if they sit in a readable track.
- **PC-side finalize is unreliable** for camcorder DVD-Rs and is a last resort, not the happy path.
- It's been exercised on the discs I had — a couple of finalized DVD-RWs and an unfinalized DVD-R — plus synthetic fixtures in CI. Your mileage on exotic media may differ; the report will be honest about it.

---

## Development

```bash
pip install -e ".[dev]"        # ruff, build, pytest
ruff check src tests           # lint
python tests/offline_test.py   # the offline test suite (pure parsers, no disc needed)
python -m build                # build sdist + wheel
```

The test suite covers the parts that don't need hardware: the volume-label date parser, the `dvd+rw-mediainfo` parser and finalization logic, the ddrescue mapfile parser, the `isoinfo` listing parser and the sector→file rot mapping, disc triage (blank / unfinalized / ready), and the ddrescue progress line parser. The subprocess-driven pieces are validated against real and synthetic discs by hand.

### CI / publishing

- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — runs ruff, the test suite on Python 3.10–3.13, and a build (with `twine check`) on every push and PR.
- [`.github/workflows/publish.yml`](.github/workflows/publish.yml) — on a published GitHub Release, builds and uploads to PyPI via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no stored token). Configure the publisher once in your PyPI project settings; there's a commented API-token fallback in the workflow if you'd rather use a secret.

To cut a release: bump `version` in `pyproject.toml`, tag `vX.Y.Z`, and publish a GitHub Release.

---

## References

- GNU ddrescue manual — <https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html>
- "Preserving optical media from the command-line" (bitsgalore) — <https://www.bitsgalore.org/2015/11/13/preserving-optical-media-from-the-command-line>
- "Ripping unfinalized DVDs from Linux" (Mark Hobson) — <https://markandruth.co.uk/2019/09/30/ripping-unfinalized-dvds-from-linux>
- DVD-Video / VIDEO_TS structure and MPEG program streams on Wikipedia
- Arch Wiki: Optical disc drive, dvdbackup

---

## License

MIT — see [LICENSE](LICENSE).
