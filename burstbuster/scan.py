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

    @property
    def stat(self):
        """(size, mtime_ns) of the primary file as the walk saw it."""
        return next((sz, mt) for p, sz, mt in self.files if p == self.primary)


def _is_link(entry):
    if entry.is_symlink():
        return True
    # junctions are reparse-point folders (DirEntry.is_junction needs Python 3.12); files can be reparse
    # points too (OneDrive), so only folders count here
    return entry.is_dir(follow_symlinks=False) and bool(
        getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)  # REPARSE_POINT


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
            # how much a copy carries: used to keep the best-described of identical pictures
            out["tags"] = len(e) + len(ex) + len(e.get_ifd(0x8825)) + bool(im.info.get("xmp"))
            out["orient"] = e.get(274, 1)
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
        # a second picture of the same name (photo.jpg + photo.png, IMG_1.jpg + img_1.jpg on Linux) is a shot
        # of its own, judged like any other, never a passenger that moves with the first
        extra = [f for f in files if f[0] != primary and Path(f[0]).suffix.lower() in IMAGE_EXT]
        for p, own in [(primary, [f for f in files if f not in extra])] + [(f[0], [f]) for f in extra]:
            ex = read_exif(p)
            shots.append(Shot(primary=p, files=sorted(own), time=ex.get("time"), camera=ex.get("camera", ""),
                              exif=ex))
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
        by_size.setdefault(s.stat[0], []).append(s)
    groups = []
    for same in by_size.values():
        if len(same) < 2:
            continue
        by_hash = {}
        for s in same:
            try:
                by_hash.setdefault(file_hash(s.primary), []).append(s)
            except OSError:  # gone or locked since the walk: not a copy of anything
                pass
        groups += [g for g in by_hash.values() if len(g) > 1]
    return groups


def pixel_hash(path):
    """Hash of the picture data only, skipping metadata, or None. A rating, tag or date written into a copy
    (Explorer, Lightroom, exiftool) changes the file but not this. Re-encoding changes it.
    JPEG and PNG are read without decoding; HEIC is decoded (slower, so only same size and time are compared).
    shortcut: RAW returns None, add if edited RAW copies show up."""
    h = hashlib.sha256()
    if Path(path).suffix.lower() in {".heic", ".heif"}:
        try:
            with Image.open(path) as im:
                h.update(f"{im.mode} {im.size}".encode())
                h.update(im.tobytes())
            return h.hexdigest()
        except Exception:
            return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                return None
            m = data[i + 1]
            if m == 0xFF:  # fill byte
                i += 1
                continue
            if m == 0xDA:  # start of scan: the rest is picture data
                h.update(data[i:])
                return h.hexdigest()
            end = i + 2 + int.from_bytes(data[i + 2:i + 4], "big")
            if not (0xE0 <= m <= 0xEF or m == 0xFE):  # keep tables, drop APPn (EXIF, XMP, ICC) and comments
                h.update(data[i:end])
            i = end
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        i = 8
        while i + 8 <= len(data):
            n = int.from_bytes(data[i:i + 4], "big")
            typ = data[i + 4:i + 8]
            if typ[:1].isupper():  # critical chunks (IHDR, PLTE, IDAT, IEND); text and EXIF chunks are ancillary
                h.update(data[i + 4:i + 8 + n])
            i += 12 + n
        return h.hexdigest()
    return None


def same_pixels(shots):
    """Groups of shots that are the same picture with different metadata (see pixel_hash). Only shots sharing
    stored size and capture time are read, so this stays cheap on a large library."""
    by_key = {}
    for s in shots:
        if s.exif.get("dims"):
            by_key.setdefault((s.exif["dims"], s.time), []).append(s)
    groups = []
    for same in by_key.values():
        if len(same) < 2:
            continue
        by_hash = {}
        for s in same:
            h = pixel_hash(s.primary)
            if h:  # a different rotation tag shows a different picture, so it is part of the key
                by_hash.setdefault((h, s.exif.get("orient", 1)), []).append(s)
        groups += [g for g in by_hash.values() if len(g) > 1]
    return groups


def look(path):
    """Tiny thumbnail (brightness pattern plus colour) as a unit vector, for telling a smaller save of a photo
    from a different photo without the models. Cosine with the original is about 1.0 for a resize and well
    below 0.97 for a black-and-white or recoloured edit; a zero vector if unreadable."""
    import numpy as np
    from PIL import ImageOps
    try:
        with Image.open(path) as im:
            if im.format == "JPEG":
                im.draft("YCbCr", (64, 64))
            t = np.asarray(ImageOps.exif_transpose(im).convert("YCbCr").resize((16, 16), Image.Resampling.BOX),
                           np.float32)
    except Exception:
        return [0.0] * 768
    y = t[..., 0].ravel()
    # brightness is centred (exposure edits still match); colour is not, so a grey edit has none
    a = np.concatenate([y - y.mean(), (t[..., 1:] - 128).ravel()])
    n = float(np.linalg.norm(a))
    return (a / n).tolist() if n else a.tolist()
