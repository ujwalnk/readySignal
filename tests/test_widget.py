import logging

import pytest
from fastapi.testclient import TestClient

from app.engine import Engine, ValidationFailed
from app.main import create_app

from conftest import SECRET, csrf_token, make_engine, new_account, run, setup_slack

ORIGIN = "https://dash.example.com"


@pytest.fixture
def admin(data_dir, clock):
    with TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock)) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        yield c


@pytest.fixture
def widget(admin):
    """Browser-like widget client: no cookies, no CSRF token — just the Bearer credential."""
    admin.put("/api/settings", json={"widget": {"enabled": True, "allowed_origin": ORIGIN}})
    token = admin.post("/api/settings/widget/token").json()["token"]
    c = TestClient(admin.app)
    c.headers["Authorization"] = f"Bearer {token}"
    c.token = token
    return c


def mk(admin, name="Claude", **kw):
    return admin.post(
        "/api/accounts",
        json={"name": name, "duration_seconds": 18000, "project": "Alpha", "prompt": "SECRET-PROMPT-TEXT", **kw},
    ).json()


def test_disabled_by_default(admin):
    assert admin.get("/widget").status_code == 404
    assert admin.get("/widget/api/summary").status_code == 401


def test_enable_requires_valid_origin(admin):
    assert admin.put("/api/settings", json={"widget": {"enabled": True}}).status_code == 422
    for bad in (
        "*",
        "http://",
        "dash.example.com",
        "https://a.com/path",
        "javascript:alert(1)",
        'https://a.com"; x',
        "https://a b.com",
    ):
        assert admin.put("/api/settings", json={"widget": {"allowed_origin": bad}}).status_code == 422, bad
    ok = admin.put("/api/settings", json={"widget": {"enabled": True, "allowed_origin": ORIGIN + "/"}})
    assert ok.status_code == 200 and ok.json()["widget"]["allowed_origin"] == ORIGIN


def test_frame_ancestors_only_for_authorised_parent(admin, widget):
    page = widget.get("/widget")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert f"frame-ancestors {ORIGIN}" in csp and "*" not in csp
    assert "x-frame-options" not in page.headers and page.headers["cache-control"] == "no-store"
    assert f'data-origin="{ORIGIN}"' in page.text
    main = admin.get("/")
    assert (
        main.headers["x-frame-options"] == "DENY"
        and "frame-ancestors 'none'" in main.headers["content-security-policy"]
    )
    admin.put("/api/settings", json={"widget": {"enabled": False}})
    assert widget.get("/widget").status_code == 404


def test_authorisation(admin, widget):
    mk(admin)
    assert widget.get("/widget/api/summary").status_code == 200
    anon = TestClient(admin.app)
    assert anon.get("/widget/api/summary").status_code == 401
    assert anon.get("/widget/api/summary", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert anon.get("/widget/api/summary", headers={"Authorization": "Basic abc"}).status_code == 401
    assert anon.get(f"/widget/api/summary?token={widget.token}").status_code == 401  # credentials never travel in URLs
    assert (
        anon.get(
            "/widget/api/summary", headers={"Authorization": f"Bearer {widget.token}", "Origin": "https://evil.test"}
        ).status_code
        == 200
    )  # Origin is not authentication either way
    assert anon.get("/widget/api/summary").headers.get("www-authenticate") == "Bearer"
    admin.put("/api/settings", json={"widget": {"enabled": False}})
    assert widget.get("/widget/api/summary").status_code == 401


def test_rotate_and_revoke(admin, widget):
    old = widget.token
    new = admin.post("/api/settings/widget/token").json()["token"]
    assert new != old
    assert (
        TestClient(admin.app).get("/widget/api/summary", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    )
    assert (
        TestClient(admin.app).get("/widget/api/summary", headers={"Authorization": f"Bearer {new}"}).status_code == 200
    )
    assert admin.delete("/api/settings/widget/token").json() == {"status": "revoked"}
    assert (
        TestClient(admin.app).get("/widget/api/summary", headers={"Authorization": f"Bearer {new}"}).status_code == 401
    )


def test_credential_is_never_stored_or_leaked(admin, widget, data_dir, caplog):
    caplog.set_level(logging.DEBUG)
    mk(admin)
    widget.get("/widget/api/summary")
    engine = admin.app.state.engine
    assert widget.token not in str(engine.config) and engine.config["widget"]["token_hash"]
    for path in ("/api/bootstrap", "/api/encryption", "/api/notifications", "/widget"):
        assert widget.token not in admin.get(path).text
    assert (
        "token_hash" not in admin.get("/api/bootstrap").text
        and admin.get("/api/bootstrap").json()["config"]["widget"]["token_configured"]
    )
    assert widget.token not in caplog.text
    for p in data_dir.rglob("*"):
        if p.is_file():
            assert widget.token.encode() not in p.read_bytes()


def test_summary_is_minimal(admin, widget):
    admin.put(
        "/api/settings",
        json={"slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/services/T/B/WIDGETLEAK"}},
    )
    mk(admin)
    raw = widget.get("/widget/api/summary")
    body = raw.json()
    assert set(body) == {"now", "show_project", "allow_prompt_copy", "accounts"}
    assert set(body["accounts"][0]) == {
        "id",
        "name",
        "icon",
        "icon_url",
        "status",
        "ts_start",
        "ts_expired",
        "duration_seconds",
        "has_prompt",
        "project",
    }
    for forbidden in ("SECRET-PROMPT-TEXT", "WIDGETLEAK", SECRET, "webhook", "template", "/data", "scrypt", "token"):
        assert forbidden not in raw.text, forbidden


def test_account_selection_and_flags(admin, widget):
    a, b, c = mk(admin, "A"), mk(admin, "B"), mk(admin, "C")
    admin.post(f"/api/accounts/{c['id']}/archive")
    assert [x["name"] for x in widget.get("/widget/api/summary").json()["accounts"]] == [
        "A",
        "B",
    ]  # default: all active
    admin.put("/api/settings", json={"widget": {"account_ids": [b["id"]]}})
    assert [x["name"] for x in widget.get("/widget/api/summary").json()["accounts"]] == ["B"]
    assert widget.post(f"/widget/api/accounts/{a['id']}/start").status_code == 404
    assert widget.get(f"/widget/api/accounts/{a['id']}/prompt").status_code == 404
    assert admin.put("/api/settings", json={"widget": {"account_ids": ["nope"]}}).status_code == 422
    admin.put("/api/settings", json={"widget": {"account_ids": [], "show_project": False, "allow_prompt_copy": False}})
    s = widget.get("/widget/api/summary").json()
    assert "project" not in s["accounts"][0] and s["accounts"][0]["has_prompt"] is False
    assert widget.get(f"/widget/api/accounts/{a['id']}/prompt").status_code == 404


def test_start_extend_use_the_shared_timer_service(admin, widget, clock):
    a = mk(admin)
    admin.post("/api/settings/statistics", json={"enabled": True})
    r1 = widget.post(f"/widget/api/accounts/{a['id']}/start").json()
    assert (
        r1["event"] == "start"
        and r1["status"] == "cooling"
        and set(r1) == {"id", "status", "ts_start", "ts_expired", "event"}
    )
    clock.advance(hours=2)
    r2 = widget.post(f"/widget/api/accounts/{a['id']}/start").json()
    assert r2["event"] == "extend"
    engine = admin.app.state.engine
    from app.models import parse_iso

    assert (parse_iso(r2["ts_expired"]) - clock.now).total_seconds() == 3 * 3600 + 5 * 3600
    assert engine.state["timers"][a["id"]]["reset_count"] == 2
    counts = {r["event_type"]: r["n"] for r in engine.stats()["monthly_counts"]}
    assert counts == {"start": 1, "extend": 1}  # analytics recorded through the same path
    assert widget.post("/widget/api/accounts/nope/start").status_code == 404
    clock.advance(hours=9)
    assert widget.get("/widget/api/summary").json()["accounts"][0]["status"] == "available"


def test_widget_start_still_queues_notification_on_expiry(data_dir, clock, curl):
    e = make_engine(data_dir, clock)
    run(setup_slack(e))
    a = new_account(e, "N", prompt="continue please")
    run(e.update_settings({"widget": {"enabled": True, "allowed_origin": ORIGIN}}))
    run(e.widget_start(a["id"]))
    clock.advance(hours=2)
    run(e.process_due())
    run(e.deliver_due())
    assert len(curl.calls) == 1 and b"continue please" in curl.calls[0]["body"]


def test_prompt_endpoint(admin, widget):
    a = mk(admin)
    assert widget.get(f"/widget/api/accounts/{a['id']}/prompt").json() == {"prompt": "SECRET-PROMPT-TEXT"}
    assert TestClient(admin.app).get(f"/widget/api/accounts/{a['id']}/prompt").status_code == 401


def test_no_cors_headers_granted(widget):
    r = widget.get("/widget/api/summary", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in r.headers
    assert widget.options(
        "/widget/api/summary", headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"}
    ).status_code in (400, 405)


def test_widget_assets_and_branding(widget):
    html = widget.get("/widget").text
    assert "Ready Signal" in html and "widget.js" in html and "unpkg" not in html and "cdn" not in html.lower()
    js = widget.get("/static/widget.js").text
    assert "ev.origin !== ORIGIN" in js and "Authorization" in js and "localStorage" in js


def test_validation_helper_direct(data_dir, clock):
    e = make_engine(data_dir, clock)
    with pytest.raises(ValidationFailed):
        run(e.update_settings({"widget": {"enabled": "yes"}}))
    assert isinstance(e, Engine)
