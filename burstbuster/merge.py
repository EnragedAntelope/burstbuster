"""Carry photo details (date, camera, place, rating, keywords, title, caption) from copies about to be recycled
over to the copy that is kept. Never edits a photo: missing details go into a new XMP sidecar next to the kept
file (IMG_1.jpg.xmp, read by digiKam, darktable and others), created only if none exists. The kept file's
modified time can be moved back to the oldest copy's."""
import html
import os
import re
from xml.sax.saxutils import escape, quoteattr

from PIL import Image

FIELDS = {"date": "date taken", "make": "camera", "model": "camera model", "gps": "place", "rating": "rating",
          "keywords": "keywords", "title": "title", "description": "caption"}
_NAMES = {**FIELDS, "sidecar": "the kept copy already has a sidecar"}
_XMP_TAGS = {"date": ("exif:DateTimeOriginal", "photoshop:DateCreated", "xmp:CreateDate"), "make": ("tiff:Make",),
             "model": ("tiff:Model",), "rating": ("xmp:Rating",), "lat": ("exif:GPSLatitude",),
             "lon": ("exif:GPSLongitude",)}


def _xmp_value(x, tag):
    m = re.search(rf'{tag}="([^"]*)"', x) or re.search(rf"<{tag}>([^<]*)</{tag}>", x)
    return html.unescape(m.group(1)).strip() if m else ""


def _xmp_items(x, tag):
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", x, re.S)
    return [html.unescape(v).strip() for v in re.findall(r"<rdf:li[^>]*>([^<]*)</rdf:li>", m.group(1))] if m else []


def _gps(v, ref):
    """EXIF degrees/minutes/seconds to the XMP form "52,22.500000N"."""
    d, m, s = (float(x) for x in v)
    return f"{int(d)},{m + s / 60 + (d - int(d)) * 60:.6f}{ref}"


def _read_xmp(x, out):
    """Fill fields missing from `out` (regex, not an XML parser: these are untrusted files)."""
    for f, tags in _XMP_TAGS.items():
        for t in tags:
            v = _xmp_value(x, t)
            if v and not out.get(f):
                out[f] = v
    for f, tag in (("keywords", "dc:subject"), ("title", "dc:title"), ("description", "dc:description")):
        items = [v for v in _xmp_items(x, tag) if v]
        if f == "keywords":
            out["keywords"] = sorted(set(out.get("keywords", [])) | set(items))
        elif items and not out.get(f):
            out[f] = items[0]


def details(primary, files=()):
    """What one copy knows about its photo, from its EXIF, embedded XMP and any .xmp sidecar in `files`."""
    out = {}
    try:
        with Image.open(primary) as im:
            e = im.getexif()
            ex, gps = e.get_ifd(0x8769), e.get_ifd(0x8825)
            xmp = im.info.get("xmp") or b""
        dt = str(ex.get(36867) or "").strip()
        if len(dt) >= 19:
            out["date"] = dt[:10].replace(":", "-") + "T" + dt[11:19]
        for f, tag in (("make", 271), ("model", 272)):
            if str(e.get(tag) or "").strip():
                out[f] = str(e.get(tag)).strip()
        if e.get(18246):
            out["rating"] = str(int(e.get(18246)))
        if gps.get(2) and gps.get(4):
            out["lat"], out["lon"] = _gps(gps[2], gps.get(1, "N")), _gps(gps[4], gps.get(3, "E"))
        _read_xmp(xmp.decode("utf-8", "replace") if isinstance(xmp, bytes) else str(xmp), out)
    except Exception:
        pass  # unreadable: contributes nothing
    for p in files:
        if p.lower().endswith(".xmp"):
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    _read_xmp(fh.read(1 << 20), out)
            except OSError:
                pass
    if out.get("lat") and out.get("lon"):
        out["gps"] = (out.pop("lat"), out.pop("lon"))
    out.pop("lat", None)
    out.pop("lon", None)
    return {k: v for k, v in out.items() if v}


def _gps_deg(v):
    m = re.fullmatch(r"(\d+),(\d+(?:\.\d+)?)([NSEW])", v.strip())
    if not m:
        return v.strip()
    d = int(m[1]) + float(m[2]) / 60
    return round(-d if m[3] in "SW" else d, 5)  # ~1 m: the same place written with different precision


def _norm(f, v):
    """Comparison key: equal keys mean the same detail, however it was written."""
    if f == "gps":
        return tuple(_gps_deg(x) for x in v)
    if f == "date":  # "2024-05-01T10:00:00.12+02:00" and "2024-05-01T10:00:00" are the same moment to the second
        m = re.match(r"(\d{4})[-:](\d\d)[-:](\d\d)(?:[T ](\d\d):(\d\d)(?::(\d\d))?)?", v)
        return m.groups(default="00") if m else v.strip()
    if f == "rating":
        try:
            return round(float(v))
        except ValueError:
            return v.strip()
    v = " ".join(str(v).split())
    return v.casefold() if f in ("keywords", "make", "model") else v


def _show(f, v):
    return ", ".join(v) if f == "gps" else str(v)


def plan(keeper, copies):
    """(add, conflicts, differs). add: details the kept copy lacks that the copies agree on, plus keywords it
    lacks (compared ignoring case and spacing). conflicts: blank fields the copies disagree on (left out).
    differs: [(field, value)] where a removed copy has a different value than the kept copy (kept value stays)."""
    add, conflicts, differs = {}, [], []
    for f in FIELDS:
        if f == "keywords":
            seen = {_norm(f, k) for k in keeper.get(f, [])}
            extra = []
            for k in (k for c in copies for k in c.get(f, [])):
                if _norm(f, k) not in seen:
                    seen.add(_norm(f, k))
                    extra.append(" ".join(k.split()))
            if extra:
                add[f] = list(keeper.get(f, [])) + extra  # the sidecar lists all, as it may override embedded ones
            continue
        vals = {}
        for c in copies:
            if c.get(f):  # equal details keep the longest spelling (the most precise date)
                key = _norm(f, c[f])
                vals[key] = str(key) if f == "rating" and isinstance(key, int) else \
                    max(vals.get(key, c[f]), c[f], key=lambda x: len(_show(f, x)))
        if keeper.get(f):
            differs += [(f, v) for k, v in vals.items() if k != _norm(f, keeper[f])]
        elif len(vals) == 1:
            add[f] = next(iter(vals.values()))
        elif vals:
            conflicts.append(f)
    return add, conflicts, differs


def describe(add, conflicts, differs=()):
    names = list(dict.fromkeys(FIELDS[f] for f in add))
    out = []
    if names:
        out.append("Keeps from its copies: " + ", ".join(names))
    if conflicts:
        out.append("Not carried over: " + ", ".join(dict.fromkeys(_NAMES[f] for f in conflicts)))
    for f in FIELDS:
        vs = [_show(f, v) for g, v in differs if g == f]
        if vs:
            out.append(f"Removed {'copy' if len(vs) == 1 else 'copies'} had {FIELDS[f]} {'; '.join(vs)} (kept this one's)")
    return out


def sidecar_path(primary):
    return primary + ".xmp"


def write_sidecar(primary, add):
    """Create primary + ".xmp" holding `add`. Never overwrites (raises FileExistsError)."""
    attrs = {"exif:DateTimeOriginal": add.get("date"), "tiff:Make": add.get("make"), "tiff:Model": add.get("model"),
             "xmp:Rating": add.get("rating")}
    if add.get("gps"):
        attrs["exif:GPSLatitude"], attrs["exif:GPSLongitude"] = add["gps"]
    a = "".join(f"\n   {k}={quoteattr(v)}" for k, v in attrs.items() if v)
    body = ""
    if add.get("keywords"):
        body += "\n   <dc:subject><rdf:Bag>" + "".join(f"<rdf:li>{escape(k)}</rdf:li>" for k in add["keywords"]) + \
                "</rdf:Bag></dc:subject>"
    for f, tag in (("title", "dc:title"), ("description", "dc:description")):
        if add.get(f):
            body += f'\n   <{tag}><rdf:Alt><rdf:li xml:lang="x-default">{escape(add[f])}</rdf:li></rdf:Alt></{tag}>'
    doc = ('<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="BurstBuster">\n'
           ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
           '  <rdf:Description rdf:about=""\n'
           '   xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
           '   xmlns:exif="http://ns.adobe.com/exif/1.0/" xmlns:tiff="http://ns.adobe.com/tiff/1.0/"'
           f"{a}>{body}\n  </rdf:Description>\n </rdf:RDF>\n</x:xmpmeta>\n")
    path = sidecar_path(primary)
    with open(path, "x", encoding="utf-8") as fh:  # "x": fail rather than replace an existing file
        fh.write(doc)
    return path


def apply(keeper_primary, keeper_files, copies):
    """Carry details from `copies` ([(primary, files, mtime_ns)] at their current paths) to the kept file.
    Returns {"sidecar": path|None, "mtime": new mtime_ns|None, "conflicts": [...], "differs": [...]} (see plan).
    Raises OSError on failure."""
    kd = details(keeper_primary, keeper_files)
    add, conflicts, differs = plan(kd, [details(p, f) for p, f, _ in copies])
    sidecar = None
    if add:
        if any(p.lower().endswith(".xmp") for p in keeper_files) or os.path.exists(sidecar_path(keeper_primary)):
            conflicts = conflicts + ["sidecar"]  # never edit an existing sidecar
        else:
            sidecar = write_sidecar(keeper_primary, add)
    st = os.stat(keeper_primary)
    oldest = min([mt for _, _, mt in copies], default=st.st_mtime_ns)
    mtime = None
    if oldest < st.st_mtime_ns:
        os.utime(keeper_primary, ns=(st.st_atime_ns, oldest))
        mtime = oldest
    return {"sidecar": sidecar, "mtime": mtime, "conflicts": conflicts, "differs": differs}
