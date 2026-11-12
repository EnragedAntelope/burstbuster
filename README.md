# BurstBuster

**Fired off 20 shots to get one good one? BurstBuster keeps the best shot of every moment and sets the rest
aside, with a plain-English reason for every choice.** Free, private, and fast on an ordinary Windows PC.

[![Tests](https://github.com/EnragedAntelope/burstbuster/actions/workflows/tests.yml/badge.svg)](https://github.com/EnragedAntelope/burstbuster/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Windows](https://img.shields.io/badge/platform-Windows-0078D6)
![Python 3.13](https://img.shields.io/badge/python-3.13-3776AB)

![BurstBuster review screen: the kept shots, and the set-aside shots with their reasons](docs/review.png)

- **Picks like you would.** Eyes open first, then the subject in focus, then the bigger smile and facing the camera.
  It's not just for people: birds, kayaks, pets and night skies are judged on what's sharpest.
- **Explains every call.** "Eyes closed", "Blurrier: 28% as sharp as the best", "Smaller copy of IMG_0120.JPG".
  Close calls go to the top so you can check them first.
- **Never loses a photo.** Nothing is deleted until you approve it, and then it goes to the Recycle Bin. One click
  restores everything.
- **Fast and light.** About 120 phone photos in 25 seconds on a desktop CPU, no graphics card needed, and a
  re-scan of the same folder in under a second ([numbers below](#speed-and-system-use)).
- **100% local.** No account, no upload, no telemetry. Your photos never leave your PC.

## Get started

1. Install [Python 3.13](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
2. Download the [latest release](https://github.com/EnragedAntelope/burstbuster/releases/latest) (Source code
   zip) and unzip it, or `git clone https://github.com/EnragedAntelope/burstbuster`.
3. Double-click `START.bat`. The first run installs what it needs (a few minutes, internet needed once). After
   that it opens straight away.

## How to use it

![Start screen: choose a folder and how much to keep](docs/start.png)

1. Choose a folder (or drag one onto the window), decide whether to include subfolders, and pick how much to keep:
   - **Keep most**: only sets aside photos that are basically the same shot and clearly worse.
   - **Best per moment** (recommended): keeps the best photo of each distinct pose or moment.
   - **One per burst**: keeps just the single best photo.
2. Press **Find bursts**. The weaker shots move into a `_BurstBuster_Holding` folder inside the folder you chose.
3. Review. Click a photo (or press Space) to compare it side by side with the pick, including a face close-up.
   Press **Keep** on anything you want back; it moves back straight away.
   If a burst picked the wrong person, press **Focus on a person** and click the one you care about; the burst
   is picked again around them (this replaces your earlier keep / set-aside choices in that burst).
4. When you're happy, press **Looks good: send set-aside photos to Recycle Bin**. Or press **Restore all** to
   undo everything.

Keys: arrows move, K keep, X set aside, Space compare, Esc close, ? help.

![Compare view: the pick next to a blurrier shot, with face close-ups](docs/compare.png)

## How it chooses

Photos taken seconds apart that look alike form a burst. Within a burst, photos with the same pose form a moment.
For each moment it first drops shots with a clear problem (eyes closed, subject blurrier than the best, more
blown-out highlights), then ranks the rest by a bigger smile, facing the camera, and sharpness. Every burst
always keeps at least one photo, and a different moment is kept unless it is clearly worse.

A photo with no face in it is never marked down for that when it shows a different moment: a bird, a sign or a
sunset is its own picture, judged on sharpness and exposure.

![A heron burst: no faces anywhere, the sharpest frame is kept](docs/no-face-burst.png)

When several people share the frame, each photo is judged on the person who appears in most of the burst
(outlined in the review), and photos where that person is missing or much smaller rank lower.

It also tidies up copies:

- Live Photo videos, RAW+JPEG pairs and sidecar files always move together with their photo.
- Byte-identical copies anywhere in the folder are found. The copy kept is the one with its companion files, then
  the one whose name does not end in "copy" or "(1)", then the shortest path.
- Smaller saves of the same photo (same camera and capture time, same shape, for example an export or a shared
  copy) are found too, and the full-size one is kept.

## Speed and system use

- **No graphics card needed.** Everything runs on the CPU with small models (about 100 MB in total, downloaded
  once and checked against fixed hashes).
- **Leaves your PC usable.** It uses at most half your CPU cores (up to 8), at roughly 400 MB of memory each.
- **Measured** on an Intel i7-13700KF (8 worker processes): 120 phone-sized photos (12 MP JPEG) in 25 seconds,
  peak memory 3.4 GB in total. A 4-core laptop runs 2 workers, so expect roughly 1 GB and a slower scan.
- **Re-scans are instant.** Measurements are cached, so scanning the same folder again took 0.1 seconds.

## Safety

- Scanning only reads your photos. Moves are journaled, so an interrupted run is settled the next time the app
  starts.
- Nothing is ever overwritten, and a photo changed since the scan is left where it is.
- Nothing is deleted permanently. On network or removable drives (no Recycle Bin), set-aside photos stay in the
  holding folder for you to delete yourself.
- Cloud-only OneDrive files are skipped, so a scan never triggers downloads.

## Privacy

No accounts, no telemetry. The only internet use is the one-time download of the models.

**Optional AI second opinion.** If you run a local AI server with a vision model (LM Studio, Ollama or llama.cpp),
tick "AI second opinion" and it will settle close calls between equally good shots. The app only talks to servers
on this computer or your home network and refuses anything else. It never overrides the eyes-closed or blur checks.

## Command line

`python -m burstbuster scan <folder> [--no-subfolders] [--level identical|pose|one] [--report out.json]`
prints what the app would do and moves nothing.

## Limits

Windows only. Photos only (JPEG, HEIC, PNG and common RAW formats); videos are not analysed, though Live Photo
videos move with their photo.

## Bugs and ideas welcome

Found a photo it got wrong, a crash, or something confusing? Have an idea that would make it better?
**[Open an issue](https://github.com/EnragedAntelope/burstbuster/issues/new/choose)**. Every report helps.
Please describe the photos instead of attaching personal ones, unless you're happy for them to be public.

Pull requests are welcome too. Run the tests with `.venv\Scripts\python -m pytest tests -q`; notes for working
on the code are in [AGENTS.md](AGENTS.md).

## Credits

Built on [MediaPipe](https://ai.google.dev/edge/mediapipe) (face, pose and object detection) and
[DINOv2](https://github.com/facebookresearch/dinov2) (image similarity), both Apache 2.0, plus
[pywebview](https://pywebview.flowrl.com/), Pillow, pillow-heif, rawpy, NumPy and ONNX Runtime.
The people and places in the screenshots are AI-generated, not real photos.

## License

[MIT](LICENSE)
