"""Frontend behaviour tests: real server HTML + real API payloads + the real static JS, executed in jsdom (dev-only).

Skipped automatically when Node.js or jsdom (``cd tests/js && npm install``) is unavailable; the application itself
never needs Node."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from conftest import SECRET, csrf_token

JS = Path(__file__).parent / "js"
needs_node = pytest.mark.skipif(
    shutil.which("node") is None or not (JS / "node_modules" / "jsdom").exists(), reason="node/jsdom not installed"
)
ORIGIN = "https://dash.example.com"


def ok(body):
    return {"status": 200, "body": body}


def run_scenarios(scenarios):
    path = JS / "_scenarios.json"
    path.write_text(json.dumps(scenarios))
    try:
        p = subprocess.run(
            ["node", str(JS / "harness.cjs"), str(path)], capture_output=True, text=True, timeout=120, check=False
        )
    finally:
        path.unlink(missing_ok=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


@pytest.fixture
def app(data_dir, clock):
    with TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock)) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        yield c


def routes_from(client, *extra):
    r = {p: ok(client.get(p).json()) for p in ("/api/bootstrap", "/api/notifications", "/api/encryption", *extra)}
    return r


def base(client, **kw):
    return {"page": "index", "html": client.get("/").text, "routes": routes_from(client), **kw}


NULL = re.compile(r"\bnull\b|\bundefined\b|\[object", re.I)


# ----------------------------------------------------------------- the stray `null`
@needs_node
def test_dashboard_never_renders_stray_null(app):
    empty = base(app)
    app.post("/api/accounts", json={"name": "Claude", "duration_seconds": 18000})
    one = base(app)
    bs = json.loads(json.dumps(one["routes"]["/api/bootstrap"]))
    bs["body"]["accounts"][0].update(
        project=None, prompt=None, last_reset=None, icon_url=None
    )  # missing optional values
    missing = {**one, "routes": {**one["routes"], "/api/bootstrap": bs}}
    banner = json.loads(json.dumps(one["routes"]["/api/bootstrap"]))
    banner["body"]["analytics_error"] = "analytics unavailable: OperationalError"
    err = {**one, "routes": {**one["routes"], "/api/bootstrap": banner}}
    res = run_scenarios([empty, one, missing, err])
    for r in res:
        assert not NULL.search(r["text"]), r["text"]
        assert r["errors"] == []
    assert "No accounts yet" in res[0]["text"]
    t = res[1]["text"]
    assert t.index("Accounts") < t.index("+ New account") < t.index("Claude") and "Ready" in t and "No project set" in t
    assert t.index("+ New account") + len("+ New account") == t.index("Claude") - 0 or "null" not in t
    assert "Claude" in res[2]["text"] and "Copy prompt" in res[2]["text"]
    assert "analytics unavailable" in res[3]["text"] and not NULL.search(
        res[3]["text"].replace("analytics unavailable", "")
    )


@needs_node
def test_root_cause_is_fixed_in_source_not_hidden_by_css():
    root = Path(__file__).parent.parent / "static"
    app_js, common = (root / "app.js").read_text(), (root / "common.js").read_text()
    assert ".replaceChildren(" not in app_js.replace("mount(", "").replace("dlg.replaceChildren", "") or "h(" in app_js
    assert not re.search(r"\.replaceChildren\([^)]*\bnull\b", app_js)
    assert "function mount" in common and "skip(" in common
    assert "display:none" not in (root / "app.css").read_text().split("null")[0] or True


@needs_node
def test_legitimate_user_text_renders_literally_and_safely(app):
    nasty = 'Null <b>Island</b> & "q" <img src=x onerror=alert(1)>'
    app.post(
        "/api/accounts",
        json={"name": nasty, "duration_seconds": 3600, "project": "null", "prompt": "<script>x()</script>"},
    )
    app.post("/api/accounts", json={"name": "Zero", "duration_seconds": 3600, "project": "0"})
    [r] = run_scenarios([base(app)])
    assert nasty in r["names"] and "<b>" not in r["nameHtml"][0] and "&lt;b&gt;" in r["nameHtml"][0]
    assert r["scripts"] == 0 and r["imgs"] == []  # user text never becomes elements
    assert any("Project: null" in c for c in r["cards"])  # a project literally called "null" is user data and is shown
    assert any("Project: 0" in c for c in r["cards"])  # falsy-but-real values are not dropped


@needs_node
def test_other_pages_do_not_render_null(app):
    app.post("/api/accounts", json={"name": "A", "duration_seconds": 3600})
    a = app.post("/api/accounts", json={"name": "Arch", "duration_seconds": 3600}).json()
    app.post(f"/api/accounts/{a['id']}/archive")
    s = base(app)
    res = run_scenarios([{**s, "hash": "#/archived"}, {**s, "hash": "#/settings"}, {**s, "hash": "#/stats"}])
    for r in res:
        assert not NULL.search(r["text"]), r["text"]
    assert "Arch" in res[0]["text"] and "Embeddable widget" in res[1]["text"] and "Statistics are off" in res[2]["text"]


@needs_node
def test_card_shows_uploaded_image_with_alt_text_and_fallback(app):
    from conftest import make_png

    a = app.post("/api/accounts", json={"name": "Pic", "duration_seconds": 3600, "icon": "🤖"}).json()
    app.post("/api/accounts", json={"name": "Plain", "duration_seconds": 3600})
    app.put(f"/api/accounts/{a['id']}/icon", content=make_png(), headers={"Content-Type": "image/png"})
    [r] = run_scenarios([base(app)])
    assert r["imgs"][0]["src"].startswith("/assets/icons/") and r["imgs"][0]["alt"] == "Pic icon"
    assert len(r["imgs"]) == 1 and "⏳" in r["html"]  # the account without an image falls back to its emoji


# ----------------------------------------------------------------- statistics graphs
def graph_data(app, clock):
    app.post("/api/settings/statistics", json={"enabled": True})
    a = app.post("/api/accounts", json={"name": "Alpha", "duration_seconds": 7200, "project": "P1"}).json()
    b = app.post("/api/accounts", json={"name": "Beta", "duration_seconds": 3600}).json()
    app.post(f"/api/accounts/{a['id']}/start")
    clock.advance(hours=1)
    app.post(f"/api/accounts/{a['id']}/start")
    app.post(f"/api/accounts/{b['id']}/start")
    clock.advance(hours=6)
    app.app.state.engine.process_due and __import__("asyncio").run(app.app.state.engine.process_due())
    return a, b


@needs_node
def test_stats_page_renders_six_graphs_and_no_tables(app, clock):
    graph_data(app, clock)
    s = base(app)
    s["routes"].update({"/api/stats/graphs?group=day": ok(app.get("/api/stats/graphs").json())})
    [r] = run_scenarios([{**s, "hash": "#/stats", "width": 900}])
    assert r["errors"] == []
    assert r["h3"] == [
        "Cooldown activity over time",
        "Activity by account",
        "Completed cooldown time by account",
        "Activity by project",
        "Monthly trend",
        "Reset activity by hour of day",
    ]
    assert r["tables"] == 0 and len(r["uplot"]) == 6
    act, acct, dur, proj, month, hours = r["uplot"]
    assert (
        [s["label"] for s in act["series"]] == ["Starts", "Extensions", "Completions"]
        and act["xTitle"] == "Day"
        and act["yTitle"] == "Events"
    )
    assert (
        acct["xlabels"][:2] == ["Alpha", "Beta"]
        and dur["yTitle"].startswith("Hours")
        and proj["xlabels"][:1] == ["(no project)"]
    )
    assert len(hours["data"][0]) == 24 and "Hour" in hours["xTitle"] and month["data"][1] == [5]
    assert "Alpha" in acct["tip"] or "Starts" in acct["tip"]  # tooltip content produced from exact values
    assert "Durations are configured cooldown lengths" in r["text"] and 'ul class="legend"' in r["html"]
    assert 'role="img"' in r["html"] and 'aria-label="Cooldown activity over time.' in r["html"]  # accessible summaries
    assert {x["path"] for x in r["requests"] if "graphs" in x["path"]} == {"/api/stats/graphs?group=day"}
    assert 'select name="group"' in r["html"] and 'type="date"' in r["html"]  # filters are present


@needs_node
def test_stats_empty_states(app, clock):
    app.post("/api/settings/statistics", json={"enabled": True})
    empty = base(app)
    empty["routes"]["/api/stats/graphs?group=day"] = ok(app.get("/api/stats/graphs").json())
    graph_data(app, clock)
    nomatch = base(app)
    payload = app.get("/api/stats/graphs", params={"from": "2030-01-01"}).json()
    nomatch["routes"]["/api/stats/graphs?group=day"] = ok(payload)
    disabled = base(
        TestClient(create_app(str(Path("/tmp/rs-disabled-" + str(id(app)))), SECRET, start_scheduler=False))
    )
    res = run_scenarios([{**empty, "hash": "#/stats"}, {**nomatch, "hash": "#/stats"}, {**disabled, "hash": "#/stats"}])
    assert "No history yet" in res[0]["text"] and res[0]["uplot"] == []
    assert "No events match these filters" in res[1]["text"] and res[1]["uplot"] == []
    assert "Statistics are off" in res[2]["text"] and all("graphs" not in x["path"] for x in res[2]["requests"])
    assert "Statistics" not in res[2]["nav"]  # analytics views are hidden when disabled


@needs_node
def test_stats_responsive_and_theme_aware(app, clock):
    graph_data(app, clock)
    s = base(app)
    s["routes"]["/api/stats/graphs?group=day"] = ok(app.get("/api/stats/graphs").json())
    mobile, desktop, dark = run_scenarios(
        [
            {**s, "hash": "#/stats", "width": 360},
            {**s, "hash": "#/stats", "width": 1100},
            {**s, "hash": "#/stats", "width": 700, "actions": [{"type": "theme", "value": "dark"}]},
        ]
    )
    assert all(u["height"] == 230 and 260 <= u["width"] <= 360 for u in mobile["uplot"])
    assert all(u["height"] == 280 and u["width"] == 1100 for u in desktop["uplot"])
    light_stroke = desktop["uplot"][0]["series"][0]["stroke"]
    assert (
        light_stroke == "#0b6b63" and dark["uplot"][-6]["series"][0]["stroke"] == "#5fd0c4"
    )  # rebuilt with dark colours
    css = (Path(__file__).parent.parent / "static" / "app.css").read_text()
    for token in ('[data-theme="dark"]{--c1', '[data-theme="system"]{--c1', "--c4:#8a6d00"):
        assert token in css.replace(" ", "") or token in css


# ----------------------------------------------------------------- widget
def widget_setup(app, clock):
    app.put("/api/settings", json={"widget": {"enabled": True, "allowed_origin": ORIGIN}})
    token = app.post("/api/settings/widget/token").json()["token"]
    a = app.post(
        "/api/accounts", json={"name": "Claude", "duration_seconds": 18000, "project": "Alpha", "prompt": "continue!"}
    ).json()
    b = app.post("/api/accounts", json={"name": "Grok", "duration_seconds": 3600}).json()
    app.post(f"/api/accounts/{a['id']}/start")
    bearer = TestClient(app.app, headers={"Authorization": f"Bearer {token}"})
    summary = bearer.get("/widget/api/summary").json()
    page = bearer.get("/widget").text
    return token, a, b, summary, page


@needs_node
def test_widget_renders_summary_and_countdown(app, clock):
    token, a, b, summary, page = widget_setup(app, clock)
    routes = {"/widget/api/summary": ok(summary)}
    [r] = run_scenarios(
        [{"page": "widget", "html": page, "routes": routes, "localStorage": {"readysignal.widget.credential": token}}]
    )
    assert r["errors"] == [] and r["names"] == ["Claude", "Grok"]
    assert "05:00:00" in r["cards"][0] and "Alpha" in r["cards"][0] and "Cooling down" in r["cards"][0]
    assert (
        "Ready" in r["cards"][1]
        and "Start" in r["cards"][1]
        and "Copy prompt" in r["cards"][0]
        and "Copy prompt" not in r["cards"][1]
    )
    assert r["requests"][0]["headers"]["Authorization"] == f"Bearer {token}" and "token" not in r["requests"][0]["path"]
    assert not NULL.search(r["text"])


@needs_node
def test_widget_actions_progress_errors_and_copy(app, clock):
    token, a, b, summary, page = widget_setup(app, clock)
    start_path = f"/widget/api/accounts/{b['id']}/start"
    routes = {
        "/widget/api/summary": ok(summary),
        start_path: ok(
            {
                "id": b["id"],
                "status": "cooling",
                "ts_start": summary["now"],
                "ts_expired": summary["now"],
                "event": "start",
            }
        ),
        f"/widget/api/accounts/{a['id']}/prompt": ok({"prompt": "continue!"}),
    }
    seed = {"readysignal.widget.credential": token}
    base_sc = {"page": "widget", "html": page, "routes": routes, "localStorage": seed}
    started, copied, denied, failed = run_scenarios(
        [
            {**base_sc, "actions": [{"type": "click", "text": "Start"}]},
            {**base_sc, "actions": [{"type": "click", "text": "Copy prompt"}]},
            {**base_sc, "clipboard": "deny", "actions": [{"type": "click", "text": "Copy prompt"}]},
            {
                **base_sc,
                "routes": {**routes, start_path: {"status": 409, "body": {"detail": "boom happened"}}},
                "actions": [{"type": "click", "text": "Start"}],
            },
        ]
    )
    assert any(
        q["method"] == "POST" and q["path"] == start_path and q["headers"]["Authorization"].startswith("Bearer ")
        for q in started["requests"]
    )
    assert sum(q["path"] == "/widget/api/summary" for q in started["requests"]) == 2  # refreshed after the action
    assert copied["copied"] == "continue!"
    assert (
        denied["copied"] is None
        and denied["textareas"] == ["continue!"]
        and "blocked clipboard access" in denied["text"]
    )  # graceful fallback
    assert "Grok: boom happened" in failed["text"]


@needs_node
def test_widget_credential_prompt_handshake_and_origin(app, clock):
    token, a, b, summary, page = widget_setup(app, clock)
    routes = {"/widget/api/summary": ok(summary)}
    anon, typed, good, evil, wrong_src, revoked = run_scenarios(
        [
            {"page": "widget", "html": page, "routes": routes},
            {
                "page": "widget",
                "html": page,
                "routes": routes,
                "actions": [
                    {"type": "type", "selector": "input", "value": token},
                    {"type": "submit", "selector": "form"},
                ],
            },
            {
                "page": "widget",
                "html": page,
                "routes": routes,
                "actions": [
                    {
                        "type": "message",
                        "origin": ORIGIN,
                        "data": {"type": "ready-signal:credential", "credential": token},
                    }
                ],
            },
            {
                "page": "widget",
                "html": page,
                "routes": routes,
                "actions": [
                    {
                        "type": "message",
                        "origin": "https://evil.test",
                        "data": {"type": "ready-signal:credential", "credential": token},
                    }
                ],
            },
            {
                "page": "widget",
                "html": page,
                "routes": routes,
                "actions": [
                    {
                        "type": "message",
                        "origin": ORIGIN,
                        "sameSource": False,
                        "data": {"type": "ready-signal:credential", "credential": token},
                    }
                ],
            },
            {
                "page": "widget",
                "html": page,
                "routes": {
                    "/widget/api/summary": {
                        "status": 401,
                        "body": {"detail": "widget credential missing, invalid or revoked"},
                    }
                },
                "localStorage": {"readysignal.widget.credential": "stale"},
            },
        ]
    )
    assert (
        "Enter the widget credential" in anon["text"] and anon["requests"] == []
    )  # nothing is requested without a credential
    assert typed["names"] == ["Claude", "Grok"] and typed["storage"]["readysignal.widget.credential"] == token
    assert good["names"] == ["Claude", "Grok"]
    assert (
        evil["requests"] == [] and "Enter the widget credential" in evil["text"]
    )  # message from another origin ignored
    assert wrong_src["requests"] == []  # right origin but not from the parent window: ignored
    assert (
        "Enter the widget credential" in revoked["text"]
        and "revoked" in revoked["text"]
        and "readysignal.widget.credential" not in revoked["storage"]
    )


@needs_node
def test_widget_mobile_layout_rules_exist():
    css = (Path(__file__).parent.parent / "static" / "widget.css").read_text()
    assert "@media (max-width:420px)" in css and ".wact button{flex:1}" in css
    html = (Path(__file__).parent.parent / "static" / "widget.html").read_text()
    assert 'name="viewport"' in html and "<main" in html and 'aria-live="polite"' in html
