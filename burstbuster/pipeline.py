"""Scan a folder and build the report. Read-only: never moves, renames or writes a photo."""
import json
import multiprocessing
import os
import re
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from . import APP_DIR, features, group, models, pick, scan

FEATURE_VERSION = 4  # bump when measure() output changes, to invalidate the cache


def _cache():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(APP_DIR / "cache.sqlite")
    db.execute("CREATE TABLE IF NOT EXISTS feats(path TEXT PRIMARY KEY, size INT, mtime INT, ver INT, data TEXT)")
    return db


def clear_cache():
    p = APP_DIR / "cache.sqlite"
    if p.exists():
        p.unlink()


def cached_feats(paths):
    """Saved measurements by original path (moving to the holding folder keeps size and mtime)."""
    db = _cache()
    out = {}
    for p in paths:
        row = db.execute("SELECT data FROM feats WHERE path=? AND ver=?", (p, FEATURE_VERSION)).fetchone()
        if row:
            out[p] = json.loads(row[0])
    db.close()
    return out


def burst_group(shots, feats, level, chosen=None):
    """Pick within one burst and describe it for the report. shots: time-ordered, each with .id."""
    r = pick.pick_burst(shots, feats, level, chosen)
    return r, {"type": "burst", "confidence": r["confidence"], "flags": r["flags"], "top": r["top"],
               "focus": ("chosen" if chosen else "auto") if r["subject"] else None,
               "shots": [dict(_shot_out(s, feats, d["keep"], d["reasons"], d["moment"], d["role"]),
                              subject=r["subject"].get(s.id))
                         for s in shots for d in [r["shots"][s.id]]]}


def _measure_safe(path):
    try:
        return path, features.measure(path), None
    except Exception as e:  # one bad file must not stop the scan; it is simply left alone
        return path, None, f"{type(e).__name__}: {e}"


def _worker_count():
    return max(1, min((os.cpu_count() or 2) // 2, 8))


def analyze(root, recursive=True, level="pose", progress=None, cancelled=None, log=print, ai=None):
    """Return the report dict. progress(stage, done, total); cancelled() -> bool.
    ai: optional {"url", "model"} of a local vision model, consulted only on close calls."""
    progress = progress or (lambda *a: None)
    cancelled = cancelled or (lambda: False)
    t0 = time.time()
    root = os.path.abspath(root)
    progress("Finding photos", 0, 0)
    shots, skipped_cloud = scan.inventory(root, recursive)

    db = _cache()
    feats, todo, errors = {}, [], []
    for s in shots:
        size, mtime = os.path.getsize(s.primary), os.stat(s.primary).st_mtime_ns
        row = db.execute("SELECT data FROM feats WHERE path=? AND size=? AND mtime=? AND ver=?",
                         (s.primary, size, mtime, FEATURE_VERSION)).fetchone()
        if row:
            feats[s.id] = json.loads(row[0])
        else:
            todo.append(s)

    if todo:
        paths = models.ensure_models(log)
        total = len(todo)
        progress("Looking at photos", len(shots) - total, len(shots))
        # spawn everywhere: forking a process that runs GUI and model threads can hang (Linux default)
        with ProcessPoolExecutor(_worker_count(), multiprocessing.get_context("spawn"), features.init_models,
                                 (paths,)) as ex:
            futs = [ex.submit(_measure_safe, s.primary) for s in todo]
            for i, fut in enumerate(as_completed(futs), 1):
                if cancelled():
                    ex.shutdown(cancel_futures=True)
                    db.commit()
                    return None
                path, data, err = fut.result()
                if err:
                    errors.append({"path": path, "error": err})
                else:
                    feats[path] = data
                    st = os.stat(path)
                    db.execute("INSERT OR REPLACE INTO feats VALUES (?,?,?,?,?)",
                               (path, st.st_size, st.st_mtime_ns, FEATURE_VERSION, json.dumps(data)))
                progress("Looking at photos", len(shots) - total + i, len(shots))
        db.commit()
    db.close()

    shots = [s for s in shots if s.id in feats]  # unreadable files are never suggested for removal
    progress("Choosing the best", 0, 0)
    groups = []

    # Byte-identical copies first; only the kept copy goes on to burst analysis.
    dupe_ids = set()
    for g in scan.exact_duplicates(shots):
        g = sorted(g, key=_copy_rank)
        keep = g[0]
        groups.append({"type": "duplicate", "confidence": "confident", "flags": [], "top": keep.id,
                       "shots": [_shot_out(keep, feats, True, [_keep_reason(keep, g[1])], 0, "pick")] +
                                [_shot_out(s, feats, False, [f"Exact copy of {os.path.relpath(keep.primary, root)}"], 0,
                                           "duplicate") for s in g[1:]]})
        dupe_ids |= {s.id for s in g[1:]}
    shots = [s for s in shots if s.id not in dupe_ids]

    # Smaller saves of the same capture: the picker would judge them on blur and smile, and could keep one.
    for g in group.resized_copies(shots, feats):
        keep = g[0]
        groups.append({"type": "duplicate", "confidence": "confident", "flags": [], "top": keep.id,
                       "shots": [_shot_out(keep, feats, True, [f"Full-size original ({_dims(keep)})"], 0, "pick")] +
                                [_shot_out(s, feats, False, [f"Smaller copy ({_dims(s)}) of "
                                                             f"{os.path.relpath(keep.primary, root)}"], 0, "duplicate")
                                 for s in g[1:]]})
        dupe_ids |= {s.id for s in g[1:]}
    shots = [s for s in shots if s.id not in dupe_ids]

    pending_ai = []
    for b in group.bursts(shots, feats):
        r, g = burst_group(b, feats, level)
        groups.append(g)
        pending_ai += [(g, ids) for ids in r["close_sets"]]

    if ai and pending_ai:
        from . import vlm
        for i, (g, ids) in enumerate(pending_ai):
            if cancelled():
                return None
            progress("Asking the AI about close calls", i, len(pending_ai))
            labels = "ABC"[:len(ids)]
            try:
                cands = [(lab, features.load_image(sid), (feats[sid]["faces"] or [{}])[0].get("box"))
                         for lab, sid in zip(labels, ids)]
                best, reason = vlm.choose(ai["url"], ai["model"], cands)
                pick.apply_ai_choice(g["shots"], ids, ids[labels.index(best)], reason)
            except Exception as e:  # the metric pick stands; tell the user why the AI didn't weigh in
                g["flags"].append(f"AI second opinion unavailable ({type(e).__name__})")
    for i, g in enumerate(groups):
        g["id"] = i
    groups.sort(key=lambda g: (g["confidence"] != "close", g["type"] != "burst", g["id"]))

    aside = [s for g in groups for s in g["shots"] if not s["keep"]]
    return {
        "root": root, "recursive": recursive, "level": level, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round(time.time() - t0, 1),
        "photos": len(shots) + len(dupe_ids), "groups": groups, "errors": errors,
        "skipped_cloud": skipped_cloud,
        "stats": {"groups": len(groups), "set_aside": len(aside), "bytes": sum(s["size"] for s in aside)},
    }


_COPY_NAME = re.compile(r"(copy( \d+)?|\(\d+\)|-\d)$", re.IGNORECASE)  # "x - Copy", "x copy 2", "x (1)", "x-1"


def _copy_rank(s):
    """Which of byte-identical copies to keep: the one with sidecars / Live Photo video, then the one whose
    name does not look like a copy, then the shortest path."""
    return (-len(s.files), bool(_COPY_NAME.search(Path(s.primary).stem)), len(s.primary), s.primary)


def _keep_reason(keep, runner_up):
    if len(keep.files) > len(runner_up.files):
        return "Original copy (has its companion files)"
    if _copy_rank(keep)[1] < _copy_rank(runner_up)[1]:
        return "Original copy (the other name looks like a copy)"
    return "Original copy (shortest path)"


def _dims(s):
    w, h = s.exif["dims"]
    return f"{w}x{h}"


def _shot_out(s, feats, keep, reasons, moment, role):
    f = feats[s.id]
    faces = f["faces"]
    return {
        "id": s.id, "files": [p for p, _, _ in s.files], "stat": [[p, sz, mt] for p, sz, mt in s.files],
        "size": s.size, "time": s.time, "keep": keep, "reasons": reasons, "moment": moment, "role": role,
        "metrics": {
            "sharp": round(f["sharp"], 1), "subject": f["subject_kind"], "faces": len(faces),
            "eyes_open": all(max(x["blink_l"], x["blink_r"]) <= features.EYE_CLOSED for x in faces) if faces else None,
            "smile": round(min((x["smile"] for x in faces), default=0), 2) if faces else None,
            "clip_hi": round(f["clip_hi"] * 100, 2),
        },
        "face_box": faces[0]["box"] if faces else None, "img_w": f["w"], "img_h": f["h"],
        "people": [p["box"] for p in f.get("persons", [])],
    }
