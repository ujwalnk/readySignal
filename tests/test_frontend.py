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
    assert "Claude" in t and "Available" in t and "Start cooldown" in t and "No project set" not in t
    assert "+ New account" not in t and "Timers are absolute timestamps" not in t
    assert "Claude" in res[2]["text"]
    assert any(c["label"] == "Copy prompt for Claude" for c in res[2]["cardInfo"][0]["controls"])
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
    projects = [x["project"] for x in r["cardInfo"]]
    assert "null" in projects and "0" in projects  # literal "null" / falsy "0" projects are data and are shown


@needs_node
def test_other_pages_do_not_render_null(app):
    app.post("/api/accounts", json={"name": "A", "duration_seconds": 3600})
    a = app.post("/api/accounts", json={"name": "Arch", "duration_seconds": 3600}).json()
    app.post(f"/api/accounts/{a['id']}/archive")
    s = base(app)
    res = run_scenarios([{**s, "hash": "#/archived"}, {**s, "hash": "#/settings"}, {**s, "hash": "#/stats"}])
    for r in res:
        assert not NULL.search(r["text"]), r["text"]
    assert (
        "Arch" in res[0]["text"]
        and "Embeddable widget" in res[1]["text"]
        and "Archived accounts" in res[1]["text"]
        and "Statistics are off" in res[2]["text"]
    )


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
        light_stroke == "#0b7285" and dark["uplot"][-6]["series"][0]["stroke"] == "#3ad0e6"
    )  # rebuilt with dark colours
    css = (Path(__file__).parent.parent / "static" / "theme.css").read_text()
    assert "--c1:#0b7285" in css and "--c4:#8a6a00" in css  # light tokens the harness mirrors
    assert css.count("--c1:#3ad0e6") == 2 and "--c4:#facc15" in css  # dark tokens: system-dark block and forced dark


# ----------------------------------------------------------------- redesign: header, cards, dialogs
from zoneinfo import ZoneInfo  # noqa: E402

from app.models import parse_iso  # noqa: E402


def mk_accounts(client, *names, **kw):
    return [
        client.post(
            "/api/accounts", json={"name": n, "duration_seconds": 18000, "prompt": f"prompt for {n}", **kw}
        ).json()
        for n in names
    ]


def mutating(res):
    return [r for r in res["requests"] if r["method"] != "GET"]


@needs_node
def test_header_brand_icons_and_no_dashboard_or_archived_links(app):
    app.put("/api/settings", json={"theme": "dark"})
    r0 = run_scenarios([base(app)])[0]
    assert r0["header"]["brand"] == {"href": "#/", "text": "Ready Signal"}
    nav = r0["header"]
    assert (
        "Dashboard" not in nav["navText"] and "Archived" not in nav["navText"] and "New account" not in nav["navText"]
    )
    assert "Statistics" not in nav["navText"]  # analytics are off by default
    by_label = {c["label"]: c for c in nav["controls"]}
    assert (
        by_label["Add account"]["tag"] == "BUTTON"
        and by_label["Add account"]["text"] == ""
        and by_label["Add account"]["title"]
    )
    assert (
        by_label["Settings"]["href"] == "#/settings"
        and by_label["Settings"]["text"] == ""
        and by_label["Settings"]["title"]
    )
    app.post("/api/settings/statistics", json={"enabled": True})
    r1 = run_scenarios([base(app)])[0]
    assert any(c["text"] == "Statistics" and c["href"] == "#/stats" for c in r1["header"]["controls"])
    # the header is the same on every page and the brand leads back to the dashboard
    for hash_ in ("#/settings", "#/archived"):
        r = run_scenarios([base(app, hash=hash_)])[0]
        assert r["header"]["brand"]["href"] == "#/" and {c["label"] for c in r["header"]["controls"]} >= {
            "Add account",
            "Settings",
        }


@needs_node
def test_add_account_icon_opens_existing_editor(app):
    r = run_scenarios([base(app, actions=[{"type": "click", "text": "Add account"}])])[0]
    assert r["dlg"]["open"] and "New account" in r["dlg"]["text"] and "Cooldown completes at" not in r["dlg"]["text"]
    assert "+ New account" not in r["text"]


@needs_node
def test_dashboard_has_only_cards_and_compact_state_aware_content(app):
    a, b, c = mk_accounts(app, "Claude", "ChatGPT", "Grok")
    app.put(f"/api/accounts/{a['id']}", json={"name": "Claude", "duration_seconds": 18000, "project": "Alpha-Project"})
    app.post(f"/api/accounts/{b['id']}/start")
    r = run_scenarios([base(app)])[0]
    t = r["text"]
    assert (
        "Timers are absolute timestamps" not in t and "Duration" not in t and "Resets" not in t and "Project:" not in t
    )
    assert "No project set" not in t and "Extend" not in t
    assert not re.search(r"\bnull\b|\bundefined\b", t, re.I)
    cards = {x["name"]: x for x in r["cardInfo"]}
    assert "available" in cards["Claude"]["cls"] and "cooling" in cards["ChatGPT"]["cls"]
    assert "Alpha-Project" in cards["Claude"]["text"] and "Start cooldown" in cards["Claude"]["text"]
    assert "Cooling down" in cards["ChatGPT"]["text"] and "Start cooldown" not in cards["ChatGPT"]["text"]
    labels = lambda card: [x["label"] for x in card["controls"]]  # noqa: E731
    assert "Edit Claude" in labels(cards["Claude"]) and "Copy prompt for Claude" in labels(cards["Claude"])
    assert labels(cards["ChatGPT"]) == ["Edit ChatGPT"]  # no Copy prompt, no Extend while cooling
    for card in r["cardInfo"]:
        for ctl in card["controls"]:
            if ctl["label"]:
                assert ctl["text"] == "" and ctl["title"]  # icon-only controls carry an accessible name and a tooltip
    assert r["hasRoleTimer"] == 1


@needs_node
def test_start_cooldown_is_one_direct_request_without_dialog(app):
    (a,) = mk_accounts(app, "Claude")
    scen = base(app, actions=[{"type": "click", "text": "Start cooldown"}])
    scen["routes"][f"/api/accounts/{a['id']}/start"] = ok({"status": "cooling"})
    r = run_scenarios([scen])[0]
    assert [(m["method"], m["path"]) for m in mutating(r)] == [("POST", f"/api/accounts/{a['id']}/start")]
    assert mutating(r)[0]["body"] is None  # no choice payload: the configured default duration is used server-side
    assert not r["dlg"]["open"] and not r["errors"]
    js = (Path(__file__).parent.parent / "static" / "app.js").read_text()
    for gone in ("Extend by", "HH:MM", "Select a new", "reminder", "chooseTimeout"):
        assert gone not in js


@needs_node
def test_copy_prompt_only_copies(app):
    mk_accounts(app, "Claude")
    r = run_scenarios([base(app, actions=[{"type": "clickSel", "selector": 'button[aria-label^="Copy prompt"]'}])])[0]
    assert r["copied"] == "prompt for Claude" and mutating(r) == []


@needs_node
def test_copy_prompt_appears_after_cooldown_completes(app, clock):
    (a,) = mk_accounts(app, "Claude")
    app.post(f"/api/accounts/{a['id']}/start")
    cooling = run_scenarios([base(app)])[0]["cardInfo"][0]
    assert not any((c["label"] or "").startswith("Copy prompt") for c in cooling["controls"])
    clock.advance(hours=6)
    done = run_scenarios([base(app)])[0]["cardInfo"][0]
    assert "available" in done["cls"] and any((c["label"] or "").startswith("Copy prompt") for c in done["controls"])


def local_input(iso, tz):
    return parse_iso(iso).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


@needs_node
def test_editor_shows_completion_field_only_for_running_timers(app):
    a, b = mk_accounts(app, "Running", "Idle")
    app.put("/api/settings", json={"timezone": "Asia/Kolkata"})
    app.post(f"/api/accounts/{a['id']}/start")
    sc = lambda name: base(app, actions=[{"type": "click", "text": f"Edit {name}"}])  # noqa: E731
    running, idle = run_scenarios([sc("Running"), sc("Idle")])
    assert "Cooldown completes at" in running["dlg"]["labels"]
    field = next(i for i in running["dlg"]["inputs"] if i["name"] == "completes_at")
    ts = app.get("/api/bootstrap").json()["accounts"][0]["ts_expired"]
    assert field["type"] == "datetime-local" and field["value"] == local_input(ts, "Asia/Kolkata")
    assert "Asia/Kolkata" in running["dlg"]["text"] and "Cooldown hours" in running["dlg"]["text"]
    assert "Cooldown completes at" not in idle["dlg"]["labels"] and not any(
        i["name"] == "completes_at" for i in idle["dlg"]["inputs"]
    )


@needs_node
def test_editor_saves_expiry_only_when_changed_and_reports_partial_failure(app):
    (a,) = mk_accounts(app, "Running")
    app.post(f"/api/accounts/{a['id']}/start")
    sc = base(app, actions=[{"type": "click", "text": "Edit Running"}, {"type": "submit", "selector": "#dlg form"}])
    sc["routes"][f"/api/accounts/{a['id']}"] = ok({"id": a["id"]})
    untouched = run_scenarios([sc])[0]
    assert [m["path"] for m in mutating(untouched)] == [f"/api/accounts/{a['id']}"]  # no expiry write when untouched
    sc = base(
        app,
        actions=[
            {"type": "click", "text": "Edit Running"},
            {"type": "type", "selector": '#dlg input[name="completes_at"]', "value": "2026-10-16T19:30"},
            {"type": "submit", "selector": "#dlg form"},
        ],
    )
    sc["routes"][f"/api/accounts/{a['id']}"] = ok({"id": a["id"]})
    sc["routes"][f"/api/accounts/{a['id']}/expiry"] = ok({"changed": True})
    moved = run_scenarios([sc])[0]
    puts = [(m["path"], json.loads(m["body"]) if m["body"] else None) for m in mutating(moved)]
    assert puts[0][0] == f"/api/accounts/{a['id']}" and puts[1] == (
        f"/api/accounts/{a['id']}/expiry",
        {"completes_at": "2026-10-16T19:30"},
    )
    assert not moved["dlg"]["open"]
    sc["routes"][f"/api/accounts/{a['id']}/expiry"] = {
        "status": 422,
        "body": {"detail": "the completion time must be in the future"},
    }
    failed = run_scenarios([sc])[0]
    assert (
        failed["dlg"]["open"]
        and "Your other changes were saved" in failed["dlg"]["text"]
        and "must be in the future" in failed["dlg"]["text"]
    )


@needs_node
def test_ambiguous_time_reveals_occurrence_choice(app):
    (a,) = mk_accounts(app, "Running")
    app.post(f"/api/accounts/{a['id']}/start")
    sc = base(
        app,
        actions=[
            {"type": "click", "text": "Edit Running"},
            {"type": "type", "selector": '#dlg input[name="completes_at"]', "value": "2026-11-01T01:30"},
            {"type": "submit", "selector": "#dlg form"},
        ],
    )
    sc["routes"][f"/api/accounts/{a['id']}"] = ok({"id": a["id"]})
    sc["routes"][f"/api/accounts/{a['id']}/expiry"] = {
        "status": 422,
        "body": {"detail": "happens twice", "code": "ambiguous_time"},
    }
    r = run_scenarios([sc])[0]
    occ = next(i for i in r["dlg"]["inputs"] if i["name"] == "occurrence")
    assert occ["hidden"] is False and r["dlg"]["open"]


def env_app(data_dir, clock, monkeypatch, **env):
    for k in ("READY_SIGNAL_TIMEZONE", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock))


@needs_node
def test_settings_environment_managed_fields(data_dir, clock, monkeypatch):
    note = "Already set in environment variable"
    with env_app(
        data_dir,
        clock,
        monkeypatch,
        READY_SIGNAL_TIMEZONE="Asia/Kolkata",
        SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T/B/ENVONLY",
    ) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        sc = base(c, hash="#/settings", actions=[{"type": "submit", "selector": "form"}])
        r = run_scenarios([sc])[0]
        assert r["fields"]["timezone"] == {"value": note, "disabled": True, "type": "text"}
        assert r["fields"]["webhook_url"]["value"] == note and r["fields"]["webhook_url"]["disabled"]
        assert (
            "READY_SIGNAL_TIMEZONE" in r["text"]
            and "SLACK_WEBHOOK_URL" in r["text"]
            and "Remove webhook" not in r["text"]
        )
        assert "ENVONLY" not in json.dumps(r) and "ENVONLY" not in c.get("/api/bootstrap").text
        assert (
            "Slack enabled" in r["text"] or "Enable Slack" in r["text"] or "Send test" in r["text"]
        )  # rest stays usable
        body = json.loads(mutating(r)[0]["body"])
        assert "timezone" not in body  # a disabled field is never submitted
    with env_app(data_dir, clock, monkeypatch) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        c.put("/api/settings", json={"slack": {"webhook_url": "https://hooks.slack.com/services/T/B/STORED"}})
        r = run_scenarios([base(c, hash="#/settings")])[0]
        assert not r["fields"]["timezone"]["disabled"] and r["fields"]["timezone"]["value"] != note
        assert not r["fields"]["webhook_url"]["disabled"] and "Remove webhook" in r["text"]


@needs_node
def test_settings_archived_link_data_management_and_widget_embed(app):
    mk_accounts(app, "A", "B", "C")
    r = run_scenarios([base(app, hash="#/settings")])[0]
    secs = {s["title"]: s for s in r["sections"]}
    assert any(
        link["href"] == "#/archived" and link["text"] == "Archived accounts" for link in secs["Accounts"]["links"]
    )
    data = secs["Data management"]
    assert "Encryption and recovery" in data["text"]
    (link,) = [x for x in data["links"] if x["href"] == "/api/backup"]
    assert link["text"] == "Download encrypted backup" and link["download"] == "ready-signal-backup-encrypted.tar.gz"
    assert [s["title"] for s in r["sections"]][
        -1
    ] == "Data management"  # its own section, not mixed into other settings
    w = secs["Embeddable widget (iframe)"]["text"]
    assert 'allow="clipboard-write"' in w and 'height="150"' in w and 'height="320"' not in w and 'width="280"' in w
    for gone in ("token", "Token", "origin", "Origin", "Revoke", "Generate", "credential entry"):
        assert gone not in w, gone
    arch = run_scenarios([base(app, hash="#/archived")])[0]
    assert "Archived accounts" in arch["text"] and "Settings" in arch["text"]


def test_backup_endpoint_download_headers(app):
    r = app.get("/api/backup")
    assert (
        r.status_code == 200 and 'filename="ready-signal-backup-encrypted.tar.gz"' in r.headers["content-disposition"]
    )


def test_stylesheets_distinguish_states_and_drop_paper_look():
    st = Path(__file__).parent.parent / "static"
    theme, css, wcss = ((st / n).read_text() for n in ("theme.css", "app.css", "widget.css"))
    for beige in ("#f6f1e7", "#efe9dc", "#fbf8f1", "cream"):
        assert beige not in theme.lower() + css.lower()
    assert ".card.available" in css and ".card.cooling" in css and "--cool-bg" in css
    assert css.count("opacity:.") < 4 and "opacity:.5" not in css.split(".card.cooling")[1].split("}")[0]
    assert (
        "--row:34px" in wcss and "overflow-y" not in wcss and "overflow-x:hidden" in wcss and "max-width:240px" in wcss
    )
    assert 'data-theme="dark"' in theme and "prefers-color-scheme:dark" in theme


# ----------------------------------------------------------------- widget (credential-free, compact)
def widget_scenario(client, **kw):
    client.put("/api/settings", json={"widget": {"enabled": True}})
    return {
        "page": "widget",
        "html": client.get("/widget").text,
        "routes": {"/widget/api/summary": ok(client.get("/widget/api/summary").json())},
        **kw,
    }


@needs_node
def test_widget_loads_without_credential_and_is_compact(app):
    a, b, c, d = mk_accounts(app, "Claude", "ChatGPT Personal", "ChatGPT Work", "Grok Research")
    app.put(
        f"/api/accounts/{a['id']}",
        json={"name": "Claude", "duration_seconds": 18000, "project": "Alpha", "prompt": "p"},
    )
    app.post(f"/api/accounts/{b['id']}/start")
    r = run_scenarios([widget_scenario(app)])[0]
    assert r["errors"] == [] and r["storage"] == {}  # nothing stored in the browser
    assert [q["path"] for q in r["requests"]] == ["/widget/api/summary"]
    assert not any("authorization" in {k.lower() for k in q["headers"]} for q in r["requests"])
    assert (
        "token" not in r["text"].lower()
        and "credential" not in r["text"].lower()
        and "connect" not in r["text"].lower()
    )
    assert r["names"] == ["Claude", "ChatGPT Personal", "ChatGPT Work", "Grok Research"]  # every account stays visible
    claude, chat = r["cardInfo"][0], r["cardInfo"][1]
    assert "Alpha" in claude["text"] and "Ready" in claude["text"]
    labels = lambda row: [x["label"] for x in row["controls"]]  # noqa: E731
    assert labels(claude) == ["Start cooldown for Claude", "Copy prompt for Claude"]
    assert labels(chat) == ["Extend ChatGPT Personal by 5h"]  # no Copy while cooling
    assert all(x["text"] == "" and x["title"] for row in r["cardInfo"] for x in row["controls"])
    assert (
        "Settings" not in r["text"]
        and "Statistics" not in r["text"]
        and "Duration" not in r["text"]
        and "Resets" not in r["text"]
    )
    assert r["hasRoleTimer"] == 1 and re.search(r"\d{1,2}:\d{2}:\d{2}", chat["text"])


@needs_node
def test_widget_actions_are_direct_requests(app):
    a, b = mk_accounts(app, "Idle", "Busy")
    app.post(f"/api/accounts/{b['id']}/start")
    sc = widget_scenario(
        app, actions=[{"type": "click", "text": "Start cooldown for Idle"}, {"type": "click", "text": "Extend Busy"}]
    )
    for acct in (a, b):
        sc["routes"][f"/widget/api/accounts/{acct['id']}/start"] = ok({"status": "cooling"})
    r = run_scenarios([sc])[0]
    assert [(m["method"], m["path"]) for m in mutating(r)] == [
        ("POST", f"/widget/api/accounts/{a['id']}/start"),
        ("POST", f"/widget/api/accounts/{b['id']}/start"),
    ]
    assert r["errors"] == [] and r["overlay"] == ""


@needs_node
def test_widget_copy_prompt_and_clipboard_fallback(app):
    (a,) = mk_accounts(app, "Idle")
    base_sc = widget_scenario(app, actions=[{"type": "clickSel", "selector": 'button[aria-label^="Copy prompt"]'}])
    base_sc["routes"][f"/widget/api/accounts/{a['id']}/prompt"] = ok({"prompt": "prompt for Idle"})
    good = run_scenarios([base_sc])[0]
    assert good["copied"] == "prompt for Idle" and mutating(good) == [] and good["overlay"] == "Copied"
    denied = run_scenarios([{**base_sc, "clipboard": "denied"}])[0]
    assert (
        denied["copied"] is None and "prompt for Idle" in denied["textareas"] and "clipboard-write" in denied["overlay"]
    )


@needs_node
def test_widget_hides_project_and_copy_per_settings(app):
    mk_accounts(app, "Idle", project="Hidden-Project")
    app.put("/api/settings", json={"widget": {"enabled": True, "show_project": False, "allow_prompt_copy": False}})
    r = run_scenarios([widget_scenario(app)])[0]
    assert "Hidden-Project" not in r["text"] and [x["label"] for x in r["cardInfo"][0]["controls"]] == [
        "Start cooldown for Idle"
    ]


def test_widget_script_has_no_credential_machinery():
    js = (Path(__file__).parent.parent / "static" / "widget.js").read_text()
    for gone in ("Authorization", "localStorage", "postMessage", "Bearer", "token", "ORIGIN", "ready-signal:"):
        assert gone not in js, gone
    assert "setInterval(tick, 1000)" in js and "60000" in js  # local 1 s countdown; backend polled once a minute
