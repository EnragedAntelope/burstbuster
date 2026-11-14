"""Picking and grouping logic on synthetic measurements (no models, no real photos)."""
import cv2
import numpy as np

from burstbuster import features, group, pick
from burstbuster.scan import Shot


def _naive_moments(burst, feats, threshold):
    """The original average-linkage loop, kept as the reference for the fast one."""
    e = np.array([feats[s.id]["emb"] for s in burst])
    d = 1.0 - e @ e.T
    clusters = [[i] for i in range(len(burst))]
    while len(clusters) > 1:
        best, pair = np.inf, (0, 0)
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                v = d[np.ix_(clusters[i], clusters[j])].mean()
                if v < best:
                    best, pair = v, (i, j)
        if best > threshold:
            break
        i, j = pair
        clusters[i] += clusters.pop(j)
    return [[burst[i].id for i in sorted(c)] for c in clusters]


def test_fast_moments_match_the_reference():
    for seed in range(5):
        shots = [Shot(primary=f"p{i}.jpg") for i in range(40)]
        feats = {s.id: {"emb": _emb(i % 6 + 10 * seed, jitter=0.15 + 0.05 * (i % 3))} for i, s in enumerate(shots)}
        for t in (0.05, 0.1, 0.3):
            assert [[s.id for s in m] for m in group.moments(shots, feats, t)] == _naive_moments(shots, feats, t)


def test_untimed_photos_join_only_near_identical_ones_in_their_folder():
    shots = [Shot(primary=p) for p in ("a/1.png", "a/2.png", "a/3.png", "b/4.png")]
    feats = {"a/1.png": {"emb": _emb(1)}, "a/2.png": {"emb": _emb(2)}, "a/3.png": {"emb": _emb(1, 0.01)},
             "b/4.png": {"emb": _emb(1)}}
    assert [[s.primary for s in b] for b in group.bursts(shots, feats)] == [["a/1.png", "a/3.png"]]


def test_sharpness_ranks_blur_lower():
    rng = np.random.default_rng(0)
    img = (rng.random((600, 600)) * 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (0, 0), 1.0)
    box = [100, 100, 500, 500]
    s = features.sharpness(img, box)
    assert features.sharpness(cv2.GaussianBlur(img, (0, 0), 3), box) < 0.5 * s
    k = np.zeros((9, 9), np.float32)
    k[4, :] = 1 / 9  # horizontal motion blur
    assert features.sharpness(cv2.filter2D(img, -1, k), box) < 0.5 * s


def _emb(seed, jitter=0.0):
    v = np.random.default_rng(seed).normal(size=64)
    if jitter:
        v = v + np.random.default_rng(seed + 1000).normal(size=64) * jitter
    return list(v / np.linalg.norm(v))


def _face(blink=0.1, smile=0.3):
    return {"box": [10, 10, 50, 50], "blink_l": blink, "blink_r": blink, "smile": smile, "jaw_open": 0,
            "yaw": 0, "pitch": 0, "cut_off": False, "sharp": 0}


def _feat(sharp, emb, faces=None):
    return {"sharp": sharp, "subject_kind": "face", "clip_hi": 0.0, "brightness": 0.3, "faces": faces or [_face()],
            "emb": emb}


def test_mixed_face_and_body_shots_compare_on_body():
    """Faces found in only some shots: face sharpness (~40) must not be compared with body sharpness (~3000)."""
    e = _emb(7)
    with_face = dict(_feat(40, e), body_sharp=700.0, frame_sharp=500.0)
    no_face = dict(_feat(3000, e), subject_kind="person", faces=[], body_sharp=1000.0, frame_sharp=520.0)
    shots, feats = _burst([(0, with_face), (0.5, no_face), (1.0, dict(no_face, body_sharp=850.0))])
    r = pick.pick_burst(shots, feats, "pose")  # face in 1 of 3 shots: no face is expected
    assert r["shots"]["p1.jpg"]["keep"]
    assert "Blurrier: 70% as sharp as the best" in r["shots"]["p0.jpg"]["reasons"]  # body vs body, not face vs body
    assert any(x.startswith("Sharpest subject (+18%") for x in r["shots"]["p1.jpg"]["reasons"])


def test_apple_burst_id_parsed_and_groups():
    from burstbuster.scan import _apple_burst_id
    import struct
    uid = b"ABCD-1234\0"
    entry = struct.pack(">HHII", 0x000B, 2, len(uid), 30)  # header 14 + count 2 + one entry 12 = 28, pad to 30
    mn = b"Apple iOS\0\0\x01MM" + struct.pack(">H", 1) + entry + b"\0\0" + uid
    assert _apple_burst_id(mn) == "ABCD-1234"
    assert _apple_burst_id(b"Canon...") is None and _apple_burst_id(None) is None
    # same burst id joins shots even when far apart in time and look
    a = Shot(primary="a.jpg", files=[("a.jpg", 1, 1)], time=0, camera="c", exif={"burst": "X"})
    b = Shot(primary="b.jpg", files=[("b.jpg", 1, 1)], time=20, camera="c", exif={"burst": "X"})
    assert len(group.bursts([a, b], {"a.jpg": {"emb": _emb(1)}, "b.jpg": {"emb": _emb(2)}})) == 1


def test_resized_copy_is_grouped_and_crop_is_not():
    e = _emb(3)

    def mk(n, dims, t=5.0):
        return Shot(primary=n, files=[(n, 1, 1)], time=t, camera="cam", exif={"dims": dims})
    shots = [mk("orig.jpg", (4032, 3024)), mk("small.jpg", (1600, 1200)), mk("crop.jpg", (3000, 3000)),
             mk("other_time.jpg", (1600, 1200), t=6.0), mk("same_size.jpg", (4032, 3024))]
    feats = {s.id: {"emb": e} for s in shots}
    assert [[s.id for s in g] for g in group.resized_copies(shots, feats)] == [["orig.jpg", "small.jpg"]]
    # a rotated export (portrait pixels) of a landscape original still counts; a different look does not
    shots = [mk("orig.jpg", (4032, 3024)), mk("rot.jpg", (1200, 1600)), mk("diff.jpg", (800, 600))]
    feats = {"orig.jpg": {"emb": e}, "rot.jpg": {"emb": e}, "diff.jpg": {"emb": _emb(4)}}
    assert [[s.id for s in g] for g in group.resized_copies(shots, feats)] == [["orig.jpg", "rot.jpg"]]


def test_exact_copy_keeps_companions_then_plain_name():
    from burstbuster.pipeline import _copy_rank
    plain = Shot(primary=r"C:\a\deep\folder\IMG_1.jpg", files=[("x", 1, 1)])
    copy = Shot(primary=r"C:\a\IMG_1 - Copy.jpg", files=[("x", 1, 1)])
    paren = Shot(primary=r"C:\IMG_1 (1).jpg", files=[("x", 1, 1)])
    live = Shot(primary=r"C:\a\deep\folder\longer\IMG_1 (2).jpg", files=[("x", 1, 1), ("y.mov", 1, 1)])
    assert sorted([copy, paren, plain], key=_copy_rank)[0] is plain
    assert sorted([copy, plain, live], key=_copy_rank)[0] is live


def _burst(specs):
    shots, feats = [], {}
    for i, (t, f) in enumerate(specs):
        s = Shot(primary=f"p{i}.jpg", files=[(f"p{i}.jpg", 1, 1)], time=t, camera="cam")
        shots.append(s)
        feats[s.id] = f
    return shots, feats


def test_burst_grouping_by_time_and_look():
    a = _emb(1)
    shots, feats = _burst([(0, _feat(1, a)), (1.5, _feat(1, a)), (10, _feat(1, a)),   # same look 8.5 s later
                           (13, _feat(1, _emb(2))), (100, _feat(1, a))])             # new scene; far later
    got = [[s.primary for s in b] for b in group.bursts(shots, feats)]
    assert got == [["p0.jpg", "p1.jpg", "p2.jpg"]]


def test_sharpest_open_eyes_wins_and_reasons_are_given():
    e = _emb(3)
    shots, feats = _burst([(0, _feat(100, e)), (0.5, _feat(150, e, [_face(blink=0.9)])), (1, _feat(120, e))])
    r = pick.pick_burst(shots, feats)["shots"]
    assert [s.primary for s in shots if r[s.id]["keep"]] == ["p2.jpg"]  # sharpest one has closed eyes
    assert "Eyes closed" in r["p1.jpg"]["reasons"]
    assert all(r[s.id]["reasons"] for s in shots)


def test_distinct_moment_is_kept_unless_much_worse():
    a, b = _emb(4), _emb(5)
    shots, feats = _burst([(0, _feat(100, a)), (0.5, _feat(90, a)), (1, _feat(95, b)), (1.5, _feat(40, _emb(6)))])
    r = pick.pick_burst(shots, feats, "pose")["shots"]
    assert {s.primary for s in shots if r[s.id]["keep"]} == {"p0.jpg", "p2.jpg"}
    assert r["p3.jpg"]["reasons"][0].startswith("Different moment, but blurrier")
    r1 = pick.pick_burst(shots, feats, "one")["shots"]
    assert [s.primary for s in shots if r1[s.id]["keep"]] == ["p0.jpg"]


def test_moment_without_a_face_is_not_dropped_for_it():
    """A shot of the sign next to a person's burst is its own picture, not a failed portrait."""
    a = _emb(4)
    sign = dict(_feat(100, _emb(5)), faces=[], subject_kind="photo", frame_sharp=500.0)
    shots, feats = _burst([(0, dict(_feat(100, a), frame_sharp=500.0)), (0.5, dict(_feat(95, a), frame_sharp=480.0)),
                           (1, sign)])
    r = pick.pick_burst(shots, feats, "pose")["shots"]
    assert r["p2.jpg"]["keep"] and not any("Face" in x for x in r["p2.jpg"]["reasons"])


def test_underexposed_frame_loses_even_when_its_noise_looks_sharper():
    """Measured on a real night burst: the dark frame scored 2x sharper from noise alone."""
    e = _emb(9)
    night = dict(_feat(0, e), faces=[], subject_kind="photo")
    shots, feats = _burst([(0, dict(night, frame_sharp=1958.0, brightness=0.065)),
                           (0.5, dict(night, frame_sharp=900.0, brightness=0.099)),
                           (1, dict(night, frame_sharp=1074.0, brightness=0.093))])
    r = pick.pick_burst(shots, feats)["shots"]
    assert r["p2.jpg"]["keep"] and not r["p0.jpg"]["keep"]
    assert "Darker: 65% as bright as the best" in r["p0.jpg"]["reasons"]
    assert not any(x.startswith("Blurrier") for x in r["p1.jpg"]["reasons"])  # judged against p2, not the noise
    # so dark that it looks like another moment (it did on the real burst): still set aside
    feats["p0.jpg"]["emb"] = _emb(10)
    r = pick.pick_burst(shots, feats)["shots"]
    assert r["p2.jpg"]["keep"] and not r["p0.jpg"]["keep"]
    assert r["p0.jpg"]["reasons"] == ["Different moment, but darker: 65% as bright as the best"]
    # every frame equally dark (a night sky) is no reason to set any aside
    shots, feats = _burst([(0, dict(night, frame_sharp=900.0, brightness=0.02)),
                           (0.5, dict(night, frame_sharp=880.0, brightness=0.021))])
    assert not any("Darker" in x for v in pick.pick_burst(shots, feats)["shots"].values() for x in v["reasons"])


def test_bigger_smile_beats_slightly_sharper():
    """83% as sharp is invisible on a face; the nicer smile should win."""
    e = _emb(8)
    shots, feats = _burst([(0, _feat(206, e, [_face(smile=0.18)])), (0.5, _feat(171, e, [_face(smile=0.26)]))])
    r = pick.pick_burst(shots, feats)["shots"]
    assert r["p1.jpg"]["keep"] and not r["p0.jpg"]["keep"]
    assert r["p1.jpg"]["reasons"][0].startswith("Bigger smile (the sharpest shot is only 20% sharper)")
    assert "Smaller smile than the pick" in r["p0.jpg"]["reasons"]


def _person(emb, box, sharp, face=None):
    return {"box": box, "emb": emb, "sharp": sharp, "face": face}


def _crowd(specs):
    """specs: per shot, list of (person emb seed, box, sharp). Whole-frame look is the same (one moment)."""
    e = _emb(20)
    return _burst([(t * 0.5, dict(_feat(1, e, []), w=1000, h=1000, subject_kind="person", body_sharp=1.0,
                                  persons=[_person(_emb(seed, 0.3), box, sh) for seed, box, sh in ps]))
                   for t, ps in enumerate(specs)])


def test_burst_follows_the_person_seen_most_and_judges_them():
    big, her = [0, 0, 600, 900], [400, 300, 600, 700]
    shots, feats = _crowd([[(1, big, 900), (2, her, 100)],       # passer-by sharp and big; she is soft
                           [(2, her, 300)],                       # just her, sharp
                           [(3, big, 900), (2, her, 280)]])
    assert pick.tracks(shots, feats)[1] == {"p0.jpg": 1, "p1.jpg": 0, "p2.jpg": 1}  # same seed matched across frames
    r = pick.pick_burst(shots, feats)
    assert r["subject"] == {"p0.jpg": 1, "p1.jpg": 0, "p2.jpg": 1}
    assert [s.id for s in shots if r["shots"][s.id]["keep"]] == ["p1.jpg"]
    # the user clicks the passer-by in p0 instead: only that photo has them
    r = pick.pick_burst(shots, feats, chosen=("p0.jpg", 0))
    assert r["shots"]["p0.jpg"]["keep"]
    assert "The person you chose isn't in this photo" in r["shots"]["p1.jpg"]["reasons"]


def test_a_burst_always_keeps_one_even_if_all_bad():
    e = _emb(7)
    shots, feats = _burst([(0, _feat(10, e, [_face(0.9)])), (0.5, _feat(12, e, [_face(0.95)]))])
    out = pick.pick_burst(shots, feats)
    assert sum(v["keep"] for v in out["shots"].values()) == 1
    assert out["flags"] and out["confidence"] == "close"
