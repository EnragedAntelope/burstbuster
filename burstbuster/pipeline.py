"""Scan a folder and build the report. Read-only: never moves, renames or writes a photo."""
import json
import math
import multiprocessing
import os
import re
import sqlite3
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from . import APP_DIR, features, group, merge, models, pick, scan

# bump when measure() output changes, to invalidate the cache; at the next bump also drop the unused
# clip_lo, jaw_open and n_people from features.measure()
FEATURE_VERSION = 4


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
    return r, {"type": "burst", **burst_fields(r, chosen),
               "shots": [dict(_shot_out(s, feats, d["keep"], d["reasons"], d["moment"], d["role"]),
                              subject=r["subject"].get(s.id))
                         for s in shots for d in [r["shots"][s.id]]]}


def burst_fields(r, chosen):
    """A burst group's fields that come from pick.pick_burst result `r`."""
    return {"confidence": r["confidence"], "flags": r["flags"], "top": r["top"],
            "focus": ("chosen" if chosen else "auto") if r["subject"] else None}


def _measure_safe(path):
    try:
        return path, features.measure(path), None
    except Exception as e:  # one bad file must not stop the scan; it is simply left alone
        return path, None, f"{type(e).__name__}: {e}"


def _worker_count():
    return max(1, min((os.cpu_count() or 2) // 2, 8))


def analyze(root, recursive=True, level="pose", progress=None, cancelled=None, log=print, ai=None, mode="bursts",
            keep_details=False):
    """Return the report dict. progress(stage, done, total); cancelled() -> bool.
    ai: optional {"url", "model"} of a local vision model, consulted only on close calls.
    mode "dupes": copies only, without the models or burst picking (much faster).
    keep_details: the "Keep photo details" option (merge.py) is on; without it, a set of copies holding details
    the kept one lacks is marked for a look."""
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
    # Tiny colour thumbnails, not the model embedding: that ignores colour, so a black-and-white edit would match.
    n = Counter((s.camera, s.time) for s in shots if s.time is not None and s.exif.get("dims"))
    looks = {s.id: scan.look(s.primary) for s in shots if n[(s.camera, s.time)] > 1}
    for g in group.resized_copies(shots, looks):
        _add_copies(groups, g, feats, lambda k, s: f"Smaller copy ({_dims(s)}) of {_rel(k, root)}",
                    f"Full-size original ({_dims(g[0])})")
    shots = _not_aside(shots, groups)

    # Edited versions (IMG_E1234 next to IMG_1234): the picker would judge them as burst frames. The original is
    # kept; the set is shown first, since either one may be the one wanted.
    for g in edited_copies(shots):
        _add_copies(groups, g, feats, lambda k, s: f"Edited version of {_rel(k, root)}",
                    "Camera original (keep the edited version instead if you prefer it)")
        groups[-1]["confidence"] = "close"
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
                choice = ids[labels.index(best)]
                pick.apply_ai_choice(g["shots"], ids, choice, reason)
                if g["top"] == ids[0]:
                    g["top"] = choice
            except Exception as e:  # the metric pick stands; tell the user why the AI didn't weigh in
                g["flags"].append(f"AI second opinion unavailable ({type(e).__name__})")
    # A kept copy that burst picking set aside: its copies go with it into the burst card, so each photo shows once
    aside = {s["id"]: (g, s["moment"]) for g in groups if g["type"] == "burst" for s in g["shots"] if not s["keep"]}
    for d in [g for g in groups if g["type"] == "duplicate" and g["top"] in aside]:
        b, moment = aside[d["top"]]
        b["shots"] += [dict(s, moment=moment) for s in d["shots"] if not s["keep"]]
        if d["confidence"] == "close":
            b["confidence"] = "close"
        groups.remove(d)
    for i, g in enumerate(groups):
        g["id"] = i
        if g["type"] == "duplicate":  # what "keep photo details" would carry over, shown in the review
            k = next(s for s in g["shots"] if s["id"] == g["top"])
            add, conflicts, differs = merge.plan(merge.details(k["id"], k["files"]),
                                                 [merge.details(s["id"], s["files"]) for s in g["shots"] if s is not k])
            g["carry"] = merge.describe(add, conflicts, differs)
            # details only the copies have, which recycling them would lose: without the option, or when the kept
            # copy already has a sidecar (never edited), all of them; with it, the ones the copies disagree on
            sidecar = any(p.lower().endswith(".xmp") for p in k["files"]) or os.path.exists(merge.sidecar_path(k["id"]))
            lost = conflicts + (list(add) if not keep_details or sidecar else [])
            if lost:
                g["confidence"] = "close"
                g["flags"].append("Only the copies have: " + ", ".join(dict.fromkeys(merge.FIELDS[f] for f in lost)))
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


# an edited copy's name and its original's: "IMG_E1234" (iPhone), "x-edited" (Google Takeout), "x-copy(1)"
_EDIT_NAME = re.compile(r"(IMG_)E(\d+)|(.+?)[-_ ](?:edited|copy\(\d+\))", re.IGNORECASE)


def edited_copies(shots):
    """Groups of [original, edited version, ...]: a name marked as an edit of another photo in the same folder,
    taken in the same second (an edit keeps the capture time; its pixels and size may differ)."""
    second = lambda s: (os.path.dirname(s.primary).lower(), math.floor(s.time))  # noqa: E731
    originals = {(*second(s), Path(s.primary).stem.lower()): s for s in shots if s.time is not None}
    out = {}  # an edit of an edit gives a second set; _add_copies merges the two
    for s in shots:
        m = s.time is not None and _EDIT_NAME.fullmatch(Path(s.primary).stem)
        orig = m and originals.get((*second(s), (m[1] + m[2] if m[1] else m[3]).lower()))
        if orig and orig is not s:
            out.setdefault(orig.id, [orig]).append(s)
    return list(out.values())


def _measure_all(shots, progress, cancelled, log):
    """Model measurements for every shot, from the cache where unchanged. (feats, errors), or (None, None)
    when cancelled."""
    db = _cache()
    feats, todo, errors = {}, [], []
    for s in shots:
        row = db.execute("SELECT data FROM feats WHERE path=? AND size=? AND mtime=? AND ver=?",
                         (s.primary, *s.stat, FEATURE_VERSION)).fetchone()
        if row:
            feats[s.id] = json.loads(row[0])
        else:
            todo.append(s)

    if todo:
        paths = models.ensure_models(log)
        stats = {s.primary: s.stat for s in todo}  # the walk's view: set_aside checks it again before moving
        done = set()

        def record(path, data, err):
            done.add(path)
            if err:
                errors.append({"path": path, "error": err})
            else:
                feats[path] = data
                db.execute("INSERT OR REPLACE INTO feats VALUES (?,?,?,?,?)",
                           (path, *stats[path], FEATURE_VERSION, json.dumps(data)))
            if len(done) % 50 == 0:
                db.commit()  # a crash or power cut keeps what was measured
            progress("Looking at photos", len(shots) - len(todo) + len(done), len(shots))

        progress("Looking at photos", len(shots) - len(todo), len(shots))
        left, suspects, blamed = [s.primary for s in todo], 0, 0
        while left:
            if cancelled():
                db.commit()
                return None, None
            # A photo that crashes a worker (native decoder, out of memory) breaks the whole pool, and which one
            # did it is unknown. The photos that were in flight are then measured one at a time to find it.
            batch = left[:suspects] or left
            # spawn everywhere: forking a process that runs GUI and model threads can hang (Linux default)
            with ProcessPoolExecutor(1 if suspects else _worker_count(), multiprocessing.get_context("spawn"),
                                     features.init_models, (paths,)) as ex:
                try:
                    if suspects:
                        results = (ex.submit(_measure_safe, p).result() for p in batch)
                    else:
                        results = (f.result() for f in as_completed([ex.submit(_measure_safe, p) for p in batch]))
                    for r in results:
                        if cancelled():
                            ex.shutdown(cancel_futures=True)
                            db.commit()
                            return None, None
                        record(*r)
                        blamed = 0
                    suspects = 0
                except BrokenProcessPool:
                    if suspects:  # one at a time: the first unfinished photo is the one that broke the pool
                        blamed += 1
                        if blamed >= 3:  # photo after photo with none measured between: the models can't start
                            db.commit()
                            raise RuntimeError("The photo analyser could not start (its worker process crashed "
                                               "before measuring any photo)") from None
                        record(next(p for p in batch if p not in done), None, "The analyser crashed on this photo")
                    suspects = 0 if suspects else 2 * _worker_count() + 2  # running plus queued at most
            left = [p for p in left if p not in done]
        db.commit()
    db.close()
    return feats, errors


# "x - Copy", "x copy 2", "x (1)", "x-1", "x 1" (macOS), "x-edited", "IMG_E1234"; and "x_Original"
_COPY_NAME = re.compile(r"(copy( \d+)?|\(\d+\)|-\d| \d{1,2}|[-_ ]edited)$|^IMG_E\d", re.IGNORECASE)
_ORIGINAL_NAME = re.compile(r"[-_ ]original$", re.IGNORECASE)


def _name_rank(path):
    stem = Path(path).stem
    return 0 if _ORIGINAL_NAME.search(stem) else 2 if _COPY_NAME.search(stem) else 1


# Which copy of one picture to keep, in order: the first rule that tells two copies apart decides.
_KEEP_RULES = [
    (lambda s: -len(s.files), "has its companion files"),
    (lambda s: -s.exif.get("tags", 0), "more photo details: date, camera, place, rating"),
    (lambda s: _name_rank(s.primary), "its name looks like the original"),
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
