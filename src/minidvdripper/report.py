"""Per-disc rip report: machine-readable JSON + human-readable TXT.

Captures everything an archivist wants later: disc identity, finalization, the
exact bit-rot map (which sectors / which files / how many bytes), per-session
durations, decode-verification result, and SHA-256 of each MKV for integrity.
"""
from __future__ import annotations

import hashlib
import json
import os


def sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def build(disc, rot, titles, remuxes, photos, affected_files,
          tool_versions: dict, started: str, finished: str,
          with_hashes: bool = True) -> dict:
    rmap = {r.out_path: r for r in remuxes}
    rep = {
        "tool": "minidvdripper",
        "started": started,
        "finished": finished,
        "disc": {
            "label": disc.label,
            "datetime": disc.datetime.isoformat() if disc.datetime else None,
            "media": disc.media,
            "media_id": disc.media_id,
            "finalized": disc.finalized,
            "disc_status": disc.disc_status,
            "last_session_state": disc.last_session_state,
            "sessions": disc.sessions,
            "capacity_bytes": disc.capacity_bytes,
            "warnings": disc.warnings,
        },
        "bit_rot": {
            "clean": rot.clean,
            "total_bytes": rot.total_bytes,
            "recovered_bytes": rot.recovered_bytes,
            "bad_bytes": rot.bad_bytes,
            "recovered_pct": round(rot.recovered_pct, 6),
            "bad_regions": [
                {"start_lba": r.start_lba, "end_lba": r.end_lba,
                 "sectors": r.sectors, "status": r.status}
                for r in rot.bad_ranges
            ],
            "affected_files": [
                {"path": f.path, "bad_sectors": f.bad_sectors,
                 "bad_bytes": f.bad_sectors * 2048}
                for f in affected_files
            ],
        },
        "sessions": [],
        "photos": {"count": photos.count, "files": [os.path.basename(p) for p in photos.copied]},
        "tools": tool_versions,
    }
    for t in titles:
        out = next((r.out_path for r in remuxes
                    if os.path.basename(r.out_path).startswith(f"{t.number:02d}__")), None)
        r = rmap.get(out)
        entry = {
            "session": t.number,
            "kind": t.kind,
            "datetime": t.datetime.isoformat() if t.datetime else None,
            "source_parts": [os.path.basename(p) for p in t.parts],
            "source_bytes": t.size_bytes,
            "mkv": os.path.basename(out) if out else None,
            "duration_s": round(r.duration, 2) if r else None,
            "streams": r.streams if r else None,
            "decode_errors": r.decode_errors if r else None,
            "chapters": len(t.chapters),
        }
        if with_hashes and out and os.path.exists(out):
            entry["mkv_sha256"] = sha256(out)
            entry["mkv_bytes"] = os.path.getsize(out)
        rep["sessions"].append(entry)
    return rep


def write(rep: dict, json_path: str, txt_path: str) -> None:
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(_human(rep))


def _human(rep: dict) -> str:
    d = rep["disc"]
    rot = rep["bit_rot"]
    L = []
    L.append("=" * 64)
    L.append(f"  MiniDVD rip report — {d['label'] or '(no label)'}")
    L.append("=" * 64)
    L.append(f"Recorded     : {d['datetime'] or 'unknown'}")
    L.append(f"Media        : {d['media']}  (id {d['media_id'] or '?'})")
    L.append(f"Finalized    : {'yes' if d['finalized'] else 'NO — see warnings'}")
    L.append(f"Sessions     : {len(rep['sessions'])}    Photos: {rep['photos']['count']}")
    L.append("")
    if rot["clean"]:
        L.append("BIT ROT      : none — 100% recovered, clean rip.")
    else:
        L.append(f"BIT ROT      : *** {rot['bad_bytes']:,} bytes UNRECOVERABLE "
                 f"({rot['recovered_pct']:.4f}% recovered) ***")
        for f in rot["affected_files"]:
            L.append(f"   - {f['path']}: {f['bad_sectors']} bad sector(s), "
                     f"{f['bad_bytes']:,} bytes")
        if not rot["affected_files"]:
            L.append("   (bad sectors fall outside catalogued files — see bad_regions)")
    if d["warnings"]:
        L.append("")
        for w in d["warnings"]:
            L.append(f"WARNING      : {w}")
    L.append("")
    L.append("-" * 64)
    L.append("Sessions (one lossless MKV each):")
    for s in rep["sessions"]:
        dur = f"{s['duration_s']:.0f}s" if s["duration_s"] else "?"
        de = s["decode_errors"]
        flag = "" if not de else f"  [!! {de} decode errors]"
        L.append(f"  {s['session']:02d}  {s['mkv']}")
        L.append(f"      date {s['datetime'] or '?'} | {dur} | {s['streams'] or ''}{flag}")
    L.append("")
    L.append("Tools: " + ", ".join(f"{k} {v}" for k, v in rep["tools"].items()))
    return "\n".join(L) + "\n"
