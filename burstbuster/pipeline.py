"""Scan a folder and build the report. Read-only: never moves, renames or writes a photo."""
import json
import multiprocessing
import os
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from . import APP_DIR, features, group, merge, models, pick, scan

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


def analyze(root, recursive=True, level="pose", progress=None, cancelled=None, log=print, ai=None, mode="bursts"):
    """Return the report dict. progress(stage, done, total); cancelled() -> bool.
    ai: optional {"url", "model"} of a local vision model, consulted only on close calls.
    mode "dupes": copies only, without the models or burst picking (much faster)."""
    progress = progress or (lambda *a: None)
    cancelled = cancelled or (lambda: False)
    t0 = time.time()
    root = os.path.abspath(root)
    progress("Finding photos", 0, 0)
    shots, skipped_cloud = scan.inventory(root, recursive)

    feats, errors = ({}, []) if mode == "dupes" else _measure_all(shots, progress, cancelled, log)
    if feats is None:
        return None
    if mode != "dupes":
        shots = [s for s in shots if s.id in feats]  # unreadable files are never suggested for removal
    photos = len(shots)
    progress("Comparing copies" if mode == "dupes" else "Choosing the best", 0, 0)
    groups = []

    # Copies first, surest kind first; only the kept copy goes on to the next step.
    for g in scan.exact_duplicates(shots):
        _add_copies(groups, sorted(g, key=_copy_rank), feats, lambda k, s: f"Exact copy of {_rel(k, root)}")
    shots = _not_aside(shots, groups)
    if cancelled():
        return None
    for g in scan.same_pixels(shots):
        _add_copies(groups, sorted(g, key=_copy_rank), feats, lambda k, s: f"Same picture as {_rel(k, root)}, "
                    + ("fewer photo details" if s.exif.get("tags", 0) < k.exif.get("tags", 0)
                       else "different photo details"))
    shots = _not_aside(shots, groups)

    # Smaller saves of the same capture: the picker would judge them on blur and smile, and could keep one.
    looks = feats
    if mode == "dupes":  # no embeddings: compare tiny thumbnails, only where shots share a capture time
        n = Counter((s.camera, s.time) for s in shots if s.time is not None and s.exif.get("dims"))
        looks = {s.id: {"emb": scan.look(s.primary)} for s in shots if n[(s.camera, s.time)] > 1}
    for g in group.resized_copies(shots, looks):
        _add_copies(groups, g, feats, lambda k, s: f"Smaller copy ({_dims(s)}) of {_rel(k, root)}",
                    f"Full-size original ({_dims(g[0])})")
    shots = _not_aside(shots, groups)

    pending_ai = []
    for b in group.bursts(shots, feats) if mode != "dupes" else []:
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
        if g["type"] == "duplicate":  # what "keep photo details" would carry over, shown in the review
            k = next(s for s in g["shots"] if s["id"] == g["top"])
            g["carry"] = merge.describe(*merge.plan(merge.details(k["id"], k["files"]),
                                                    [merge.details(s["id"], s["files"]) for s in g["shots"] if s is not k]))
    groups.sort(key=lambda g: (g["confidence"] != "close", g["type"] != "burst", g["id"]))

    aside = [s for g in groups for s in g["shots"] if not s["keep"]]
    return {
        "root": root, "recursive": recursive, "level": level, "mode": mode,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1),
        "photos": photos, "groups": groups, "errors": errors,
        "skipped_cloud": skipped_cloud,
        "stats": {"groups": len(groups), "set_aside": len(aside), "bytes": sum(s["size"] for s in aside)},
    }


def _add_copies(groups, g, feats, why, keep_reason=None):
    """Add a group for copies of one picture; g[0] is kept. An earlier group that shares a photo with this one
    (its keeper is kept or set aside here) joins this one, so each photo appears in one group only."""
    keep = g[0]
    new = {"type": "duplicate", "confidence": "confident", "flags": [], "top": keep.id,
           "shots": [_shot_out(keep, feats, True, [keep_reason or _keep_reason(keep, g[1])], 0, "pick")] +
                    [_shot_out(s, feats, False, [why(keep, s)], 0, "duplicate") for s in g[1:]]}
    ids = {s.id for s in g}
    for old in [x for x in groups if x["top"] in ids]:
        groups.remove(old)
        if old["top"] == keep.id:
            new["shots"][0]["reasons"][:0] = old["shots"][0]["reasons"]
        new["shots"] += [s for s in old["shots"] if not s["keep"]]
    groups.append(new)


def _not_aside(shots, groups):
    aside = {s["id"] for g in groups for s in g["shots"] if not s["keep"]}
    return [s for s in shots if s.id not in aside]


def _rel(s, root):
    return os.path.relpath(s.primary, root)


def _measure_all(shots, progress, cancelled, log):
    """Model measurements for every shot, from the cache where unchanged. (feats, errors), or (None, None)
    when cancelled."""
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
                    return None, None
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
    return feats, errors


_COPY_NAME = re.compile(r"(copy( \d+)?|\(\d+\)|-\d)$", re.IGNORECASE)  # "x - Copy", "x copy 2", "x (1)", "x-1"

# Which copy of one picture to keep, in order: the first rule that tells two copies apart decides.
_KEEP_RULES = [
    (lambda s: -len(s.files), "has its companion files"),
    (lambda s: -s.exif.get("tags", 0), "more photo details: date, camera, place, rating"),
    (lambda s: bool(_COPY_NAME.search(Path(s.primary).stem)), "the other name looks like a copy"),
    (lambda s: len(s.primary), "shortest path"),
    (lambda s: min(mt for _, _, mt in s.files), "oldest file"),
]


def _copy_rank(s):
    return tuple(rule(s) for rule, _ in _KEEP_RULES) + (s.primary,)


def _keep_reason(keep, runner_up):
    why = next((w for rule, w in _KEEP_RULES if rule(keep) != rule(runner_up)), "shortest path")
    return f"Original copy ({why})"


def _dims(s):
    w, h = s.exif["dims"]
    return f"{w}x{h}"


def _shot_out(s, feats, keep, reasons, moment, role):
    out = {"id": s.id, "files": [p for p, _, _ in s.files], "stat": [[p, sz, mt] for p, sz, mt in s.files],
           "size": s.size, "time": s.time, "keep": keep, "reasons": reasons, "moment": moment, "role": role,
           "dims": s.exif.get("dims"), "metrics": None, "face_box": None, "people": []}
    f = feats.get(s.id)
    if not f:  # copies-only scan: nothing measured
        return out
    faces = f["faces"]
    out.update({
        "metrics": {
            "sharp": round(f["sharp"], 1), "subject": f["subject_kind"], "faces": len(faces),
            "eyes_open": all(max(x["blink_l"], x["blink_r"]) <= features.EYE_CLOSED for x in faces) if faces else None,
            "smile": round(min((x["smile"] for x in faces), default=0), 2) if faces else None,
            "clip_hi": round(f["clip_hi"] * 100, 2),
        },
        "face_box": faces[0]["box"] if faces else None, "img_w": f["w"], "img_h": f["h"],
        "people": [p["box"] for p in f.get("persons", [])],
    })
    return out
