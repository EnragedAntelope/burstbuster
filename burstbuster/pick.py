"""Choose keepers in a burst: filter by hard checks, then rank. Every decision carries
plain-English reasons. Relative within the burst, so absolute metric scales don't matter."""
import statistics

from . import group
from .features import EYE_CLOSED

KEEP_SHARP = 0.80       # within one pose, softer than this fraction of the best = set aside
OTHER_POSE_SHARP = 0.60 # a different pose is kept unless it is this much blurrier than the best
CLIP_MARGIN = 0.02      # extra fraction of blown-out pixels tolerated vs the cleanest shot
DARKER = 0.80           # within one pose, under this fraction of the brightest shot's brightness = underexposed
OTHER_POSE_DARK = 0.70  # across poses (a much darker frame often looks like a new pose to the grouping)
SMILE_STEP = 0.15       # smile difference that counts as noticeable
CLOSE_SHARP = 0.95      # runner-up this close in sharpness = close call
SAME_PERSON = 0.65      # person-crop similarity between frames: same person ~0.7-0.9, others ~0.2-0.6
SMALLER = 0.5           # main person under this fraction of their biggest size in the burst = less visible


def _closed(f):
    return [x for x in f["faces"] if max(x["blink_l"], x["blink_r"]) > EYE_CLOSED]


def _facing(f, pitch0):
    if not f["faces"]:
        return 0.0
    return max(abs(x["yaw"]) + abs(x["pitch"] - pitch0) for x in f["faces"])


def _smile(f):
    if f.get("subj_face"):
        return f["subj_face"]["smile"]
    return min((x["smile"] for x in f["faces"]), default=0.0)


def _smile_rank(f):
    return round(_smile(f) / SMILE_STEP)


def tracks(burst, F):
    """Follow each person through the burst (shots in time order): a list of {shot id: person index}.
    A person is matched to whoever looked most alike in the last two frames."""
    out = []
    for fi, s in enumerate(burst):
        ps = F[s.id].get("persons") or []
        live = [t for t in out if fi - t["last"] <= 2]
        pairs = sorted(((group.cos(p["emb"], t["emb"]), j, ti) for j, p in enumerate(ps) for ti, t in enumerate(live)),
                       reverse=True)
        used_p, used_t = set(), set()
        for sim, j, ti in pairs:
            if sim < SAME_PERSON:
                break
            if j not in used_p and ti not in used_t:
                used_p.add(j)
                used_t.add(ti)
                live[ti]["ids"][s.id] = j
                live[ti].update(emb=ps[j]["emb"], last=fi)
        out += [{"ids": {s.id: j}, "emb": p["emb"], "last": fi} for j, p in enumerate(ps) if j not in used_p]
    return [t["ids"] for t in out]


def _size(f, p):
    x0, y0, x1, y1 = p["box"]
    return (x1 - x0) * (y1 - y0) / (f["w"] * f["h"])


def subject(burst, F, chosen=None):
    """The person this burst is about, as {shot id: person index or None}, or None to judge whole frames.
    chosen = (shot id, person index) the user clicked; otherwise the person seen in the most shots,
    preferring big and central. Only used when someone shares the frame with others."""
    ts = tracks(burst, F)
    if chosen:
        t = next((t for t in ts if t.get(chosen[0]) == chosen[1]), None)
    else:
        if not any(len(F[s.id].get("persons") or []) > 1 for s in burst):
            return None

        def score(t):
            def weight(sid):
                f, p = F[sid], F[sid]["persons"][t[sid]]
                cx, cy = (p["box"][0] + p["box"][2]) / 2 / f["w"], (p["box"][1] + p["box"][3]) / 2 / f["h"]
                return _size(f, p) * (1 - min(1, ((cx - .5) ** 2 + (cy - .5) ** 2) ** .5 / .71))
            return len(t), sum(weight(sid) for sid in t) / len(t)
        t = max(ts, key=score, default=None)
        if t and len(t) < max(2, len(burst) / 2):
            t = None
    return {s.id: t.get(s.id) for s in burst} if t else None


def _subject_view(F, subj, chosen):
    """Measurements re-centred on one person. Returns (F, kind)."""
    P = {i: F[i]["persons"][j] if j is not None else None for i, j in subj.items()}
    use_face = all(p["face"] for p in P.values() if p)
    out = {}
    for i, f in F.items():
        p = P[i]
        g = {**f, "subj_face": p and p["face"], "size": _size(f, p) if p else 0.0, "missing": None}
        if p is None:
            g["sharp"] = 0.0
            g["missing"] = "The person you chose isn't in this photo" if chosen else "Main person not in this photo"
        else:
            g["sharp"] = p["face"]["sharp"] if use_face else p["sharp"]
            if p["face"] and p["face"] not in f["faces"]:  # their face counts even if small
                g["faces"] = f["faces"] + [p["face"]]
        out[i] = g
    return out, ("face" if use_face else "subject")


def _sharp_basis(F):
    """One sharpness measure for the whole burst: face, else body, else frame. Mixing them is meaningless
    (a face crop and a whole body differ ~100x), which happens when faces are found in only some shots."""
    fs = list(F.values())
    if all(f["faces"] for f in fs):
        return "sharp", "face"
    if all(f["subject_kind"] == "animal" for f in fs):
        return "sharp", "animal"
    if all(f.get("body_sharp") is not None for f in fs):
        return "body_sharp", "subject"
    return "frame_sharp", "photo"


def _gate_failures(f, ref_sharp, min_clip, face_expected, sharp_limit):
    """Hard problems, worded for the user. Empty list = passes."""
    out = [f["missing"]] if f.get("missing") else []
    closed = _closed(f)
    if closed:
        n = len(f["faces"])
        if n == 1:
            x = closed[0]
            out.append("Eyes closed" if min(x["blink_l"], x["blink_r"]) > EYE_CLOSED else "One eye closed")
        else:
            out.append(f"Eyes closed ({len(closed)} of {n} people)")
    if face_expected and not f["faces"]:
        out.append("Face not visible")
    if ref_sharp > 0 and f["sharp"] < sharp_limit * ref_sharp:
        out.append(f"Blurrier: {int(100 * f['sharp'] / ref_sharp)}% as sharp as the best")
    if f["clip_hi"] > min_clip + CLIP_MARGIN:
        out.append("More blown-out highlights")
    if f.get("dim", 1) < DARKER:
        out.append(f"Darker: {int(100 * f['dim'])}% as bright as the best")
    return out


def pick_burst(burst, feats, level="pose", chosen=None):
    """Return {"shots": {shot.id: {"keep", "reasons", "moment", "role"}}, "confidence", "flags", "top",
    "close_sets", "subject"}. chosen = (shot id, person index) to judge every shot on that person."""
    F = {s.id: feats[s.id] for s in burst}
    subj = subject(burst, F, chosen)
    if subj:
        F, kind = _subject_view(F, subj, chosen)
    else:
        basis, kind = _sharp_basis(F)
        F = {i: {**f, "sharp": f[basis]} for i, f in F.items()}
    max_size = max(F[s.id].get("size", 0) for s in burst)
    who = "The person you chose" if chosen else "Main person"

    def visible(f):
        return not subj or f["size"] >= SMALLER * max_size
    pitches = [x["pitch"] for s in burst for x in F[s.id]["faces"]]
    pitch0 = statistics.median(pitches) if pitches else 0.0
    min_clip = min(F[s.id]["clip_hi"] for s in burst)

    # a missing face only counts against a shot when its own moment mostly shows one: across moments a
    # shot without a person is a different picture (a bird, a sign), not a failed one
    def fails(s, ref, limit, face=False):
        return _gate_failures(F[s.id], ref, min_clip, face, limit)

    def rank_key(s, ref, face=False):
        f = F[s.id]
        # anything that passes the sharpness check competes on expression: a 15% softer face is invisible
        return (not fails(s, ref, KEEP_SHARP, face), visible(f), f["sharp"] >= KEEP_SHARP * ref, _smile_rank(f),
                -round(_facing(f, pitch0) / 10), f["sharp"])

    result, close, flags, close_sets = {}, False, [], []
    keepers = []
    for mi, m in enumerate(group.moments(burst, feats, group.MOMENT_T[level])):
        top_bright = max(F[s.id]["brightness"] for s in m)
        for s in m:
            F[s.id]["dim"] = F[s.id]["brightness"] / top_bright if top_bright else 1
        # an underexposed frame's noise reads as extra sharpness, so it must not set the bar for the others
        ref = max(F[s.id]["sharp"] for s in m if F[s.id]["dim"] >= DARKER)
        fe = sum(1 for s in m if F[s.id]["faces"]) >= len(m) / 2
        ranked = sorted(m, key=lambda s: rank_key(s, ref, fe), reverse=True)
        k = ranked[0]
        keepers.append((mi, k))
        kf = F[k.id]
        reasons = []
        if len(m) == 1:
            reasons.append("Only shot of this moment")
        else:
            nxt = F[ranked[1].id]
            second = max(F[s.id]["sharp"] for s in ranked[1:])
            gain = (kf["sharp"] / second - 1) * 100 if second else 0
            if kf["sharp"] == ref:
                reasons.append(f"Sharpest {kind}" + (f" (+{gain:.0f}% vs next)" if gain >= 3 else ""))
            if visible(kf) and any(not visible(F[s.id]) and not F[s.id]["missing"] for s in m):
                reasons.append(f"{who} larger in the frame")
            if not _closed(kf) and any(_closed(F[s.id]) for s in m):
                reasons.append("Eyes open")
            sharpest = F[max(m, key=lambda s: F[s.id]["sharp"]).id]
            if 0 < kf["sharp"] < ref and _smile_rank(kf) > _smile_rank(sharpest):
                reasons.append(f"Bigger smile (the sharpest shot is only {ref / kf['sharp'] * 100 - 100:.0f}% sharper)")
            elif _smile_rank(kf) > _smile_rank(nxt):
                reasons.append("Bigger smile")
            if _facing(nxt, pitch0) - _facing(kf, pitch0) >= 10:
                reasons.append("Facing the camera")
            if not reasons:
                reasons.append("Best balance of sharpness and expression")
            runner = ranked[1]
            if not fails(runner, ref, KEEP_SHARP, fe):
                if nxt["sharp"] >= CLOSE_SHARP * kf["sharp"] or _smile(nxt) - _smile(kf) >= SMILE_STEP:
                    close = True
                    close_sets.append([s.id for s in ranked if not fails(s, ref, KEEP_SHARP, fe)][:3])
        result[k.id] = {"keep": True, "reasons": reasons, "moment": mi, "role": "pick"}
        for s in ranked[1:]:
            f = F[s.id]
            why = fails(s, ref, KEEP_SHARP, fe)
            if not why:
                pct = round(100 * f["sharp"] / kf["sharp"]) if kf["sharp"] else 100
                if pct <= 95:
                    why.append(f"Slightly softer ({pct}% as sharp as the pick)")
                if _smile_rank(f) < _smile_rank(kf):
                    why.append("Smaller smile than the pick")
                if not visible(f):
                    why.append(f"{who} smaller in the frame")
                if _facing(f, pitch0) - _facing(kf, pitch0) >= 10:
                    why.append("Looking further from the camera")
                if not why:
                    why.append("Nearly identical to the pick")
            if any(x.get("cut_off") for x in f["faces"]) and not any(x.get("cut_off") for x in kf["faces"]):
                why.append("Face cut off by the frame edge")
            result[s.id] = {"keep": False, "reasons": ["Same moment as the pick"] + why, "moment": mi, "role": "same-pose"}

    # Across poses: the overall best always stays; other poses stay unless clearly worse.
    top_bright = max(F[s.id]["brightness"] for s in burst)

    def bdim(s):
        return F[s.id]["brightness"] / top_bright if top_bright else 1
    best_sharp = max(F[s.id]["sharp"] for s in burst if F[s.id]["dim"] >= DARKER and bdim(s) >= OTHER_POSE_DARK)
    _, top = max(keepers, key=lambda mk: (bdim(mk[1]) >= OTHER_POSE_DARK, rank_key(mk[1], best_sharp)))
    top_ok = not fails(top, best_sharp, OTHER_POSE_SHARP)

    def hard(s):
        miss = F[s.id].get("missing")  # the main person being absent is no reason to drop a different moment
        out = [w for w in fails(s, best_sharp, OTHER_POSE_SHARP)
               if not w.startswith(("More blown", "Darker")) and w != miss]
        if bdim(s) < OTHER_POSE_DARK:
            out.insert(0, f"Darker: {int(100 * bdim(s))}% as bright as the best")  # likely the cause of the rest
        return out

    for mi, k in keepers:
        if k.id == top.id:
            continue
        if hard(k) and top_ok:
            # the whole pose group goes; describe each shot against the overall best
            for s, r in result.items():
                if r["moment"] == mi:
                    h = hard(next(x for x in burst if x.id == s))
                    r.update(keep=False, role="other-pose",
                             reasons=["Different moment, but " + h[0].lower()] if h else
                             ["Different moment, but its best shot is " + hard(k)[0].lower()])
        else:
            result[k.id]["reasons"].insert(0, "A different moment worth keeping")
    if not top_ok:
        flags.append("No great shot in this burst - please take a look")
    assert any(r["keep"] for r in result.values()), "every burst must keep at least one photo"
    return {"shots": result, "confidence": "close" if close or not top_ok else "confident", "flags": flags,
            "top": top.id, "close_sets": [c for c in close_sets if result[c[0]]["keep"]], "subject": subj or {}}


def apply_ai_choice(shots, ids, chosen, reason):
    """Record the AI's pick among equally good candidates (`shots` = report shot dicts of one group)."""
    by = {s["id"]: s for s in shots}
    old = by[ids[0]]
    if chosen == ids[0]:
        old["reasons"].append("AI second opinion agrees" + (f": {reason}" if reason else ""))
        return
    new = by[chosen]
    new.update(keep=True, role="pick",
               reasons=["AI second opinion" + (f": {reason}" if reason else ""),
                        "Passed every check (eyes, sharpness, exposure)"])
    old.update(keep=False, role="same-pose",
               reasons=["Same moment as the pick", "Equally sharp; the AI second opinion preferred another shot"])
