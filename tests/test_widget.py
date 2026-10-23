import pytest
from fastapi.testclient import TestClient

from app.engine import Engine, ValidationFailed
from app.main import create_app

from conftest import SECRET, csrf_token, make_engine, new_account, run, setup_slack


@pytest.fixture
def admin(data_dir, clock):
    with TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock)) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        yield c


@pytest.fixture
def widget(admin):
    """Browser-like widget client: no cookies, no CSRF token, no credential of any kind."""
    admin.put("/api/settings", json={"widget": {"enabled": True}})
    return TestClient(admin.app)


def mk(admin, name="Claude", **kw):
    return admin.post(
        "/api/accounts",
        json={"name": name, "duration_seconds": 18000, "project": "Alpha", "prompt": "SECRET-PROMPT-TEXT", **kw},
    ).json()


def test_disabled_by_default(admin):
    assert admin.get("/widget").status_code == 404
    assert admin.get("/widget/api/summary").status_code == 404
    assert admin.post("/widget/api/accounts/x/start").status_code == 404


def test_enable_needs_nothing_else(admin):
    r = admin.put("/api/settings", json={"widget": {"enabled": True}})
    assert r.status_code == 200
    w = r.json()["widget"]
    assert set(w) == {"enabled", "account_ids", "show_project", "allow_prompt_copy"}  # no token/origin fields
    assert TestClient(admin.app).get("/widget/api/summary").status_code == 200  # no credential, no cookie


def test_removed_credential_surface(admin, widget):
    assert admin.post("/api/settings/widget/token").status_code in (404, 405)
    assert admin.delete("/api/settings/widget/token").status_code in (404, 405)
    r = admin.put("/api/settings", json={"widget": {"allowed_origin": "https://dash.example.com"}})
    assert r.status_code == 200 and "allowed_origin" not in r.json()["widget"]  # unknown legacy key is ignored
    assert "allowed_origin" not in admin.app.state.engine.config["widget"]
    assert widget.get("/widget/api/summary", headers={"Authorization": "Bearer junk"}).status_code == 200  # ignored


def test_widget_can_be_framed_main_pages_cannot(admin, widget):
    page = widget.get("/widget")
    assert page.status_code == 200
    assert "x-frame-options" not in page.headers
    assert "frame-ancestors" not in page.headers["content-security-policy"]  # no restriction on the parent
    assert page.headers["cache-control"] == "no-store"
    assert "data-origin" not in page.text and "__ORIGIN__" not in page.text
    main = admin.get("/")
    assert (
        main.headers["x-frame-options"] == "DENY"
        and "frame-ancestors 'none'" in main.headers["content-security-policy"]
    )
    api = admin.get("/api/bootstrap")
    assert api.headers["x-frame-options"] == "DENY"
    admin.put("/api/settings", json={"widget": {"enabled": False}})
    assert widget.get("/widget").status_code == 404 and widget.get("/widget/api/summary").status_code == 404


def test_old_widget_config_is_migrated(data_dir, clock):

    e = make_engine(data_dir, clock)
    cfg = e.files["config"].load()
    cfg["schema_version"] = 2
    cfg["widget"].update(
        allowed_origin="https://dash.example.com", token_hash="ab" * 32, token_created_at="2026-01-01T00:00:00Z"
    )
    cfg["widget"]["enabled"] = True
    cfg["widget"]["show_project"] = False
    e.files["config"].save(cfg)
    e2 = make_engine(data_dir, clock)
    assert e2.config["schema_version"] == 3
    assert e2.config["widget"] == {"enabled": True, "account_ids": [], "show_project": False, "allow_prompt_copy": True}


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
    for forbidden in (
        "SECRET-PROMPT-TEXT",
        "WIDGETLEAK",
        SECRET,
        "webhook",
        "template",
        "/data",
        "scrypt",
        "token",
        "origin",
    ):
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
    run(e.update_settings({"widget": {"enabled": True}}))
    run(e.widget_start(a["id"]))
    clock.advance(hours=2)
    run(e.process_due())
    run(e.deliver_due())
    assert len(curl.calls) == 1 and b"continue please" in curl.calls[0]["body"]


def test_prompt_endpoint(admin, widget):
    a = mk(admin)
    assert widget.get(f"/widget/api/accounts/{a['id']}/prompt").json() == {"prompt": "SECRET-PROMPT-TEXT"}
    admin.put("/api/settings", json={"widget": {"enabled": False}})
    assert widget.get(f"/widget/api/accounts/{a['id']}/prompt").status_code == 404


def test_no_cors_headers_granted(widget):
    r = widget.get("/widget/api/summary", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in r.headers
    assert widget.options(
        "/widget/api/summary", headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"}
    ).status_code in (400, 405)


def test_widget_assets_are_credential_free_and_compact(widget):
    html = widget.get("/widget").text
    assert "widget.js" in html and "widget.css" in html and "unpkg" not in html and "cdn" not in html.lower()
    assert (
        "Authorization" not in widget.get("/static/widget.js").text
        and "localStorage" not in widget.get("/static/widget.js").text
    )
    js = widget.get("/static/widget.js").text
    assert "postMessage" not in js and "origin" not in js.lower().replace("origin:", "")
    assert "320" not in html


def test_validation_helper_direct(data_dir, clock):
    e = make_engine(data_dir, clock)
    with pytest.raises(ValidationFailed):
        run(e.update_settings({"widget": {"enabled": "yes"}}))
    assert isinstance(e, Engine)
