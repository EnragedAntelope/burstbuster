"""Photos may only ever be sent to this PC or the home network."""
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
