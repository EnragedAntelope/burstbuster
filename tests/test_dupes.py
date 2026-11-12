"""Copies-only mode on synthetic JPEGs: exact copies, same picture with more metadata, smaller saves."""
import shutil

import numpy as np
from PIL import Image

from burstbuster import scan
from burstbuster.pipeline import analyze


def _photo(path, seed, size=(400, 300), when="2024:05:01 10:00:00"):
    rng = np.random.default_rng(seed)
    a = (rng.random((30, 40, 3)) * 255).astype(np.uint8)  # blocky picture survives resizing, unlike pixel noise
    im = Image.fromarray(a).resize(size, Image.Resampling.NEAREST)
    ex = Image.Exif()
    ex[271], ex[272] = "Cam", "One"
    ex.get_ifd(0x8769)[36867] = when
    im.save(path, quality=92, exif=ex)


def _with_xmp(src, dst):
    """Same JPEG with an XMP rating segment added after the start marker: new bytes, same picture data."""
    data = open(src, "rb").read()
    body = b"http://ns.adobe.com/xap/1.0/\0" + b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><xmp:Rating>5</xmp:Rating></x:xmpmeta>'
    seg = b"\xff\xe1" + (len(body) + 2).to_bytes(2, "big") + body
    open(dst, "wb").write(data[:2] + seg + data[2:])


def test_pixel_hash_ignores_metadata_but_not_pixels(tmp_path):
    _photo(tmp_path / "a.jpg", 1)
    _with_xmp(tmp_path / "a.jpg", tmp_path / "b.jpg")
    _photo(tmp_path / "c.jpg", 2)
    h = {n: scan.pixel_hash(str(tmp_path / n)) for n in ("a.jpg", "b.jpg", "c.jpg")}
    assert h["a.jpg"] and h["a.jpg"] == h["b.jpg"] != h["c.jpg"]
    assert scan.read_exif(str(tmp_path / "b.jpg"))["tags"] > scan.read_exif(str(tmp_path / "a.jpg"))["tags"]


def test_dupes_mode_keeps_one_best_copy(tmp_path):
    _photo(tmp_path / "IMG_1.jpg", 1)
    shutil.copy(tmp_path / "IMG_1.jpg", tmp_path / "IMG_1 (1).jpg")           # exact copy
    _with_xmp(tmp_path / "IMG_1.jpg", tmp_path / "IMG_1 - Copy.jpg")           # rated copy: more details wins
    Image.open(tmp_path / "IMG_1.jpg").resize((200, 150)).save(                # smaller save, same EXIF
        tmp_path / "small.jpg", exif=Image.open(tmp_path / "IMG_1.jpg").getexif())
    _photo(tmp_path / "other.jpg", 2)                                          # same time and size, different picture
    rep = analyze(str(tmp_path), mode="dupes")
    assert rep["mode"] == "dupes" and rep["photos"] == 5
    assert len(rep["groups"]) == 1, rep["groups"]
    ids = [s["id"] for g in rep["groups"] for s in g["shots"]]
    assert len(ids) == len(set(ids)) == 4  # chained copy groups merge: each photo appears exactly once
    shots = {s["id"].split("\\")[-1].split("/")[-1]: s for s in rep["groups"][0]["shots"]}
    assert [n for n, s in shots.items() if s["keep"]] == ["IMG_1 - Copy.jpg"]
    assert set(shots) == {"IMG_1 - Copy.jpg", "IMG_1.jpg", "IMG_1 (1).jpg", "small.jpg"}
    assert "more photo details" in shots["IMG_1 - Copy.jpg"]["reasons"][0]
    assert shots["small.jpg"]["reasons"][0].startswith("Smaller copy (200x150)")


def test_look_tells_resize_from_other_picture(tmp_path):
    _photo(tmp_path / "a.jpg", 1)
    Image.open(tmp_path / "a.jpg").resize((120, 90)).save(tmp_path / "s.jpg")
    Image.open(tmp_path / "a.jpg").convert("L").resize((120, 90)).save(tmp_path / "g.jpg")  # black-and-white edit
    _photo(tmp_path / "b.jpg", 2)
    la, ls, lg, lb = (np.array(scan.look(str(tmp_path / n))) for n in ("a.jpg", "s.jpg", "g.jpg", "b.jpg"))
    assert la @ ls > 0.97 > max(la @ lg, la @ lb)
