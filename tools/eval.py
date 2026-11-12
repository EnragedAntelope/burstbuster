"""Score the picker against hand labels. Read-only: never moves a photo.

    python tools/eval.py [folder] [--level pose]

The folder (default testset/) must contain labels.json: {"bursts": [{"name", "moments": {name: [file, ...]}}]}.
Checks: the tool finds the same bursts, and keeps exactly one photo per labelled moment."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from burstbuster.pipeline import analyze  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default=str(Path(__file__).resolve().parents[1] / "testset"))
    ap.add_argument("--level", default="pose", choices=["identical", "pose", "one"])
    a = ap.parse_args()
    labels = json.loads((Path(a.folder) / "labels.json").read_text(encoding="utf-8"))
    rep = analyze(a.folder, True, a.level, log=lambda m: None)

    found = [{os.path.basename(s["id"]): s for s in g["shots"]} for g in rep["groups"] if g["type"] == "burst"]
    fails = 0
    for b in labels["bursts"]:
        names = {n for m in b["moments"].values() for n in m}
        match = [f for f in found if set(f) == names]
        if not match:
            fails += 1
            print(f"FAIL {b['name']}: no burst with exactly these photos")
            continue
        shots = match[0]
        for moment, files in b["moments"].items():
            kept = [f for f in files if shots[f]["keep"]]
            # pose: one per moment; identical (keep most): at least one; one: at most one
            ok = {"pose": len(kept) == 1, "identical": len(kept) >= 1, "one": len(kept) <= 1}[a.level]
            fails += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {b['name']} / {moment}: kept {kept or 'nothing'}")
        if a.level == "one" and sum(s["keep"] for s in shots.values()) != 1:
            fails += 1
            print(f"FAIL {b['name']}: level 'one' must keep a single photo")
    extra = len(found) - len(labels["bursts"])
    if extra:
        fails += 1
        print(f"FAIL {extra} unexpected extra burst(s)")
    print(f"\n{rep['photos']} photos, {rep['seconds']} s, {'PASS' if not fails else f'{fails} problem(s)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
