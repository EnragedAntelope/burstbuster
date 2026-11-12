# BurstBuster - agent notes

Local desktop tool (Windows, macOS on Apple Silicon, Linux): detects photo bursts and near-duplicates, keeps the best shot per moment
with plain-English reasons, moves the rest to a holding folder, and sends them to the Recycle Bin on one
approval. User-facing usage lives in README.md; this file is for working on the code.

## Current state

_Last verified: 2026-11-12_

- **Status:** main is v1.0.2 (`burstbuster.__version__`), public on GitHub (one release per version); CI runs
  pytest on Windows, Ubuntu and macOS x Python 3.11-3.14 (`.github/workflows/tests.yml`).
- **Works:** read-only CLI scan; grouping and picking (eval passed on a private local test set at all three
  levels before release); journaled move / restore / recycle with tests; pywebview GUI (start, progress, review with
  compare and keyboard, done); optional local-only VLM second opinion (tested with LM Studio); following one
  person through a multi-person burst (automatic, plus a "Focus on a person" click override in review);
  smaller saves of one capture (exports, shared copies) set aside before burst picking, full size kept;
  exact copies keep the one with companion files, then more EXIF/XMP, then a non-"copy" name; same picture with
  edited metadata (JPEG/PNG `scan.pixel_hash`) grouped as copies; DupeDestroyer mode (`mode="dupes"`, start-screen
  toggle, `scan --dupes`): copies only, no models, resized copies matched by `scan.look`; underexposed frames set aside
  ("Darker", relative to the brightest shot, checked on real dark bursts); macOS/Linux support (Trash via
  send2trash, Qt webview on Linux, `start.sh`). Linux checked in WSL Ubuntu: CLI scan with models, window
opens with a working bridge, set-aside then Trash on dummy files. macOS: CI tests only.
- **In progress:** nothing.
- **To do (next pick-ups):**
  - Run the app for real on macOS, and click through a full run on a Linux desktop (WSL checks only so far).
  - Verify the HEIC fix on a real iPhone HEIC (only a generated one was tested).
  - Match copies whose EXIF was stripped (messaging apps) to their original by look alone.
  - DupeDestroyer: merge metadata across copies (XMP sidecar on the keeper, oldest date), HEIC pixel matching,
    re-take `docs/start.png` (start screen now has the mode toggle), try it on a real library.
  - Live-window click-through, real drag-and-drop, and a log file for `pythonw` startup crashes (see gaps).
- **Known gaps:** the review screen has been used in the live window; a full live run through to the Recycle
  Bin is (unknown - confirm before relying on this); the Recycle Bin call itself is verified on a dummy file.
  Drag-and-drop is wired (pywebview DOM drop event) but untested with a real drag.
  Apple BurstUUID grouping is in, but the private test photos have none (taps, not held-shutter bursts).
  Moment threshold retuned to 0.10 from user feedback (near-identical shots were kept as separate
  moments); `testset/labels.json` follows that coarser meaning. The private `testset/` is not present in
  every checkout, so `tools/eval.py` only runs where it exists. Not built: aesthetic score (judged not to
  help near-identical shots), GPU inference, video analysis. A startup crash under `pythonw` is invisible
  (no log file). Person following is tuned on one
  outdoor burst only (`pick.SAME_PERSON`); `Api.focus` (re-pick plus file moves) is checked through the mocked
  bridge, not yet in the live window. "Nicer pose" is not measured at all.
  The darker-frame limits (`pick.DARKER`, `OTHER_POSE_DARK`) were tuned on four private bursts with little
  margin (a dark frame at 78%, a normal one at 81%). Brightness is a gate, not a rank: of two passing frames
  the sharper wins even if the other is better exposed. Intel Macs are unsupported (no mediapipe wheel).
  Copies that lost their EXIF (messaging apps) are not matched to the original.
- **Deep docs:** none beyond this file and README.md.

## Private data - never commit

This repo is public. `testset/` (private family photos) and `burst-picking-and-catalog.md` are git-ignored
and must never leave the machine they are on. `.gitignore` also blocks every image/video extension;
`tests/test_safety.py` fails if any of them is ever tracked. Do not send these photos to any web service.

The only tracked images are the README screenshots in `docs/*.png` (named in `test_safety.py`). They show
AI-generated demo photos only: never screenshot a real photo, and never let a real folder path, journal
or settings file show in one (run the app with `LOCALAPPDATA` pointed at an empty folder).
Speed figures in README.md come from a timed `scan` of phone-sized demo JPEGs; re-measure if the pipeline
changes.

## Build, test, run

```
START.bat                                   # first run: venv + install + models; then opens the app
./start.sh                                  # same on macOS/Linux (use .venv/bin/python below there)
.venv\Scripts\python -m burstbuster         # open the app
.venv\Scripts\python -m burstbuster scan <folder> --report out.json   # read-only
.venv\Scripts\python -m burstbuster scan <folder> --dupes             # copies only, no models
.venv\Scripts\python -m pytest tests -q     # synthetic tests only
.venv\Scripts\python tools\eval.py [--level pose|one|identical]       # scores testset/ against labels.json
```

Python 3.11+ venv in `.venv` (onnxruntime, Pillow and rawpy set the floor). Models download to `models/` (git-ignored) from pinned URLs with SHA256 checks
(`burstbuster/models.py`).

## Layout

- `scan.py` - walk (no junctions, skips holding folder and cloud-only files), companions -> one Shot, EXIF
  and stored pixel size (HEIC needs `pillow_heif.register_heif_opener()` at import, or it gets no EXIF).
- `features.py` - per-photo measurements in worker processes: pose -> head crop -> face landmarks
  (blink, smile, yaw/pitch), subject sharpness, clipping, DINOv2 embedding; `persons` (object-detector +
  pose boxes, each with its own crop embedding, sharpness and unfiltered face).
- `group.py` - resized copies (same camera + capture time + shape, fewer pixels), bursts (time gap plus
  embedding similarity) and moments (average-linkage on embedding).
- `pick.py` - filter (hard gates) then rank, with reasons; marks close calls for the VLM. With several
  people, `tracks()` follows each person frame to frame (person-crop embedding) and `subject()` picks whose
  face/body every shot is judged on. Any shot passing the sharpness gate competes on smile.
- `pipeline.py` - orchestrates (copies first: exact, same pixels, resized; then bursts unless `mode="dupes"`;
  `_KEEP_RULES` is the ordered keep-a-copy rule list, `_add_copies` merges chained copy groups), caches features in `APP_DIR\cache.sqlite` (`%LOCALAPPDATA%\BurstBuster` on
  Windows, `~/Library/Application Support/BurstBuster` on macOS, `$XDG_DATA_HOME/BurstBuster` on Linux).
- `fileops.py` - the only code that moves files: journal, set_aside, restore, recycle, reconcile.
- `vlm.py` - OpenAI-compatible client; local/private hosts only, no redirects.
- `app.py` + `ui/` - pywebview window, `js_api` bridge (no HTTP port); page is inlined at startup.

## Conventions and gotchas

- Face detection on the whole image misses full-body shots; always go pose -> head crop -> face landmarker.
- Sharpness is Laplacian variance on the subject crop resized to a fixed size, divided by brightness;
  only meaningful relative within a burst.
- Moving: same-volume `fileops._move` only (rename on Windows; link + unlink on POSIX, where rename would
  silently overwrite), journal row committed before each move, never overwrite, all companions or none.
  Recycling uses `IFileOperation` with `FOF_WANTNUKEWARNING` on Windows (send2trash silences that warning)
  and send2trash elsewhere. Never add permanent deletion.
- Worker processes always use the `spawn` start method (Linux defaults to fork, which is unsafe once threaded libraries are loaded, and would differ from Windows).
- The page CSP needs `'unsafe-eval'`: pywebview's `evaluate_js` and its DOM helper wrap every call in `eval`,
  which Qt (Linux) enforces. Linux also needs system `libgles2` for MediaPipe and the usual QtWebEngine
  libraries (libnss3, libxkbcommon-x11-0, libxcb-*); a desktop install normally has the latter.
- pywebview exposes public attributes of the `js_api` object to JS; keep `Api` state in `_underscore` names.
- Install `opencv-contrib-python` (comes with mediapipe) only; adding `opencv-python-headless` breaks cv2.
- Within a burst, sharpness is compared on one basis for every shot (face, else body, else frame;
  `pick._sharp_basis`): face and body scores differ ~100x, and dark shots often lose the face.
- `APP_DIR` moved from `%LOCALAPPDATA%\BurstPhotoBuster` (old name); `__init__.py` migrates it once.
- Bump `FEATURE_VERSION` in `pipeline.py` whenever `features.measure()` output changes.
- Plain keyboard characters only in code, docs and commits.
