"""Desktop app: an HTML page in a native window (pywebview / Edge WebView2). The page talks to
this Python object directly; no web server or network port is opened."""
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview
from PIL import Image, ImageOps

from . import APP_DIR, fileops, merge, vlm
from .scan import IMAGE_EXT

UI = Path(__file__).resolve().parent / "ui"
SETTINGS = APP_DIR / "settings.json"
RUNS = APP_DIR / "runs"


def _load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


def _data_url(im, quality=85):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _render(path, long_side):
    """Downscaled, correctly rotated preview. Opens the file read-only."""
    if Path(path).suffix.lower() in IMAGE_EXT - {".heic", ".heif"}:
        im = Image.open(path)
        if im.format == "JPEG":
            im.draft("RGB", (long_side, long_side))
        im = ImageOps.exif_transpose(im)
    else:
        from .features import load_image
        im = Image.fromarray(load_image(path))
    im.thumbnail((long_side, long_side), Image.Resampling.LANCZOS)
    return im


class Api:
    def __init__(self):
        self._window = None
        self._report = None
        self._run = None
        self._prog = {"stage": "", "done": 0, "total": 0, "running": False, "error": None, "started": 0}
        self._cancel = False
        self._thumbs = {}
        self._lock = threading.Lock()

    # ---------- start screen ----------
    def init(self):
        problems = fileops.reconcile()
        return {"settings": _load_json(SETTINGS, {}), "open_runs": fileops.open_runs(), "problems": problems}

    def choose_folder(self):
        r = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return r[0] if r else None

    def folder_info(self, path):
        if not path or not os.path.isdir(path):
            return {"ok": False, "why": "That folder doesn't exist."}
        ok, why = fileops.recycle_supported(path)
        return {"ok": True, "recycle": ok, "recycle_why": why}

    def ai_status(self, url=None):
        """Find a local AI server (or check the given one) and list its models, vision models first."""
        try:
            if url:
                if not vlm.is_local(url):
                    return {"ok": False, "why": "Only servers on this computer or your home network are allowed."}
                info = {"name": "Custom server", "url": url, "models": vlm.list_models(url)}
            else:
                info = vlm.detect()
                if not info:
                    return {"ok": False, "why": "No local AI server found."}
        except Exception as e:
            return {"ok": False, "why": f"Couldn't reach that server ({type(e).__name__})."}
        vision = set()
        try:  # LM Studio tells us which models can see images
            base = info["url"].rsplit("/v1", 1)[0]
            with vlm._open(base + "/api/v0/models", timeout=1.5) as r:
                vision = {m["id"] for m in json.load(r)["data"] if m.get("type") == "vlm"}
        except Exception:
            pass
        models = sorted(info["models"], key=lambda m: (m not in vision, m))
        info.update(ok=True, models=[{"id": m, "vision": (m in vision) if vision else None} for m in models
                                     if not m.startswith("text-embedding")])
        return info

    # ---------- scanning ----------
    def start_scan(self, opts):
        if self._prog["running"]:
            return False
        _save_json(SETTINGS, opts)
        self._cancel = False
        self._prog = {"stage": "Starting", "done": 0, "total": 0, "running": True, "error": None,
                     "started": time.time()}
        threading.Thread(target=self._scan, args=(opts,), daemon=True).start()
        return True

    def _scan(self, opts):
        from .pipeline import analyze
        try:
            def progress(stage, done, total):
                self._prog.update(stage=stage, done=done, total=total)
            ai = {"url": opts["ai_url"], "model": opts["ai_model"]} if opts.get("ai") and opts.get("ai_model") else None
            rep = analyze(opts["folder"], opts.get("recursive", True), opts.get("level", "pose"),
                          progress=progress, cancelled=lambda: self._cancel, log=lambda m: progress(m, 0, 0), ai=ai,
                          mode=opts.get("mode", "bursts"))
            if rep is None:
                self._prog.update(running=False, stage="Cancelled - nothing was moved.")
                return
            self._prog.update(stage="Setting aside the weaker shots", done=0, total=0)
            run = fileops.new_run_id()
            aside = [s for g in rep["groups"] for s in g["shots"] if not s["keep"]]
            res = fileops.set_aside(rep["root"], run, [{"id": s["id"], "stat": s["stat"]} for s in aside])
            blocked = dict(res["skipped"])
            for g in rep["groups"]:
                for s in g["shots"]:
                    s["held"] = s["id"] in res["moved"]
                    if s["id"] in blocked:
                        s.update(keep=True, blocked=blocked[s["id"]])
            rep["run"] = run
            rep["merge"] = bool(opts.get("merge"))
            rep["recycle"], rep["recycle_why"] = fileops.recycle_supported(rep["root"])
            rep["holding"] = fileops.holding_dir(rep["root"], run)
            with self._lock:
                self._report, self._run = rep, run
                self._persist()
            self._prog.update(running=False, stage="done")
        except Exception as e:
            self._prog.update(running=False, error=f"{type(e).__name__}: {e}")

    def progress(self):
        return self._prog

    def cancel(self):
        self._cancel = True

    def _persist(self):
        _save_json(RUNS / f"{self._run}.json", self._report)

    def get_report(self):
        return self._report

    def resume(self, run):
        rep = _load_json(RUNS / f"{run}.json", None)
        if not rep:
            return False
        held = set(fileops.run_shots(run))
        for g in rep["groups"]:
            for s in g["shots"]:
                s["held"] = s["id"] in held
        rep["recycle"], rep["recycle_why"] = fileops.recycle_supported(rep["root"])
        self._report, self._run = rep, run
        return True

    # ---------- review ----------
    def _shot(self, sid):
        for g in self._report["groups"]:
            for s in g["shots"]:
                if s["id"] == sid:
                    return g, s
        raise KeyError(sid)

    def image(self, sid, size=360):
        key = (sid, size)
        if key in self._thumbs:
            return self._thumbs[key]
        try:
            url = _data_url(_render(fileops.path_for(self._run, sid), size))
        except OSError:  # recycled, or moved outside the app
            return None
        if size <= 360:  # grid thumbnails only: large compare previews would fill memory over a long review
            self._thumbs[key] = url
        return url

    def face(self, sid):
        """Face close-up at full working resolution (what sharpness was measured on)."""
        _, s = self._shot(sid)
        if not s.get("face_box"):
            return None
        from .features import load_image
        try:
            img = load_image(fileops.path_for(self._run, sid))
        except OSError:
            return None
        x0, y0, x1, y1 = s["face_box"]
        p = (x1 - x0) * 0.35
        crop = img[int(max(y0 - p, 0)):int(y1 + p), int(max(x0 - p, 0)):int(x1 + p)]
        return _data_url(Image.fromarray(crop), 92)

    def set_keep(self, sid, keep, force=False):
        """Keep a photo (moves it back now) or set it aside (moves it to the holding folder now)."""
        with self._lock:
            g, s = self._shot(sid)
            if s.get("recycled"):
                return {"ok": False, "why": "Already in the Recycle Bin - restore it from there."}
            if not keep and not force and sum(x["keep"] for x in g["shots"]) <= 1 and s["keep"]:
                return {"ok": False, "confirm": "That would set aside every photo in this set. Are you sure?"}
            if keep and s["held"]:
                r = fileops.restore(self._run, [sid])
                if sid not in r["restored"]:
                    return {"ok": False, "why": r["skipped"][0][1] if r["skipped"] else "Couldn't move it back."}
                s["held"] = False
            elif not keep and not s["held"]:
                r = fileops.set_aside(self._report["root"], self._run, [{"id": sid, "stat": s["stat"]}])
                if sid not in r["moved"]:
                    return {"ok": False, "why": r["skipped"][0][1] if r["skipped"] else "Couldn't move it."}
                s["held"] = True
            s["keep"] = keep
            s["user"] = True
            self._persist()
            return {"ok": True}

    def focus(self, gid, sid=None, idx=None):
        """Re-pick a burst around one person (person `idx` in photo `sid`), or automatically when sid is None,
        then move photos to match. Your earlier keep / set-aside choices in this burst are replaced."""
        from types import SimpleNamespace
        from .pipeline import burst_group, cached_feats
        with self._lock:
            g = next(g for g in self._report["groups"] if g.get("id") == gid)
            live = [s for s in g["shots"] if not s.get("recycled")]
            if len(live) < 2:
                return {"ok": False, "why": "Too few photos left in this burst."}
            feats = cached_feats([s["id"] for s in live])
            if len(feats) < len(live) or not all("persons" in f for f in feats.values()):
                return {"ok": False, "why": "This burst was measured by an older version. Scan the folder again."}
            _, new = burst_group([SimpleNamespace(id=s["id"]) for s in live], feats, self._report["level"],
                                 (sid, idx) if sid else None)
            by = {s["id"]: s for s in new["shots"]}
            for s in live:
                s.update({k: by[s["id"]][k] for k in ("keep", "reasons", "moment", "role", "subject")})
                s.pop("user", None)
                s.pop("blocked", None)
            g.update({k: new[k] for k in ("confidence", "flags", "top", "focus")})
            back = [s["id"] for s in live if s["keep"] and s["held"]]
            aside = [s for s in live if not s["keep"] and not s["held"]]
            skipped = []
            if back:
                r = fileops.restore(self._run, back)
                skipped += r["skipped"]
                for s in live:
                    if s["id"] in r["restored"]:
                        s["held"] = False
            if aside:
                r = fileops.set_aside(self._report["root"], self._run, [{"id": s["id"], "stat": s["stat"]} for s in aside])
                skipped += r["skipped"]
                for s in aside:
                    s["held"] = s["id"] in r["moved"]
            for s in live:  # what is on disk wins over the plan
                s["keep"] = not s["held"]
            self._persist()
            return {"ok": True, "skipped": len(skipped)}

    def _carry_over(self):
        """Before recycling copies: carry their photo details over to the kept copy (merge.apply). A set whose
        details could not be written gets its copies moved back, so no detail is lost."""
        failed = []
        for g in self._report["groups"]:
            held = [s for s in g["shots"] if s.get("held")]
            kept = [s for s in g["shots"] if s["keep"] and not s.get("held") and not s.get("recycled")]
            if g["type"] != "duplicate" or not held or not kept:
                continue
            k = next((s for s in kept if s["id"] == g["top"]), kept[0])
            copies = [(fileops.path_for(self._run, s["id"]), [fileops.path_for(self._run, p) for p in s["files"]],
                       min(mt for _, _, mt in s["stat"])) for s in held]
            try:
                r = merge.apply(k["id"], k["files"], copies)
            except OSError as e:
                failed += [s["id"] for s in held]
                g["flags"].append(f"Photo details not carried over ({e.strerror or type(e).__name__}); copies kept")
                continue
            if r["mtime"]:
                for row in k["stat"]:
                    if row[0] == k["id"]:
                        row[2] = r["mtime"]
            if r["sidecar"]:  # a companion from now on: it moves with the photo
                st = os.stat(r["sidecar"])
                k["files"].append(r["sidecar"])
                k["stat"].append([r["sidecar"], st.st_size, st.st_mtime_ns])
                k["reasons"].append("Photo details from its copies saved in " + os.path.basename(r["sidecar"]))
            k["reasons"] += merge.describe({}, r["conflicts"], r["differs"])
        if failed:
            res = fileops.restore(self._run, failed)
            for g in self._report["groups"]:
                for s in g["shots"]:
                    if s["id"] in res["restored"]:
                        s.update(held=False, keep=True)
        return len(failed)

    def accept_all(self):
        with self._lock:
            kept_back = self._carry_over() if self._report.get("merge") else 0
            r = fileops.recycle(self._run)
            r["kept_back"] = kept_back
            held = set(fileops.run_shots(self._run))
            for g in self._report["groups"]:
                for s in g["shots"]:
                    if s.get("held") and s["id"] not in held:
                        s.update(held=False, recycled=True)
            self._persist()
            return r

    def restore_all(self):
        with self._lock:
            r = fileops.restore(self._run)
            held = set(fileops.run_shots(self._run))
            for g in self._report["groups"]:
                for s in g["shots"]:
                    if s.get("held") and s["id"] not in held:
                        s["held"] = False
            self._persist()
            return {"restored": len(r["restored"]), "skipped": r["skipped"]}

    def restore_run(self, run):
        r = fileops.restore(run)
        return {"restored": len(r["restored"]), "skipped": r["skipped"]}

    def open_holding(self):
        if self._report and os.path.isdir(self._report["holding"]):
            _reveal(self._report["holding"], select=False)

    def show_in_folder(self, sid):
        _reveal(fileops.path_for(self._run, sid), select=True)

    def clear_cache(self):
        from .pipeline import clear_cache
        clear_cache()
        return True


def _reveal(path, select):
    """Open a folder, or the folder holding a file with that file selected where the platform can."""
    path = os.path.normpath(path)
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", path]) if select else os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if select else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if select else path])


def _page():
    html = (UI / "index.html").read_text(encoding="utf-8")
    html = html.replace("/*STYLE*/", (UI / "style.css").read_text(encoding="utf-8"))
    return html.replace("/*SCRIPT*/", (UI / "app.js").read_text(encoding="utf-8"))


def _on_drop(window, e):
    """A folder (or a photo inside it) dropped on the window: use that folder."""
    files = (e.get("dataTransfer") or {}).get("files") or []
    path = files[0].get("pywebviewFullPath") if files else None
    if path and not os.path.isdir(path):
        path = os.path.dirname(path)
    if path:
        window.evaluate_js(f"droppedFolder({json.dumps(path)})")


def _bind_drop(window):
    from webview.dom import DOMEventHandler
    window.dom.document.events.dragover += DOMEventHandler(lambda e: None, prevent_default=True)
    window.dom.document.events.drop += DOMEventHandler(lambda e: _on_drop(window, e), prevent_default=True)


def run():
    api = Api()
    w = api._window = webview.create_window("BurstBuster", html=_page(), js_api=api,
                                            width=1280, height=880, min_size=(900, 640), text_select=True)
    w.events.loaded += lambda: _bind_drop(w)
    webview.start(private_mode=True)


