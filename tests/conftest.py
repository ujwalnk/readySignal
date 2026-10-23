import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import notify
from app.engine import Engine

SECRET = "correct-horse-battery-staple"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 15, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


def make_engine(data_dir, clock, secret=SECRET):
    e = Engine(data_dir, secret, clock)
    e.load()
    return e


@pytest.fixture
def engine(data_dir, clock):
    return make_engine(data_dir, clock)


@pytest.fixture
def curl(monkeypatch):
    """Replace curl_post; `curl.results` is a list of CurlResult consumed in order."""
    calls = []
    results = []

    async def fake(url, body, connect_timeout, total_timeout):
        calls.append({"url": url, "body": body})
        return results.pop(0) if results else notify.CurlResult(0, 200, "ok")

    monkeypatch.setattr(notify, "curl_post", fake)
    fake.calls, fake.results = calls, results
    return fake


def run(coro):
    return asyncio.run(coro)


async def setup_slack(e, template=None):
    patch = {"enabled": True, "webhook_url": "https://hooks.slack.com/services/T0/B0/SECRETTOKEN"}
    if template:
        patch["template"] = template
    await e.update_settings({"slack": patch})


def csrf_token(client):
    html = client.get("/").text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


# ---------------------------------------------------------------- image helpers
import base64 as _b64
import struct as _struct
import zlib as _zlib


def make_png(w=2, h=2):
    def chunk(t, d):
        return _struct.pack(">I", len(d)) + t + d + _struct.pack(">I", _zlib.crc32(t + d) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", _struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", _zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_jpeg(w=16, h=16):
    sof = b"\xff\xc0\x00\x0b\x08" + _struct.pack(">HH", h, w) + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + sof + b"\xff\xd9"


WEBP = _b64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>t</title><circle cx="5" cy="5" r="4" fill="#0b6b63"/></svg>'


def new_account(e, name="Img", **kw):
    return run(e.create_account({"name": name, "duration_seconds": 3600, **kw}))
