"""Offline unit tests for the pure parsers — no disc/ISO needed.

Run: PYTHONPATH=src .venv/bin/python tests/offline_test.py
"""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from minidvdripper import (  # noqa: E402
    carve,
    contact,
    describe,
    disc,
    icons,
    imaging,
    isofs,
    notify,
    stills,
)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name}")


# ---- 1. volume-label datetime --------------------------------------------
def test_label_dt():
    cases = {
        "2005_08_21_04H13M_PM": datetime(2005, 8, 21, 16, 13),
        "2005_08_22_11H05M_AM": datetime(2005, 8, 22, 11, 5),
        "2005_12_31_12H00M_AM": datetime(2005, 12, 31, 0, 0),   # midnight
        "2005_12_31_12H00M_PM": datetime(2005, 12, 31, 12, 0),  # noon
        "2010_01_02_18H30M":    datetime(2010, 1, 2, 18, 30),   # 24h, no am/pm
    }
    for label, want in cases.items():
        check(f"label {label}", disc.parse_label_datetime(label) == want)
    check("label garbage -> None", disc.parse_label_datetime("MY_HOLIDAY") is None)

    # folder name normalised to 24h (matches the 24h clip file names)
    check("folder 12h PM -> 24h", disc.folder_label("2007_04_08_01H10M_PM") == "2007_04_08_13H10M")
    check("folder 12h AM midnight",
          disc.folder_label("2005_12_31_12H00M_AM") == "2005_12_31_00H00M")
    check("folder 24h passthrough", disc.folder_label("2010_01_02_18H30M") == "2010_01_02_18H30M")
    check("folder unparsed -> raw", disc.folder_label("MY_HOLIDAY") == "MY_HOLIDAY")
    check("folder empty -> None", disc.folder_label("") is None)


# ---- 2. mediainfo parse + finalization -----------------------------------
MEDIAINFO = """INQUIRY:                [MATSHITA][DVD-RAM UJ8B0AW ][1.00]
GET [CURRENT] CONFIGURATION:
 Mounted Media:         13h, DVD-RW Restricted Overwrite
 Media ID:              OPTODISCW002
READ DISC INFORMATION:
 Disc status:           complete
 Number of Sessions:    1
 State of Last Session: complete
 Number of Tracks:      1
READ CAPACITY:          537856*2048=1101529088
"""


def test_mediainfo():
    info = disc.parse_mediainfo(MEDIAINFO)
    check("media parsed", info.media == "DVD-RW Restricted Overwrite")
    check("media_id parsed", info.media_id == "OPTODISCW002")
    check("disc status complete", info.disc_status == "complete")
    check("sessions=1", info.sessions == 1)
    check("capacity bytes", info.capacity_bytes == 1101529088)
    check("finalized=True", info.finalized is True)
    check("no warnings", not info.warnings)

    bad = disc.parse_mediainfo(MEDIAINFO.replace("Disc status:           complete",
                                                 "Disc status:           incomplete"))
    check("incomplete -> not finalized", bad.finalized is False)
    check("incomplete -> warning", len(bad.warnings) == 1)


# ---- 3. ddrescue mapfile parse -------------------------------------------
MAPFILE = """# Mapfile. Created by GNU ddrescue version 1.30
# Command line: ddrescue -b 2048 -d -n -v /dev/sr0 disc.iso disc.map
# current_pos  current_status  current_pass
0x000C8000     +               2
#      pos        size  status
0x00000000  0x000C8000  +
0x000C8000  0x00001000  -
0x000C9000  0x00100000  +
0x001C9000  0x00000800  *
"""


def test_mapfile():
    with tempfile.NamedTemporaryFile("w", suffix=".map", delete=False) as f:
        f.write(MAPFILE)
        path = f.name
    rep = imaging.parse_mapfile(path)
    os.unlink(path)
    # good: 0xC8000 + 0x100000 ; bad: 0x1000 + 0x800
    want_bad = 0x1000 + 0x800
    want_good = 0xC8000 + 0x100000
    check("rot bad_bytes", rep.bad_bytes == want_bad)
    check("rot recovered_bytes", rep.recovered_bytes == want_good)
    check("rot total", rep.total_bytes == want_bad + want_good)
    check("rot 2 bad ranges", len(rep.bad_ranges) == 2)
    check("rot not clean", rep.clean is False)
    # bad range #1: pos 0xC8000 size 0x1000 -> lba 100..101 (0xC8000/2048=100)
    r0 = rep.bad_ranges[0]
    check("bad range lba", r0.start_lba == 0xC8000 // 2048 and r0.end_lba == 0xC9000 // 2048)


# ---- 4. isoinfo -l parse + sector->file map ------------------------------
ISOINFO = """Directory listing of /
d---------   0    0    0            2048 Aug 21 2005 [     23 02]  .
d---------   0    0    0            2048 Aug 21 2005 [     23 02]  ..

Directory listing of /VIDEO_TS/
----------   0    0    0               16384 Feb 25 2007 [     24 00]  VIDEO_TS.IFO;1
----------   0    0    0            99000000 Aug 21 2005 [     30 00]  VTS_01_1.VOB;1
----------   0    0    0           265000000 Aug 22 2005 [  48360 00]  VTS_02_1.VOB;1
"""


def test_isoinfo():
    files = isofs.parse_isoinfo_listing(ISOINFO)
    byname = {f.path: f for f in files}
    check("3 files parsed", len(files) == 3)
    check("vts01 path", "/VIDEO_TS/VTS_01_1.VOB" in byname)
    v1 = byname["/VIDEO_TS/VTS_01_1.VOB"]
    check("vts01 lba", v1.start_lba == 30)
    check("vts01 size", v1.size == 99000000)
    # craft a bad range inside VTS_01_1.VOB (lba 30..~48342): hit lba 100..102
    bad = [imaging.BadRange(100, 102, "-")]
    affected = isofs.map_rot_to_files(files, bad)
    check("rot maps to vts01", len(affected) == 1 and affected[0].path.endswith("VTS_01_1.VOB"))
    check("rot 2 sectors", affected[0].bad_sectors == 2)


# ---- 5. triage: blank vs unfinalized-with-data vs ready ------------------
UNFINALIZED = """ Mounted Media:         11h, DVD-R Sequential
 Disc status:           appendable
 State of Last Session: incomplete
 Number of Tracks:      4
READ TRACK INFORMATION[#1]:
 Track State:           reserved incremental
 Track Size:            12272*2KB
READ TRACK INFORMATION[#2]:
 Track State:           complete incremental
 Track Size:            176*2KB
READ TRACK INFORMATION[#3]:
 Track State:           complete incremental
 Track Size:            232032*2KB
READ TRACK INFORMATION[#4]:
 Track State:           invisible incremental
 Track Size:            469408*2KB
READ CAPACITY:          0*2048=0
"""

BLANK = """ Mounted Media:         11h, DVD-R Sequential
 Disc status:           blank
 Number of Tracks:      1
READ TRACK INFORMATION[#1]:
 Track State:           blank
 Track Size:            2298496*2KB
READ CAPACITY:          0*2048=0
"""


def test_triage():
    unf = disc.parse_mediainfo(UNFINALIZED)
    check("unfinalized kind", unf.kind == "unfinalized")
    check("unfinalized has_data", unf.has_data is True)
    check("unfinalized 2 recordings", unf.recorded_tracks == 2)
    check("unfinalized ~476MB", 470 < unf.recorded_bytes / 1e6 < 480)
    check("unfinalized not imageable", unf.imageable is False)
    check("unfinalized NOT blocked (recoverable directly)", unf.blocker is None)
    check("unfinalized scan says recoverable", "RECOVERABLE" in unf.scan_line())

    blk = disc.parse_mediainfo(BLANK)
    check("blank kind", blk.kind == "blank")
    check("blank no data", blk.has_data is False)
    check("blank blocker", "blank" in (blk.blocker or "").lower())

    rdy = disc.parse_mediainfo(MEDIAINFO)   # the finalized sample from test 2
    check("ready kind", rdy.kind == "ready")
    check("ready imageable", rdy.imageable is True)
    check("ready no blocker", rdy.blocker is None)


# ---- 6. JPEG carving (for unfinalized-disc still recovery) ---------------
def _mini_jpeg(app=b"\xff\xe0", payload=b"\xaa\xbb"):
    """A structurally-valid (not pixel-valid) JPEG: SOI, one APPn segment, SOS,
    scan data with byte-stuffing + a restart marker, EOI."""
    seg = app + (len(payload) + 2).to_bytes(2, "big") + payload
    sos = b"\xff\xda" + (4).to_bytes(2, "big") + b"\xcc\xdd"
    scan = b"\x11\x22\x33\xff\x00\x44\x55\xff\xd0\x66\x77"   # FF00 stuffing, FFD0 restart
    return b"\xff\xd8" + seg + sos + scan + b"\xff\xd9"


def test_stills():
    real = _mini_jpeg()
    # buffer: noise, a FALSE FF D8 FF C0 (SOF0, not APPn → must be ignored), the JPEG, noise
    buf = (b"\x00\x11" * 50) + b"\xff\xd8\xff\xc0\x01\x02\x03" + (b"\x7f" * 20) \
        + real + (b"\x00" * 40)
    found = stills.find_jpegs(buf, min_size=8)
    check("carve finds exactly one jpeg", len(found) == 1)
    check("carve returns the exact bytes", found and found[0] == real)
    check("carve rejects bare/false SOI", b"\xff\xc0" not in found[0][:4] if found else False)

    # thumbnail trap: APP1 segment that *contains* a full nested JPEG (the EXIF
    # thumbnail). A naive next-FFD9 search would stop early; we must return the
    # OUTER image whole.
    thumb = _mini_jpeg()
    outer = (b"\xff\xd8" + b"\xff\xe1" + (len(thumb) + 2).to_bytes(2, "big") + thumb
             + b"\xff\xda" + (4).to_bytes(2, "big") + b"\xcc\xdd"
             + b"\x11\x22\x33" + b"\xff\xd9")
    found2 = stills.find_jpegs(outer, min_size=8)
    check("thumbnail trap: one jpeg, not two", len(found2) == 1)
    check("thumbnail trap: full image, not truncated", found2 and found2[0] == outer)


# ---- 7. TUI icons / chips ------------------------------------------------
def test_icons():
    # nerd glyphs are Private-Use (>= 0xE000); unicode fallback is printable BMP
    g_nerd, color = icons.level_icon("ok", nerd=True)
    g_uni, _ = icons.level_icon("ok", nerd=False)
    check("level ok colour", color == "green")
    check("level nerd is PUA glyph", len(g_nerd) == 1 and ord(g_nerd) >= 0xE000)
    check("level unicode fallback differs", g_uni == "✓" and g_uni != g_nerd)
    check("unknown level -> info",
          icons.level_icon("bogus", True) == icons.level_icon("info", True))

    sg, scol = icons.step_icon("run", nerd=False)
    check("step run unicode", sg == "◐" and scol == "yellow")

    # field glyphs: present in nerd mode, empty in unicode mode (clean chip)
    check("field nerd present", icons.field_icon("device", True) != "")
    check("field unicode empty", icons.field_icon("media", False) == "")

    c = icons.chip("DVD-ROM", "white", "#1f4e79", icons.field_icon("media", True))
    check("chip has bg+fg", c.startswith("[white on #1f4e79]") and c.endswith("[/]"))
    check("chip carries text", "DVD-ROM" in c)
    c2 = icons.chip("finalized", "black", "#2e7d32", "")  # no glyph
    check("chip without icon = bare text", c2 == "[black on #2e7d32] finalized [/]")
    check("chip_plain form", icons.chip_plain("1350 MB") == "[1350 MB]")


# ---- 8. completion notification helpers (pure parts only) ----------------
def test_notify():
    check("events: complete/warning/error",
          set(notify._EVENTS) == {"complete", "warning", "error"})
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "complete.oga"), "w"):
        pass
    check("find_sound hit", notify._find_sound(["complete.oga"], dirs=[d]) is not None)
    check("find_sound miss", notify._find_sound(["nope.oga"], dirs=[d]) is None)
    check("find_sound first match wins",
          notify._find_sound(["nope.oga", "complete.oga"], dirs=[d]).endswith("complete.oga"))


# ---- 9. contact / montage helpers (pure parts only) ----------------------
def test_contact():
    check("hms mm:ss", contact._hms(125) == "0:02:05")
    check("hms hh:mm:ss", contact._hms(3661) == "1:01:01")
    d = tempfile.mkdtemp()
    vd, pd = os.path.join(d, "video"), os.path.join(d, "photos")
    os.makedirs(vd)
    os.makedirs(pd)
    for n in ("01.mkv", "02.mp4"):
        open(os.path.join(vd, n), "w").close()
    for n in ("a.jpg", "b.JPG", "c.png"):
        open(os.path.join(pd, n), "w").close()
    vids, photos = contact._gather(vd, pd)
    check("gather finds both videos", len(vids) == 2)
    check("gather finds all photos", len(photos) == 3)
    check("gather sorted", vids == sorted(vids))


# ---- 10. carve: find MPEG-2 pack header past a filesystem prefix ----------
def test_carve_pack():
    # 2 sectors of junk, then a pack header at the start of sector 2
    blob = (b"\x00" * (2 * 2048)) + b"\x00\x00\x01\xba" + (b"\xff" * 100)
    with tempfile.NamedTemporaryFile("wb", suffix=".iso", delete=False) as f:
        f.write(blob)
        path = f.name
    end = (len(blob) + 2047) // 2048
    check("pack header found at sector 2", carve._first_pack_lba(path, 0, end + 1) == 2)
    check("no pack header -> -1",
          carve._first_pack_lba(path, 0, 2) == -1)   # search only the junk sectors
    os.unlink(path)


# ---- 11. describe: slug / duration / name parsing / TSV ------------------
def test_describe():
    check("slug accents+spaces",
          describe._slug("Anniversaire Mamie 70 ans") == "Anniversaire-Mamie-70-ans")
    check("slug drops punctuation", describe._slug("Foot: PSG vs OM!") == "Foot-PSG-vs-OM")
    check("dur minutes", describe._dur_tag(750) == "12m30s")
    check("dur hours", describe._dur_tag(3725) == "1h02m05s")
    check("session prefix", describe._session("01__2005-08-21_17h30.mkv") == "01")
    check("date in name", describe._date_in_name("01__2005-08-21_17h30.mkv") == "2005-08-21")
    check("time in name", describe._time_in_name("01__2005-08-21_17h30.mkv") == (17, 30))
    check("no time -> None", describe._time_in_name("01__2005-08-21__fete__1m.mkv") is None)
    p = os.path.join(tempfile.mkdtemp(), "descriptions.tsv")
    describe._write_tsv(p, [{"session": "01", "date": "2005-08-21",
                             "duration": "12m30s", "description": "Fête à Mamie"}])
    rows = describe._read_tsv(p)
    check("tsv roundtrip", rows["01"]["description"] == "Fête à Mamie")


if __name__ == "__main__":
    test_describe()
    test_carve_pack()
    test_label_dt()
    test_mediainfo()
    test_mapfile()
    test_isoinfo()
    test_triage()
    test_stills()
    test_icons()
    test_notify()
    test_contact()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
