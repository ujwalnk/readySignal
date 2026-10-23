import json

import pytest
from fastapi.testclient import TestClient

from app.assets import MAX_UPLOAD_BYTES, ImageError, sanitize_svg, validate_image
from app.engine import ASSETS_SUBDIR, NotFound, ValidationFailed, backup_blob
from app.main import create_app
from app.rotate import rotate
from app.store import StoreError

from conftest import SECRET, SVG, WEBP, csrf_token, make_engine, make_jpeg, make_png, new_account, run

FORMATS = [
    (make_png(), "image/png"),
    (make_jpeg(), "image/jpeg"),
    (WEBP, "image/webp"),
    (SVG, "image/svg+xml"),
]


@pytest.mark.parametrize("data,ctype", FORMATS)
def test_supported_formats_round_trip_encrypted_at_rest(engine, data_dir, data, ctype):
    a = new_account(engine)
    r = run(engine.set_icon(a["id"], data))
    assert r["icon_url"].startswith("/assets/icons/") and r["icon_asset"]["content_type"] == ctype
    body, got = engine.read_icon(r["icon_asset"]["id"])
    assert got == ctype and (body == data or ctype == "image/svg+xml")
    stored = (data_dir / ASSETS_SUBDIR / f"{r['icon_asset']['id']}.enc").read_bytes()
    assert json.loads(stored)["alg"] == "AES-256-GCM"  # uploaded files do not undermine the encryption boundary
    assert b"PNG" not in stored and b"<svg" not in stored and b"RIFF" not in stored


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"hello world",
        b"<html><script>alert(1)</script></html>",
        make_png()[:-20],  # truncated
        make_png(4097, 4),  # absurd dimensions
        make_jpeg()[:-2],  # no EOI
        b"RIFF\x04\x00\x00\x00WEBP",
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 10,
        b"MZ\x90\x00" + b"\x00" * 64,
        b"<svg",
    ],
)
def test_invalid_files_rejected(bad):
    with pytest.raises(ImageError):
        validate_image(bad)


def test_oversized_rejected(engine):
    a = new_account(engine)
    with pytest.raises(ValidationFailed):
        run(engine.set_icon(a["id"], make_png() + b"\x00" * MAX_UPLOAD_BYTES))
    assert engine.accounts["accounts"][0]["icon_asset"] is None


UNSAFE = {
    "script element": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><circle r="4"/></svg>',
    "event handler": b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><circle r="4" onclick="x()"/></svg>',
    "foreignObject": b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><iframe src="http://evil.test"/></foreignObject></svg>',
    "external image": b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><image xlink:href="http://evil.test/a.png"/></svg>',
    "external use": b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="http://evil.test/s.svg#x"/></svg>',
    "javascript href": b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:alert(1)"><circle r="4"/></a></svg>',
    "style import": b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(http://evil.test/x.css);</style><circle r="4" style="fill:url(http://evil.test/p)"/></svg>',
    "animate": b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"><animate attributeName="href" to="javascript:alert(1)"/></circle></svg>',
}


@pytest.mark.parametrize("name", UNSAFE)
def test_unsafe_svg_is_stripped(name):
    out = sanitize_svg(UNSAFE[name]).decode()
    for needle in (
        "script",
        "onload",
        "onclick",
        "evil.test",
        "javascript",
        "@import",
        "foreignObject",
        "iframe",
        "animate",
        "<image",
        "<style",
    ):
        assert needle not in out, (name, needle, out)
    assert out.startswith("<svg")


@pytest.mark.parametrize(
    "doc",
    [
        b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]><svg xmlns="http://www.w3.org/2000/svg">&a;</svg>',
        b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://evil.test/x.dtd"><svg xmlns="http://www.w3.org/2000/svg"/>',
        b"<svg>no namespace</svg>",
        b'<html xmlns="http://www.w3.org/1999/xhtml"/>',
        b"\xff\xfe<svg/>",
    ],
)
def test_svg_structural_rejections(doc):
    with pytest.raises(ImageError):
        validate_image(doc)


def test_safe_svg_features_survive():
    out = sanitize_svg(SVG).decode()
    assert "<circle" in out and 'fill="#0b6b63"' in out and "viewBox" in out


def test_replace_and_remove_cleans_files(engine, data_dir):
    a = new_account(engine)
    d = data_dir / ASSETS_SUBDIR
    first = run(engine.set_icon(a["id"], make_png()))["icon_asset"]["id"]
    second = run(engine.set_icon(a["id"], SVG))["icon_asset"]["id"]
    assert first != second and [p.stem for p in d.glob("*.enc")] == [second]
    with pytest.raises(NotFound):
        engine.read_icon(first)
    r = run(engine.clear_icon(a["id"]))
    assert r["icon_url"] is None and r["icon"] and not list(d.glob("*.enc"))
    with pytest.raises(NotFound):
        engine.read_icon(second)


def test_icon_change_does_not_touch_timer_or_identity(engine, clock):
    a = new_account(engine)
    t = run(engine.start(a["id"]))
    r = run(engine.set_icon(a["id"], make_png()))
    assert (r["ts_expired"], r["reset_count"], r["id"], r["duration_seconds"]) == (t["ts_expired"], 1, a["id"], 3600)


def test_missing_and_malicious_ids(engine, data_dir):
    a = new_account(engine)
    run(engine.set_icon(a["id"], make_png()))
    for bad in ("0" * 32, "../accounts.json.enc", "..%2f..%2fetc", "x"):
        with pytest.raises(NotFound):
            engine.read_icon(bad)
    (data_dir / ASSETS_SUBDIR / f"{engine.accounts['accounts'][0]['icon_asset']['id']}.enc").unlink()
    with pytest.raises(NotFound):
        engine.read_icon(engine.accounts["accounts"][0]["icon_asset"]["id"])  # file lost: 404, not a crash


def test_restart_persistence_and_orphan_cleanup(data_dir, clock):
    e = make_engine(data_dir, clock)
    a = new_account(e)
    aid = run(e.set_icon(a["id"], WEBP))["icon_asset"]["id"]
    orphan = data_dir / ASSETS_SUBDIR / ("ab" * 16 + ".enc")
    orphan.write_bytes(b"left over from a crash")
    e2 = make_engine(data_dir, clock)
    assert e2.read_icon(aid) == (WEBP, "image/webp") and not orphan.exists()
    with pytest.raises(StoreError):
        make_engine(data_dir, clock, "another-valid-secret-xyz").read_icon(aid)


def test_rotation_and_backup_cover_assets(data_dir, clock):
    e = make_engine(data_dir, clock)
    a = new_account(e)
    aid = run(e.set_icon(a["id"], make_png()))["icon_asset"]["id"]
    import io
    import tarfile

    names = tarfile.open(fileobj=io.BytesIO(backup_blob(data_dir))).getnames()
    assert f"assets/icons/{aid}.enc" in names
    new = "brand-new-secret-value-123"
    rotate(data_dir, SECRET, new)
    assert make_engine(data_dir, clock, new).read_icon(aid)[1] == "image/png"
    with pytest.raises(StoreError):
        make_engine(data_dir, clock).read_icon(aid)
    assert not list(data_dir.rglob("*.new"))


# ---------------------------------------------------------------- HTTP
@pytest.fixture
def client(data_dir, clock):
    with TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock)) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        yield c


def test_http_upload_serve_headers_delete(client):
    acct = client.post("/api/accounts", json={"name": "Z", "duration_seconds": 3600}).json()
    r = client.put(f"/api/accounts/{acct['id']}/icon", content=SVG, headers={"Content-Type": "image/svg+xml"})
    assert r.status_code == 200
    url = r.json()["icon_url"]
    got = client.get(url)
    assert got.status_code == 200 and got.headers["content-type"] == "image/svg+xml"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert (
        "sandbox" in got.headers["content-security-policy"] and "script" not in got.headers["content-security-policy"]
    )
    assert got.headers["cache-control"].startswith("private")
    assert client.delete(f"/api/accounts/{acct['id']}/icon").json()["icon_url"] is None
    assert client.get(url).status_code == 404
    assert client.get("/assets/icons/..%2f..%2faccounts.json.enc").status_code == 404
    assert client.get("/assets/icons/nothex").status_code == 404


def test_http_rejects_oversize_bad_type_and_missing_csrf(client):
    acct = client.post("/api/accounts", json={"name": "Z", "duration_seconds": 3600}).json()
    path = f"/api/accounts/{acct['id']}/icon"
    big = client.put(path, content=b"\x00" * (MAX_UPLOAD_BYTES + 1))
    assert big.status_code == 413
    assert client.put(path, content=b"not an image").status_code == 422
    assert client.put("/api/accounts/nope/icon", content=make_png()).status_code == 404
    assert TestClient(client.app).put(path, content=make_png()).status_code == 403
    chunked = client.put(path, content=(c for c in [b"\x00" * 300_000, b"\x00" * 300_000]))  # no Content-Length
    assert chunked.status_code == 413
