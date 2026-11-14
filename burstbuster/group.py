"""Burst grouping (time + look) and splitting a burst into distinct poses ("moments").

Similarity is DINOv2 cosine on the whole frame. Thresholds were set on the private test set:
same scene 8 s apart = 0.91, indoor vs outdoor = 0.66; different poses in one burst differ
by about 0.05-0.1 in distance, near-identical frames by 0.01-0.03.
"""
import os

import numpy as np

BURST_GAP = 2.0       # seconds: always the same burst
SCENE_GAP = 30.0      # seconds: same burst only if the photos also look alike
SCENE_SIM = 0.85      # cosine needed to join across a longer gap
NOTIME_SIM = 0.97     # photos with no capture time join only if nearly identical

# Moment split distance per strictness level (higher = fewer, bigger moments)
# 0.10 tuned on real bursts: a hand-held camera shifting between frames reaches ~0.09 for the same pose
MOMENT_T = {"identical": 0.05, "pose": 0.10, "one": float("inf")}


def cos(a, b):
    return float(np.dot(a, b))  # embeddings are already L2-normalised


def bursts(shots, feats):
    """Return list of bursts, each a list of shots (>= 2) in time order.
    `feats` maps shot.id -> measure() dict."""
    out = []
    timed = sorted([s for s in shots if s.time is not None], key=lambda s: (s.camera, s.time))
    run = []
    for s in timed:
        if run:
            p = run[-1]
            gap = s.time - p.time
            same = s.camera == p.camera and (
                gap <= BURST_GAP or (gap <= SCENE_GAP and cos(feats[s.id]["emb"], feats[p.id]["emb"]) >= SCENE_SIM)
                or (s.exif.get("burst") is not None and s.exif.get("burst") == p.exif.get("burst")))
            if not same:
                out.append(run)
                run = []
        run.append(s)
    if run:
        out.append(run)
    # shortcut: photos with no capture time are only matched within their own folder, O(n^2) per folder in
    # numpy (seconds for 10k photos); use an index (e.g. faiss) if folders reach hundreds of thousands
    untimed = {}
    for s in shots:
        if s.time is None:
            untimed.setdefault(os.path.dirname(s.primary), []).append(s)
    for group in untimed.values():
        e = np.array([feats[s.id]["emb"] for s in group])
        used = np.zeros(len(group), bool)
        for i in range(len(group)):
            if used[i]:
                continue
            js = np.nonzero((e[i + 1:] @ e[i] >= NOTIME_SIM) & ~used[i + 1:])[0] + i + 1
            used[js] = True
            out.append([group[i]] + [group[j] for j in js])
    return [b for b in out if len(b) > 1]


def resized_copies(shots, feats):
    """Groups of one capture saved at different sizes (exports, shared copies): same camera and capture time,
    same shape, smaller, and nearly identical look. Largest first. A crop changes the shape, so it stays out."""
    by_capture = {}
    for s in shots:
        if s.time is not None and s.exif.get("dims"):
            by_capture.setdefault((s.camera, s.time), []).append(s)
    out = []
    for g in by_capture.values():
        g = sorted(g, key=lambda s: -s.exif["dims"][0] * s.exif["dims"][1])
        big, (bw, bh) = g[0], g[0].exif["dims"]
        copies = [s for s in g[1:] if s.exif["dims"][0] * s.exif["dims"][1] < bw * bh
                  and abs(min(s.exif["dims"]) / max(s.exif["dims"]) - min(bw, bh) / max(bw, bh)) < 0.01
                  and cos(feats[s.id]["emb"], feats[big.id]["emb"]) >= NOTIME_SIM]
        if copies:
            out.append([big] + copies)
    return out


def moments(burst, feats, threshold):
    """Split a burst into groups of near-identical frames (average-linkage clustering)."""
    if threshold == float("inf"):
        return [list(burst)]
    n = len(burst)
    e = np.array([feats[s.id]["emb"] for s in burst])
    d = 1.0 - e @ e.T  # cluster-to-cluster average distance, indexed by each cluster's first frame
    np.fill_diagonal(d, np.inf)
    size = np.ones(n)
    clusters = {i: [i] for i in range(n)}
    while len(clusters) > 1:
        i, j = divmod(int(np.argmin(d)), n)  # first closest pair, i < j (d is symmetric)
        if d[i, j] > threshold:
            break
        d[i] = (size[i] * d[i] + size[j] * d[j]) / (size[i] + size[j])  # average linkage, updated in place
        d[:, i] = d[i]
        d[j] = d[:, j] = d[i, i] = np.inf
        size[i] += size[j]
        clusters[i] += clusters.pop(j)
    return [[burst[i] for i in sorted(c)] for c in clusters.values()]
