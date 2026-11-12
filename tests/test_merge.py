"""Carrying photo details from removed copies to the kept one. Synthetic JPEGs in temp folders only."""
import os
from fractions import Fraction

import pytest
from PIL import Image

from burstbuster import fileops, merge
from burstbuster.app import Api


@pytest.fixture(autouse=True)
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(fileops, "JOURNAL", tmp_path / "journal" / "journal.sqlite")


def _jpeg(path, gps=False, rating=None, keywords=()):
    ex = Image.Exif()
    ex[271], ex[272] = "Cam", "One"
    ex.get_ifd(0x8769)[36867] = "2024:05:01 10:00:00"
    if gps:
        g = ex.get_ifd(0x8825)
        g[1], g[2], g[3], g[4] = "N", (Fraction(52), Fraction(22), Fraction(30)), "E", (Fraction(4), Fraction(54), Fraction(0))
    if rating:
        ex[18246] = rating
    xmp = None
    if keywords:
        xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description><dc:subject><rdf:Bag>'
               + "".join(f"<rdf:li>{k}</rdf:li>" for k in keywords) + "</rdf:Bag></dc:subject></rdf:Description>"
               "</rdf:RDF></x:xmpmeta>").encode()
    Image.new("RGB", (64, 48), "gray").save(path, exif=ex, **({"xmp": xmp} if xmp else {}))
    return str(path)


def test_details_and_plan(tmp_path):
    keep = merge.details(_jpeg(tmp_path / "k.jpg"))
    a = merge.details(_jpeg(tmp_path / "a.jpg", gps=True, rating=5, keywords=("beach",)))
    b = merge.details(_jpeg(tmp_path / "b.jpg", rating=3, keywords=("family",)))
    assert a["gps"] == ("52,22.500000N", "4,54.000000E") and a["rating"] == "5" and a["keywords"] == ["beach"]
    add, conflicts, differs = merge.plan(keep, [a, b])
    assert add == {"gps": ("52,22.500000N", "4,54.000000E"), "keywords": ["beach", "family"]}
    assert conflicts == ["rating"]  # 5 vs 3: left out, and said so
    assert "date" not in add and differs == []  # the kept copy already has the same date


def test_plan_compares_details_not_spelling():
    keep = {"keywords": ["Beach"], "rating": "3", "date": "2024-05-01T10:00:00"}
    copies = [{"keywords": ["beach ", "Sun  set"], "rating": "5", "date": "2024-05-01T10:00:00.12+02:00",
               "gps": ("52,22.5N", "4,54E"), "title": "Day out"},
              {"keywords": ["sun set", "BEACH"], "rating": "5.0", "date": "2024:05:01 10:00:00",
               "gps": ("52,22.500000N", "4,54.000000E"), "title": "Day  out"}]
    add, conflicts, differs = merge.plan(keep, copies)
    assert add["keywords"] == ["Beach", "Sun set"]  # no case or spacing duplicates, kept copy's spelling first
    assert add["gps"] == ("52,22.500000N", "4,54.000000E") and add["title"] in ("Day out", "Day  out")
    assert conflicts == []  # the same place, date and title, written differently
    assert differs == [("rating", "5")]  # kept copy's 3 stays, and the removed copies' 5 is reported
    assert "Removed copy had rating 5 (kept this one's)" in merge.describe(add, conflicts, differs)


def test_apply_writes_a_sidecar_once_and_takes_the_oldest_date(tmp_path):
    k = _jpeg(tmp_path / "k.jpg")
    a = _jpeg(tmp_path / "a.jpg", gps=True, rating=5)
    before = open(k, "rb").read()
    os.utime(a, ns=(1, 1_000_000_000))
    r = merge.apply(k, [k], [(a, [a], os.stat(a).st_mtime_ns)])
    assert r["sidecar"] == k + ".xmp" and r["conflicts"] == []
    assert open(k, "rb").read() == before  # the photo itself is never edited
    assert os.stat(k).st_mtime_ns == 1_000_000_000
    back = merge.details(k, [r["sidecar"]])  # the sidecar reads back as the details it was given
    assert back["gps"] == ("52,22.500000N", "4,54.000000E") and back["rating"] == "5"
    side = open(r["sidecar"], encoding="utf-8").read()
    r2 = merge.apply(k, [k], [(a, [a], os.stat(a).st_mtime_ns)])  # an existing sidecar is never changed
    assert r2["sidecar"] is None and r2["conflicts"] == ["sidecar"]
    assert open(r["sidecar"], encoding="utf-8").read() == side


def test_xmp_text_is_escaped(tmp_path):
    k = _jpeg(tmp_path / "k.jpg")
    p = merge.write_sidecar(k, {"title": 'a <b> & "c"', "keywords": ["x<y"]})
    assert merge.details(k, [p]) == {**merge.details(k), "title": 'a <b> & "c"', "keywords": ["x<y"]}


def _report(root, keep, copy):
    shot = lambda p, keep: {"id": p, "files": [p], "stat": [[p, os.stat(p).st_size, os.stat(p).st_mtime_ns]],  # noqa: E731
                            "keep": keep, "held": False, "reasons": []}
    return {"root": str(root), "merge": True, "groups": [{"type": "duplicate", "top": keep, "flags": [],
                                                          "shots": [shot(keep, True), shot(copy, False)]}]}


def _api(rep, run):
    api = Api()
    api._report, api._run = rep, run
    api._persist = lambda: None
    return api


def test_carry_over_runs_on_set_aside_copies(tmp_path):
    k, c = _jpeg(tmp_path / "IMG_1.jpg"), _jpeg(tmp_path / "IMG_1 (1).jpg", rating=4)
    rep, run = _report(tmp_path, k, c), fileops.new_run_id()
    res = fileops.set_aside(str(tmp_path), run, [{"id": c, "stat": rep["groups"][0]["shots"][1]["stat"]}])
    assert c in res["moved"]
    rep["groups"][0]["shots"][1]["held"] = True
    assert _api(rep, run)._carry_over() == 0
    keeper = rep["groups"][0]["shots"][0]
    assert merge.details(k, keeper["files"])["rating"] == "4"
    assert keeper["files"] == [k, k + ".xmp"] and len(keeper["stat"]) == 2  # sidecar now moves with the photo


def test_carry_over_failure_puts_copies_back(tmp_path, monkeypatch):
    k, c = _jpeg(tmp_path / "IMG_1.jpg"), _jpeg(tmp_path / "IMG_1 (1).jpg", rating=4)
    rep, run = _report(tmp_path, k, c), fileops.new_run_id()
    fileops.set_aside(str(tmp_path), run, [{"id": c, "stat": rep["groups"][0]["shots"][1]["stat"]}])
    rep["groups"][0]["shots"][1]["held"] = True

    def boom(*a):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(merge, "write_sidecar", boom)
    assert _api(rep, run)._carry_over() == 1
    copy = rep["groups"][0]["shots"][1]
    assert os.path.exists(c) and copy["keep"] and not copy["held"]  # back in place, never recycled
    assert "not carried over" in rep["groups"][0]["flags"][0]


def test_heic_copy_with_edited_details_matches(tmp_path):
    from burstbuster import scan
    im = Image.new("RGB", (64, 48), "teal")
    ex = Image.Exif()
    ex[18246] = 5
    im.save(tmp_path / "a.heic")
    im.save(tmp_path / "b.heic", exif=ex)  # same picture, a rating added
    Image.new("RGB", (64, 48), "red").save(tmp_path / "c.heic")
    h = {n: scan.pixel_hash(str(tmp_path / n)) for n in ("a.heic", "b.heic", "c.heic")}
    assert open(tmp_path / "a.heic", "rb").read() != open(tmp_path / "b.heic", "rb").read()
    assert h["a.heic"] and h["a.heic"] == h["b.heic"] != h["c.heic"]
