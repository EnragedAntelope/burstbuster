"""Copies-only mode on synthetic JPEGs: exact copies, same picture with more metadata, smaller saves."""
import os
import shutil

import numpy as np
from PIL import Image, ImageDraw

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
    la, ls, lg, lb = (np.array(scan.look(str(tmp_path / n))["emb"]) for n in ("a.jpg", "s.jpg", "g.jpg", "b.jpg"))
    assert la @ ls > 0.97 > max(la @ lg, la @ lb)


def _edit(src, dst, size=None):
    """Copy of a JPEG (same EXIF) with one corner filled in grey, as when something is removed, optionally saved
    smaller. The whole still looks alike (thumbnail cosine about 0.98)."""
    im = Image.open(src)
    ex = im.getexif()
    im = im.convert("RGB")
    ImageDraw.Draw(im).rectangle([im.width * 0.85, im.height * 0.85, im.width, im.height], fill=(128, 128, 128))
    (im.resize(size) if size else im).save(dst, quality=92, exif=ex)


def test_smaller_copy_with_a_local_edit_is_not_a_plain_copy(tmp_path):
    _photo(tmp_path / "IMG_1.jpg", 1)
    Image.open(tmp_path / "IMG_1.jpg").resize((200, 150)).save(tmp_path / "small.jpg",
                                                               exif=Image.open(tmp_path / "IMG_1.jpg").getexif())
    _edit(tmp_path / "IMG_1.jpg", tmp_path / "marked.jpg", (200, 150))  # same look overall, one corner changed
    from burstbuster import group
    lk = {n: scan.look(str(tmp_path / n)) for n in ("IMG_1.jpg", "small.jpg", "marked.jpg")}
    assert group.cos(lk["IMG_1.jpg"]["emb"], lk["marked.jpg"]["emb"]) >= group.NOTIME_SIM  # the whole still matches
    rep = analyze(str(tmp_path), mode="dupes")
    names = sorted(s["id"].replace("\\", "/").split("/")[-1] for g in rep["groups"] for s in g["shots"])
    assert names == ["IMG_1.jpg", "small.jpg"]


def test_edited_version_is_a_copy_shown_first_not_a_burst_frame(tmp_path):
    _photo(tmp_path / "IMG_1234.jpg", 1)
    _edit(tmp_path / "IMG_1234.jpg", tmp_path / "IMG_E1234.jpg")           # iPhone edit: same name, same second
    _photo(tmp_path / "trip.jpg", 3)
    _edit(tmp_path / "trip.jpg", tmp_path / "trip-edited.jpg")             # Google Takeout edit
    _edit(tmp_path / "trip-edited.jpg", tmp_path / "trip-edited-edited.jpg", (300, 200))  # edit of the edit: same set
    _photo(tmp_path / "IMG_E9.jpg", 4)                                     # edit-like name, original missing
    rep = analyze(str(tmp_path), mode="dupes")
    sets = {tuple(sorted(s["id"].replace("\\", "/").split("/")[-1] for s in g["shots"])): g for g in rep["groups"]}
    assert set(sets) == {("IMG_1234.jpg", "IMG_E1234.jpg"), ("trip-edited-edited.jpg", "trip-edited.jpg", "trip.jpg")}
    for g in sets.values():
        assert g["confidence"] == "close"
        keep = [s for s in g["shots"] if s["keep"]]
        aside = [s for s in g["shots"] if not s["keep"]]
        assert len(keep) == 1 and os.path.basename(keep[0]["id"]) in ("IMG_1234.jpg", "trip.jpg")
        assert all(s["reasons"][0].startswith("Edited version of") for s in aside)


def test_copy_holding_details_the_kept_one_lacks_is_flagged(tmp_path):
    _photo(tmp_path / "IMG_1.jpg", 1)
    (tmp_path / "IMG_1.mov").write_bytes(b"live photo")  # companion: this copy is kept
    _with_xmp(tmp_path / "IMG_1.jpg", tmp_path / "IMG_1 (1).jpg")  # the removed copy has a rating
    g = analyze(str(tmp_path), mode="dupes")["groups"][0]
    assert g["confidence"] == "close" and g["flags"] == ["Only the copies have: rating"]
    g = analyze(str(tmp_path), mode="dupes", keep_details=True)["groups"][0]  # carried over: nothing is lost
    assert g["confidence"] == "confident" and g["flags"] == []


def test_exact_copies_read_in_full_only_when_their_start_matches(tmp_path, monkeypatch):
    for name, head in (("a.jpg", b"A"), ("b.jpg", b"B"), ("c.jpg", b"B")):
        (tmp_path / name).write_bytes(head + b"\0" * 200_000)  # one size, like uncompressed RAWs
    reads = []
    real = scan.file_hash
    monkeypatch.setattr(scan, "file_hash", lambda p, limit=None: reads.append((p[-5:], limit)) or real(p, limit))
    shots = [scan.Shot(primary=str(tmp_path / n), files=[(str(tmp_path / n), 200_001, 1)])
             for n in ("a.jpg", "b.jpg", "c.jpg")]
    groups = scan.exact_duplicates(shots)
    assert [[s.primary[-5:] for s in g] for g in groups] == [["b.jpg", "c.jpg"]]
    assert sorted(r for r in reads if r[1] is None) == [("b.jpg", None), ("c.jpg", None)]  # a.jpg: 64 KB only
