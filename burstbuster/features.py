"""Per-photo measurements. Pure read: opens the image file read-only, never writes it.

Faces are found by locating each person's head with the pose model, then running the
face landmarker on a crop around the head. The face model alone misses full-body shots
(faces too small) and misreads eyes at low resolution; the crop fixes both.
"""
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from .scan import RAW_EXT

WORK_LONG = 1280      # image size fed to the pose / object / embedding models
EYE_CLOSED = 0.6      # eyeBlink blendshape above this = eye closed (open eyes behind glasses read up to ~0.45)
_PETS = {"dog", "cat", "bird", "horse", "sheep", "cow", "bear", "elephant", "zebra", "giraffe"}

_m = {}  # per-process MediaPipe objects


def init_models(paths):
    """Create the MediaPipe tasks once per process. `paths` is models.ensure_models()."""
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision as v
    _m["mp"] = mp
    _m["face"] = v.FaceLandmarker.create_from_options(v.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=paths["face_landmarker.task"]),
        num_faces=4, output_face_blendshapes=True, output_facial_transformation_matrixes=True,
        min_face_detection_confidence=0.3))
    _m["pose"] = v.PoseLandmarker.create_from_options(v.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=paths["pose_landmarker_full.task"]), num_poses=6))
    _m["obj"] = v.ObjectDetector.create_from_options(v.ObjectDetectorOptions(
        base_options=BaseOptions(model_asset_path=paths["efficientdet_lite0.tflite"]),
        score_threshold=0.35, max_results=8))
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = 2  # several worker processes run at once; keep each one lean
    _m["dino"] = ort.InferenceSession(paths["dinov2_small.onnx"], so, providers=["CPUExecutionProvider"])


_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def embed(rgb, long=322):
    """DINOv2 CLS embedding of the whole frame, L2-normalised. Keeps aspect ratio (no centre crop,
    which would cut off raised arms in portrait shots)."""
    h, w = rgb.shape[:2]
    k = long / max(h, w)
    w2, h2 = max(14, round(w * k / 14) * 14), max(14, round(h * k / 14) * 14)
    a = cv2.resize(rgb, (w2, h2), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    a = ((a - _MEAN) / _STD).transpose(2, 0, 1)[None]
    cls = np.asarray(_m["dino"].run(None, {"pixel_values": a})[0])[0, 0]
    return [float(x) for x in cls / np.linalg.norm(cls)]


def _mpimg(a):
    mp = _m["mp"]
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(a))


def load_image(path):
    """RGB array at about half resolution (fast JPEG draft decode), orientation applied."""
    ext = Path(path).suffix.lower()
    if ext in RAW_EXT:
        import io
        import rawpy
        with rawpy.imread(path) as raw:
            th = raw.extract_thumb()
        if th.format == rawpy.ThumbFormat.JPEG:
            im = Image.open(io.BytesIO(th.data))
        else:
            im = Image.fromarray(th.data)
    else:
        if ext in (".heic", ".heif"):
            import pillow_heif
            pillow_heif.register_heif_opener()
        im = Image.open(path)
        if im.format == "JPEG":
            im.draft("RGB", (im.width // 2, im.height // 2))
    im = ImageOps.exif_transpose(im).convert("RGB")
    if max(im.size) > 3000:
        im.thumbnail((3000, 3000), Image.Resampling.LANCZOS)
    return np.asarray(im)


def sharpness(gray, box):
    """Variance of the Laplacian on the region, resized to a fixed size so distance changes
    don't skew it, divided by brightness so exposure doesn't masquerade as focus."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    crop = gray[max(y0, 0):max(y1, 1), max(x0, 0):max(x1, 1)]
    if crop.size < 64:
        return 0.0
    s = 384 / max(crop.shape)
    crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    lap = cv2.Laplacian(crop, cv2.CV_64F).var()
    return float(lap / max(crop.mean() / 255.0, 0.05))


def _yaw_pitch(matrix):
    r = np.asarray(matrix)[:3, :3]
    yaw = math.degrees(math.atan2(-r[2, 0], math.hypot(r[2, 1], r[2, 2])))
    pitch = math.degrees(math.atan2(r[2, 1], r[2, 2]))
    return yaw, pitch


def _face_in_crop(img, cx, cy, half):
    """Run the face landmarker on a square crop; return the face nearest the crop centre."""
    H, W = img.shape[:2]
    x0, y0 = int(max(cx - half, 0)), int(max(cy - half, 0))
    x1, y1 = int(min(cx + half, W)), int(min(cy + half, H))
    if x1 - x0 < 32 or y1 - y0 < 32:
        return None
    r = _m["face"].detect(_mpimg(img[y0:y1, x0:x1]))
    best, bestd = None, 1e9
    for lms, bs, mat in zip(r.face_landmarks, r.face_blendshapes, r.facial_transformation_matrixes):
        xs = [p.x for p in lms]
        ys = [p.y for p in lms]
        fx = (min(xs) + max(xs)) / 2 * (x1 - x0) + x0
        fy = (min(ys) + max(ys)) / 2 * (y1 - y0) + y0
        d = math.hypot(fx - cx, fy - cy)
        if d < bestd:
            b = {c.category_name: c.score for c in bs}
            yaw, pitch = _yaw_pitch(mat)
            box = [min(xs) * (x1 - x0) + x0, min(ys) * (y1 - y0) + y0,
                   max(xs) * (x1 - x0) + x0, max(ys) * (y1 - y0) + y0]
            best, bestd = {
                "box": box,
                "blink_l": b.get("eyeBlinkLeft", 0.0), "blink_r": b.get("eyeBlinkRight", 0.0),
                "smile": (b.get("mouthSmileLeft", 0.0) + b.get("mouthSmileRight", 0.0)) / 2,
                "jaw_open": b.get("jawOpen", 0.0),
                "yaw": yaw, "pitch": pitch,
            }, d
    return best


def measure(path):
    """All measurements for one photo, as a JSON-friendly dict."""
    img = load_image(path)
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    k = WORK_LONG / max(H, W)
    small = cv2.resize(img, (int(W * k), int(H * k)), interpolation=cv2.INTER_AREA)
    s_img = _mpimg(small)

    # People: pose -> body box + head position
    people, heads = [], []
    for lm in _m["pose"].detect(s_img).pose_landmarks:
        vis = [p for p in lm if (p.visibility or 0) > 0.5]
        if len(vis) < 6:
            continue
        xs, ys = [p.x * W for p in vis], [p.y * H for p in vis]
        box = [min(xs), min(ys), max(xs), max(ys)]
        people.append(box)
        hp = [lm[i] for i in (0, 2, 5, 7, 8) if (lm[i].visibility or 0) > 0.3]
        if hp:
            hx = np.mean([p.x for p in hp]) * W
            hy = np.mean([p.y for p in hp]) * H
            span = (max(p.x for p in hp) - min(p.x for p in hp)) * W
            heads.append((hx, hy, max(span * 3.0, 0.08 * max(H, W))))
    # Close-ups where the body isn't visible: the face model finds them directly.
    for lms in _m["face"].detect(s_img).face_landmarks:
        xs, ys = [p.x * W for p in lms], [p.y * H for p in lms]
        cx, cy, w = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs)
        if all(math.hypot(cx - hx, cy - hy) > hs / 3 for hx, hy, hs in heads):
            heads.append((cx, cy, w * 1.5))

    faces = []
    for hx, hy, half in heads:
        f = _face_in_crop(img, hx, hy, half)
        if f and all(math.hypot((f["box"][0] + f["box"][2]) / 2 - (g["box"][0] + g["box"][2]) / 2,
                                (f["box"][1] + f["box"][3]) / 2 - (g["box"][1] + g["box"][3]) / 2)
                     > (f["box"][2] - f["box"][0]) / 2 for g in faces):
            faces.append(f)
    for f in faces:
        x0, y0, x1, y1 = f["box"]
        f["cut_off"] = x0 < 2 or y0 < 2 or x1 > W - 2 or y1 > H - 2
        f["sharp"] = sharpness(gray, f["box"])

    pets, det_people = [], []
    for d in _m["obj"].detect(s_img).detections:
        if d.categories:
            b = d.bounding_box
            box = [b.origin_x / k, b.origin_y / k, (b.origin_x + b.width) / k, (b.origin_y + b.height) / k]
            name = d.categories[0].category_name
            if name == "person":
                det_people.append(box)
            elif name in _PETS and not people:
                pets.append(box)
    persons = _persons(small, k, gray, H, det_people, people, faces)

    if faces:  # background people don't count: keep faces at least half the size of the biggest
        big = max(f["box"][2] - f["box"][0] for f in faces)
        faces = [f for f in faces if f["box"][2] - f["box"][0] >= 0.5 * big]

    # Subject region: faces, else largest person, else animal, else centre
    if faces:
        subject, kind = None, "face"
        subj_sharp = float(np.mean([f["sharp"] for f in faces]))
    else:
        if people:
            subject, kind = max(people, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])), "person"
        elif pets:
            subject, kind = max(pets, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])), "animal"
        else:
            subject, kind = [W * 0.2, H * 0.2, W * 0.8, H * 0.8], "centre"
        subj_sharp = sharpness(gray, subject)

    g_small = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    return {
        "w": W, "h": H,
        "subject_kind": kind,
        "sharp": subj_sharp,
        "frame_sharp": sharpness(gray, [W * 0.2, H * 0.2, W * 0.8, H * 0.8]),
        # body sharpness too, so a burst where only some faces were found can still compare like with like
        "body_sharp": sharpness(gray, max(people, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))) if people else None,
        "clip_hi": float((g_small >= 250).mean()),
        "clip_lo": float((g_small <= 5).mean()),
        "brightness": float(g_small.mean() / 255),
        "faces": faces,
        "n_people": len(people),
        "persons": persons,
        "emb": embed(small),
    }


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _persons(small, k, gray, H, det_boxes, pose_boxes, faces):
    """Everyone in the photo, so a burst can follow one person: box, look (embedding of the
    person crop), sharpness and their face (if found). Faces here are not size-filtered."""
    boxes = [b for b in det_boxes if b[3] - b[1] >= 0.05 * H]
    boxes += [b for b in pose_boxes if all(_iou(b, d) < 0.4 for d in boxes)]
    out = []
    for b in boxes:
        x0, y0, x1, y1 = (int(round(v * k)) for v in b)
        crop = small[max(y0, 0):max(y1, 1), max(x0, 0):max(x1, 1)]
        if crop.shape[0] < 14 or crop.shape[1] < 14:
            continue
        out.append({"box": [float(v) for v in b], "sharp": sharpness(gray, b),
                    "emb": embed(crop, 224), "face": None})
    for f in faces:  # each face goes to the smallest person box around its centre
        cx, cy = (f["box"][0] + f["box"][2]) / 2, (f["box"][1] + f["box"][3]) / 2
        inside = [p for p in out if p["face"] is None and p["box"][0] <= cx <= p["box"][2] and p["box"][1] <= cy <= p["box"][3]]
        if inside:
            min(inside, key=lambda p: (p["box"][2] - p["box"][0]) * (p["box"][3] - p["box"][1]))["face"] = dict(f)
    return out
