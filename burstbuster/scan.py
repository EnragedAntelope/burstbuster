"""Inventory: walk a folder, group companion files into shots, read EXIF. Read-only."""
import hashlib
import struct
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pillow_heif
from PIL import Image

from . import HOLDING_DIR

pillow_heif.register_heif_opener()  # without it HEIC photos get no capture time and never group by time

IMAGE_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png"}
RAW_EXT = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}
COMPANION_EXT = {".mov", ".mp4", ".aae", ".xmp"}
ALL_EXT = IMAGE_EXT | RAW_EXT | COMPANION_EXT

# Cloud-only placeholders (OneDrive etc.): reading them would trigger a download.
_CLOUD_ATTRS = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


@dataclass
class Shot:
    """One photo plus every file that belongs with it (RAW pair, Live Photo video, sidecars)."""
    primary: str                      # the file we analyse
    files: list = field(default_factory=list)  # [(path, size, mtime_ns)] for every file in the shot
    time: float | None = None         # capture time, epoch seconds (camera clock, naive)
    camera: str = ""
    exif: dict = field(default_factory=dict)

    @property
    def size(self):
        return sum(s for _, s, _ in self.files)

    @property
    def id(self):
        return self.primary


def _is_link(entry):
    return entry.is_symlink() or entry.is_junction()


def walk(root, recursive=True, skipped=None):
    """Yield (path, stat) for candidate files. Never follows links/junctions; skips the holding folder.
    Cloud-only placeholders are appended to `skipped` instead of yielded."""
    skipped = [] if skipped is None else skipped
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if _is_link(e):
                continue
            if e.is_dir(follow_symlinks=False):
                if recursive and e.name != HOLDING_DIR and not e.name.startswith("."):
                    stack.append(e.path)
                continue
            if Path(e.name).suffix.lower() not in ALL_EXT:
                continue
            st = e.stat(follow_symlinks=False)
            if getattr(st, "st_file_attributes", 0) & _CLOUD_ATTRS:
                skipped.append(e.path)
                continue
            yield e.path, st


def _key(path):
    """Companion key: folder + stem without any extension ('IMG_1.JPG.xmp' -> 'img_1')."""
    p = Path(path)
    name = p.name.lower()
    while Path(name).suffix.lower() in ALL_EXT:
        name = name[: -len(Path(name).suffix)]
    return (str(p.parent).lower(), name)


def _pick_primary(paths):
    for exts in (IMAGE_EXT, RAW_EXT):
        cands = [p for p in paths if Path(p).suffix.lower() in exts]
        if cands:
            # prefer JPEG/HEIC over PNG; stable by name
            return sorted(cands, key=lambda p: (Path(p).suffix.lower() == ".png", p))[0]
    return None


def _apple_burst_id(mn):
    """iPhone burst-mode id (maker note tag 0x000B), or None. Only set when the shutter is held down."""
    if not isinstance(mn, bytes) or not mn.startswith(b"Apple iOS\0") or mn[12:14] != b"MM":
        return None
    try:
        for i in range(struct.unpack(">H", mn[14:16])[0]):
            e = 16 + 12 * i
            tag, typ, cnt, val = struct.unpack(">HHII", mn[e:e + 12])
            if tag == 0x000B and typ == 2:  # ASCII; offsets are from the maker note start
                raw = mn[val:val + cnt] if cnt > 4 else mn[e + 8:e + 8 + cnt]
                return raw.rstrip(b"\0").decode("ascii", "replace") or None
    except struct.error:
        pass
    return None


def read_exif(path):
    """Capture time, camera id and a few settings. Missing values are simply absent."""
    out = {}
    try:
        if Path(path).suffix.lower() in RAW_EXT:
            return out  # shortcut: RAW-only shots get no EXIF time, add rawpy/exiftool metadata if needed
        with Image.open(path) as im:
            out["dims"] = im.size  # stored pixels (before EXIF rotation); header only, no decode
            e = im.getexif()
            ex = e.get_ifd(0x8769)
    except Exception:
        return out
    dt = ex.get(36867) or e.get(306)
    if dt:
        try:
            t = datetime.strptime(str(dt).strip()[:19], "%Y:%m:%d %H:%M:%S")
            sub = str(ex.get(37521) or "0").strip() or "0"
            digits = "".join(c for c in sub if c.isdigit())
            out["time"] = t.timestamp() + (float("0." + digits) if digits else 0.0)
        except ValueError:
            pass
    out["camera"] = " ".join(str(x).strip() for x in (e.get(271), e.get(272), ex.get(42033)) if x)
    burst = _apple_burst_id(ex.get(0x927C))
    if burst:
        out["burst"] = burst
    for tag, name in ((33434, "exposure"), (33437, "fnumber"), (34855, "iso"), (37386, "focal")):
        v = ex.get(tag)
        if v is not None:
            try:
                out[name] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def inventory(root, recursive=True):
    """Return (shots, skipped_cloud_paths)."""
    groups, skipped = {}, []
    for path, st in walk(root, recursive, skipped):
        groups.setdefault(_key(path), []).append((path, st.st_size, st.st_mtime_ns))
    shots = []
    for files in groups.values():
        primary = _pick_primary([f[0] for f in files])
        if primary is None:
            continue  # a video or sidecar on its own is not a photo; leave it alone
        ex = read_exif(primary)
        shots.append(Shot(primary=primary, files=sorted(files), time=ex.get("time"),
                          camera=ex.get("camera", ""), exif=ex))
    shots.sort(key=lambda s: (s.time is None, s.time or 0, s.primary))
    return shots, skipped


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def exact_duplicates(shots):
    """Groups of shots whose primary files are byte-identical. Hashes only same-size files."""
    by_size = {}
    for s in shots:
        by_size.setdefault(os.path.getsize(s.primary), []).append(s)
    groups = []
    for same in by_size.values():
        if len(same) < 2:
            continue
        by_hash = {}
        for s in same:
            by_hash.setdefault(file_hash(s.primary), []).append(s)
        groups += [g for g in by_hash.values() if len(g) > 1]
    return groups
