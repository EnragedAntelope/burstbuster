"""CLI. `python -m burstbuster scan <folder>` is read-only; `python -m burstbuster` opens the app."""
import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser(prog="burstbuster")
    sub = ap.add_subparsers(dest="cmd")
    sc = sub.add_parser("scan", help="analyse a folder and print/save the report (moves nothing)")
    sc.add_argument("folder")
    sc.add_argument("--no-subfolders", action="store_true")
    sc.add_argument("--level", choices=["identical", "pose", "one"], default="pose")
    sc.add_argument("--report", help="write the JSON report here")
    a = ap.parse_args()
    if a.cmd == "scan":
        from .pipeline import analyze
        rep = analyze(a.folder, not a.no_subfolders, a.level,
                      progress=lambda st, d, t: print(f"\r{st} {d}/{t}   ", end="", file=sys.stderr))
        print(file=sys.stderr)
        if a.report:
            with open(a.report, "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=1)
        for g in rep["groups"]:
            print(f"\n[{g['type']} {g['id']}] {g['confidence']} {' '.join(g['flags'])}")
            for s in sorted(g["shots"], key=lambda s: s["id"]):
                print(f"  {'KEEP ' if s['keep'] else 'aside'} m{s['moment']} {s['id']}  sharp={s['metrics']['sharp']}"
                      f"  smile={s['metrics']['smile']}  | {'; '.join(s['reasons'])}")
        st = rep["stats"]
        print(f"\n{rep['photos']} photos, {st['groups']} groups, {st['set_aside']} set aside "
              f"({st['bytes'] / 1e6:.0f} MB), {len(rep['errors'])} unreadable, {rep['seconds']} s")
    else:
        from .app import run
        run()


if __name__ == "__main__":
    main()
