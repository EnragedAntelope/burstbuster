"""Photos may only ever be sent to this PC or the home network."""
import urllib.request

import pytest

from burstbuster import vlm


@pytest.mark.parametrize("url", ["http://127.0.0.1:1234/v1", "http://localhost:11434/v1",
                                 "http://192.168.0.20:8080/v1", "http://10.0.0.5/v1"])
def test_local_allowed(url):
    assert vlm.is_local(url)


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://8.8.8.8/v1", "file:///etc/passwd",
                                 "ftp://127.0.0.1/", "http:///v1", "not a url"])
def test_remote_refused(url):
    assert not vlm.is_local(url)


def test_choose_refuses_remote_before_sending():
    with pytest.raises(ValueError):
        vlm.choose("http://8.8.8.8/v1", "m", [])


def test_system_proxy_is_ignored(monkeypatch):
    """A proxy setting must not route photos off the local network."""
    import importlib
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:3128")
    try:
        mod = importlib.reload(vlm)  # the opener is built at import, so build it with the proxy set
        assert not any(isinstance(h, urllib.request.ProxyHandler) for h in mod._open.__self__.handlers)
    finally:
        monkeypatch.delenv("HTTP_PROXY")
        importlib.reload(vlm)


@pytest.mark.parametrize("text,best", [
    ('{"best": "A", "reason": "bright smile with open eyes, making', "A"),  # cut off by the token limit
    ('Sure! {"best": "B", "reason": "sharper"}', "B"),
    ("I cannot tell", None)])
def test_answer_parsing(monkeypatch, text, best):
    import io
    import json

    import numpy as np
    reply = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
    monkeypatch.setattr(vlm, "_open", lambda req, timeout: io.BytesIO(reply))
    if best is None:
        with pytest.raises(ValueError):
            vlm.choose("http://127.0.0.1:1/v1", "m", [])
    else:
        assert vlm.choose("http://127.0.0.1:1/v1", "m", [(best, np.zeros((8, 8, 3), "uint8"), None)])[0] == best
