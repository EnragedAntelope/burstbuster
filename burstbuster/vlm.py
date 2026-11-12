"""Optional AI second opinion for close calls, via any local OpenAI-compatible server
(LM Studio, Ollama, llama.cpp, ...). Photos are only ever sent to this PC or the home network."""
import base64
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

import cv2

KNOWN = [("LM Studio", "http://127.0.0.1:1234/v1"),
         ("Ollama", "http://127.0.0.1:11434/v1"),
         ("llama.cpp", "http://127.0.0.1:8080/v1")]

PROMPT = ("These are {n} near-identical photos from one burst, labelled {labels}. Each is shown whole and, "
          "if there is a person, as a face close-up. Technical checks found them equally sharp. Pick the one a "
          "person would most want to keep: natural expression, eyes open, flattering moment, best pose or action. "
          'Reply with JSON only: {{"best": "<label>", "reason": "<one short sentence>"}}')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # never follow a redirect away from the local server


# no proxies: a system proxy setting would otherwise route photos off this network
_open = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open


def is_local(url):
    """True only for loopback or private-network hosts (checked after DNS resolution)."""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        ips = {ipaddress.ip_address(str(ai[4][0]).split("%")[0])
               for ai in socket.getaddrinfo(u.hostname, u.port or 80)}
        return bool(ips) and all(ip.is_private or ip.is_loopback for ip in ips)
    except (OSError, ValueError):
        return False


def list_models(url, timeout=1.5):
    if not is_local(url):
        raise ValueError("Only servers on this computer or your home network are allowed.")
    with _open(url.rstrip("/") + "/models", timeout=timeout) as r:
        data = json.load(r)
    return [m["id"] for m in data.get("data", [])]


def detect():
    """First running local server: {"name", "url", "models"} or None."""
    for name, url in KNOWN:
        try:
            models = list_models(url, timeout=0.8)
            if models:
                return {"name": name, "url": url, "models": models}
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def _jpeg_data_url(rgb, long_side):
    h, w = rgb.shape[:2]
    k = min(1.0, long_side / max(h, w))
    small = cv2.resize(rgb, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(small, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def choose(url, model, candidates, timeout=120):  # first call may wait for the server to load the model
    """candidates: [(label, rgb_array, face_box or None)]. Returns (label, reason) or raises."""
    if not is_local(url):
        raise ValueError("Only servers on this computer or your home network are allowed.")
    labels = [c[0] for c in candidates]
    content: list[dict] = [{"type": "text", "text": PROMPT.format(n=len(candidates), labels=", ".join(labels))}]
    for label, rgb, box in candidates:
        content.append({"type": "text", "text": f"Photo {label}:"})
        content.append({"type": "image_url", "image_url": {"url": _jpeg_data_url(rgb, 768)}})
        if box:
            x0, y0, x1, y1 = box
            p = (x1 - x0) * 0.4
            crop = rgb[int(max(y0 - p, 0)):int(y1 + p), int(max(x0 - p, 0)):int(x1 + p)]
            if crop.size:
                content.append({"type": "image_url", "image_url": {"url": _jpeg_data_url(crop, 384)}})
    body = json.dumps({"model": model, "temperature": 0, "max_tokens": 200,
                       "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with _open(req, timeout=timeout) as r:
        text = json.load(r)["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", text or "", re.S)
    ans = json.loads(m.group(0)) if m else {}
    best = str(ans.get("best", "")).strip().strip('"').upper()
    if best not in labels:
        raise ValueError(f"unclear answer from the model: {text[:80]!r}")
    return best, str(ans.get("reason", "")).strip()[:200]
