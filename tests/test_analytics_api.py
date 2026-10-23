import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.analytics import REPORTS
from app.crypto import SecretError
from app.main import create_app
from app.store import StoreError

from conftest import SECRET, csrf_token, make_engine, run, setup_slack


def test_no_sqlite_access_while_disabled(engine, clock, monkeypatch, data_dir):
    def boom(*a, **k):
        raise AssertionError("sqlite touched while statistics disabled")

    monkeypatch.setattr(sqlite3, "connect", boom)

    async def main():
        a = await engine.create_account({"name": "A", "duration_seconds": 3600, "project": "p"})
        await engine.start(a["id"])
        clock.advance(hours=2)
        await engine.process_due()
        with pytest.raises(Exception):
            engine.stats()

    run(main())
    assert not (data_dir / "analytics.sqlite3").exists()


def test_events_reports_and_disable_semantics(engine, clock, data_dir, curl):
    async def main():
        await setup_slack(engine)
        await engine.set_statistics(True)
        assert (data_dir / "analytics.sqlite3").exists()
        a = await engine.create_account(
            {"name": "Keep", "icon": "🤖", "duration_seconds": 7200, "project": "Proj", "prompt": "continue"}
        )
        await engine.start(a["id"])
        clock.advance(hours=1)
        await engine.start(a["id"])  # extend
        clock.advance(hours=4)
        await engine.process_due()
        rep = engine.stats()
        counts = {(r["event_type"]): r["n"] for r in rep["monthly_counts"]}
        assert counts == {"start": 1, "extend": 1, "complete": 1}
        assert rep["monthly_duration_by_account"][0]["seconds"] == 4 * 3600  # span incl. extension
        assert rep["monthly_duration_by_project"][0]["project"] == "Proj"
        assert rep["reset_counts"][0]["resets"] == 2
        await engine.process_due()  # idempotent completion event
        assert sum(r["n"] for r in engine.stats()["monthly_counts"]) == 3
        b = await engine.start(a["id"])
        await engine.set_archived(a["id"], True)
        assert engine.stats()["archived_history"][0]["events"] == 4
        await engine.set_archived(a["id"], False)

        with pytest.raises(Exception):
            await engine.set_statistics(False)  # confirmation required
        r = await engine.set_statistics(False, confirm=True)
        assert r["errors"] == [] and not list(data_dir.glob("analytics.sqlite3*"))
        acct = engine.accounts["accounts"][0]
        assert (acct["name"], acct["icon"], acct["duration_seconds"], acct["project"], acct["prompt"]) == (
            "Keep",
            "🤖",
            7200,
            "",
            "",
        )
        assert engine.view(acct)["ts_expired"] == b["ts_expired"]  # active timer retained
        assert engine.notifs["jobs"][0]["snapshot"]["prompt"] == ""
        await engine.set_statistics(True)  # history is not reconstructed
        assert engine.stats()["monthly_counts"] == []

    run(main())


def test_deletion_failure_reported_and_retried_on_restart(engine, clock, data_dir, monkeypatch):
    from pathlib import Path

    async def main():
        await engine.set_statistics(True)
        real = Path.unlink

        def failing(self, *a, **k):
            if self.name.startswith("analytics.sqlite3"):
                raise PermissionError(13, "Permission denied")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", failing)
        r = await engine.set_statistics(False, confirm=True)
        assert r["errors"] and engine.config["analytics_purge_pending"]
        monkeypatch.undo()

    run(main())
    e2 = make_engine(data_dir, engine.clock)  # startup finishes the purge
    assert not list(data_dir.glob("analytics.sqlite3*")) and not e2.config["analytics_purge_pending"]


def test_analytics_failure_does_not_block_timers(engine, clock, data_dir):
    async def main():
        await engine.set_statistics(True)
        (data_dir / "analytics.sqlite3").unlink()
        (data_dir / "analytics.sqlite3").mkdir()  # make sqlite unusable
        a = await engine.create_account({"name": "A", "duration_seconds": 3600})
        r = await engine.start(a["id"])
        assert r["status"] == "cooling" and r["warnings"] and "not recorded" in r["warnings"][0]

    run(main())


def test_reports_are_plain_sql(engine, data_dir):
    run(engine.set_statistics(True))
    with sqlite3.connect(data_dir / "analytics.sqlite3") as c:
        for sql in REPORTS.values():
            c.execute(sql).fetchall()
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2


# ---------------------------------------------------------------- HTTP layer
@pytest.fixture
def client(data_dir, clock):
    with TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock)) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        yield c


def test_app_refuses_to_start_without_secret_or_with_bad_data(data_dir, monkeypatch):
    monkeypatch.delenv("SECRET", raising=False)
    with pytest.raises(SecretError):
        create_app(str(data_dir), None)
    create_app(str(data_dir), SECRET)
    with pytest.raises(StoreError):
        create_app(str(data_dir), "x" * 24)


def test_csrf_enforced(client):
    ok = client.post("/api/accounts", json={"name": "A", "duration_seconds": 3600})
    assert ok.status_code == 201
    bad = client.post("/api/accounts", json={"name": "B", "duration_seconds": 3600}, headers={"X-CSRF-Token": "nope"})
    assert bad.status_code == 403
    missing = TestClient(client.app).post("/api/accounts", json={"name": "B", "duration_seconds": 3600})
    assert missing.status_code == 403
    cross = client.post(
        "/api/accounts", json={"name": "C", "duration_seconds": 3600}, headers={"Origin": "http://evil.example"}
    )
    assert cross.status_code == 403


def test_account_crud_and_errors(client):
    r = client.post(
        "/api/accounts", json={"name": "<b>x</b>", "icon": "🤖", "duration_seconds": 3600, "prompt": "p"}
    ).json()
    assert (
        client.put(f"/api/accounts/{r['id']}", json={"name": "N", "duration_seconds": 7200}).json()["duration_seconds"]
        == 7200
    )
    assert client.post(f"/api/accounts/{r['id']}/start").json()["status"] == "cooling"
    assert client.post("/api/accounts/nope/start").status_code == 404
    assert client.post("/api/accounts", json={"name": "N", "duration_seconds": 3600}).status_code == 409
    assert client.post("/api/accounts", json={"name": "Z", "duration_seconds": 5}).status_code == 422
    assert client.post(f"/api/accounts/{r['id']}/archive").json()["archived"]
    assert client.get("/api/stats").status_code == 404


def test_settings_theme_persist_and_no_flash(client, data_dir, clock):
    assert client.put("/api/settings", json={"theme": "dark"}).json()["theme"] == "dark"
    assert 'data-theme="dark"' in client.get("/").text  # server-rendered: no flash of wrong theme
    assert client.put("/api/settings", json={"theme": "neon"}).status_code == 422
    assert make_engine(data_dir, clock).config["theme"] == "dark"


def test_secret_never_in_api_responses(client):
    client.put(
        "/api/settings",
        json={"slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/services/A/B/LEAKCHECK99"}},
    )
    for path in ("/api/bootstrap", "/api/encryption", "/api/notifications"):
        assert "LEAKCHECK" not in client.get(path).text and SECRET not in client.get(path).text


def test_template_preview_endpoint_and_static_and_health(client):
    ok = client.post("/api/settings/template/preview", json={"template": '{"text":"hi $account"}'})
    assert ok.json()["text"] == "hi Example — Work"
    assert client.post("/api/settings/template/preview", json={"template": "{"}).status_code == 422
    assert client.get("/healthz").json()["status"] == "ok"
    css = client.get("/static/app.css").text
    assert "max-width:560px" in css and "prefers-color-scheme" in client.get("/static/theme.css").text
    assert client.get("/api/backup").headers["content-type"] == "application/gzip"


def test_secret_file_support(data_dir, tmp_path, monkeypatch):
    f = tmp_path / "secret"
    f.write_text(SECRET + "\n")
    monkeypatch.delenv("SECRET", raising=False)
    monkeypatch.setenv("SECRET_FILE", str(f))
    assert create_app(str(data_dir)).state.engine.keyring
    monkeypatch.setenv("SECRET_FILE", str(tmp_path / "missing"))
    with pytest.raises(SecretError):
        create_app(str(data_dir))
