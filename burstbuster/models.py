"""Model files: one-time download from pinned URLs, verified by SHA256."""
import hashlib
import urllib.request
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
_MP = "https://storage.googleapis.com/mediapipe-models/"

# Versioned URLs (not "latest") so the hashes stay valid. All Apache-2.0.
MODELS = {
    "face_landmarker.task": (
        _MP + "face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"),
    "pose_landmarker_full.task": (
        _MP + "pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task",
        "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"),
    "efficientdet_lite0.tflite": (
        _MP + "object_detector/efficientdet_lite0/int8/1/efficientdet_lite0.tflite",
        "0720bf247bd76e6594ea28fa9c6f7c5242be774818997dbbeffc4da460c723bb"),
    "dinov2_small.onnx": (
        "https://huggingface.co/onnx-community/dinov2-small/resolve/"
        "8b1f705a3a7f6f062f6bdd21986c1583d3ef105d/onnx/model.onnx",
        "f22797eabf810a75e41de68d378541ebea372122b25c4ce3ef25ff618250c20a"),
}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_models(log=print):
    """Download any missing model and verify every model's hash. Returns {name: path}."""
    MODEL_DIR.mkdir(exist_ok=True)
    out = {}
    for name, (url, sha) in MODELS.items():
        path = MODEL_DIR / name
        if not path.exists():
            log(f"Downloading model {name} (one time only)...")
            tmp = path.with_suffix(".part")
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    f.write(chunk)
            if _sha256(tmp) != sha:
                tmp.unlink()
                raise RuntimeError(f"Model {name} failed its integrity check; not used.")
            tmp.replace(path)
        elif _sha256(path) != sha:
            raise RuntimeError(f"Model file {path} does not match its pinned hash. Delete it to re-download.")
        out[name] = str(path)
    return out


if __name__ == "__main__":
    print(ensure_models())
