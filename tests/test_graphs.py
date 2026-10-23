import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.analytics import Analytics
from app.engine import NotFound, ValidationFailed
from app.main import create_app

from conftest import SECRET, csrf_token, new_account, run


@pytest.fixture
def loaded(engine, clock):
    """Two accounts, activity over three days and two projects (one renamed mid-flight)."""
    run(engine.set_statistics(True))
    a = new_account(engine, "Alpha", project="P1", prompt="first prompt")
    b = new_account(engine, "Beta")
    run(
        engine.update_account(
            a["id"], {"name": "Alpha", "duration_seconds": 7200, "project": "P1", "prompt": "first prompt"}
        )
    )
    run(engine.start(a["id"]))  # day 1 08:00 start (P1)
    clock.advance(hours=1)
    run(engine.start(a["id"]))  # 09:00 extend
    run(
        engine.update_account(
            a["id"], {"name": "Alpha", "duration_seconds": 7200, "project": "P2", "prompt": "second prompt"}
        )
    )
    clock.advance(hours=4)
    run(engine.process_due())  # complete (snapshot project P2)
    run(engine.start(b["id"]))  # 13:00 start, no project
    clock.advance(days=1, hours=-5)  # day 2 08:00
    run(engine.start(a["id"]))  # start (P2)
    clock.advance(days=32)
    run(engine.process_due())  # completes a (day 34) and b
    return engine, a, b


def test_activity_buckets_and_totals(loaded):
    e, a, b = loaded
    g = e.graphs()
    assert g["has_history"] and g["matching_events"] == sum(
        g["activity"][k][i] for k in ("start", "extend", "complete") for i in range(len(g["activity"]["buckets"]))
    )
    act = dict(
        zip(g["activity"]["buckets"], zip(g["activity"]["start"], g["activity"]["extend"], g["activity"]["complete"]))
    )
    assert act["2026-10-15"] == (2, 1, 2)  # complete events are stamped at their expiry time
    assert (
        sum(g["activity"]["start"]) == 3 and sum(g["activity"]["extend"]) == 1 and sum(g["activity"]["complete"]) == 3
    )
    m = e.graphs(group="month")
    assert m["activity"]["buckets"] == ["2026-10"] and m["activity"]["start"] == [3]
    w = e.graphs(group="week")
    assert w["activity"]["buckets"][0] == "2026-10-12"  # Monday of 15 Oct 2026


def test_by_account_duration_project_monthly_resets(loaded):
    e, a, b = loaded
    g = e.graphs()
    assert g["by_account"]["labels"] == ["Alpha", "Beta"]
    assert (
        g["by_account"]["start"] == [2, 1]
        and g["by_account"]["extend"] == [1, 0]
        and g["by_account"]["complete"] == [2, 1]
    )
    d = dict(zip(g["duration_by_account"]["labels"], g["duration_by_account"]["seconds"]))
    assert d["Beta"] == 3600 and d["Alpha"] == 4 * 3600 + 7200  # first span incl. extension + second cooldown
    assert dict(zip(g["by_project"]["labels"], g["by_project"]["starts"])) == {
        "(no project)": 1,
        "P1": 1,
        "P2": 1,
    }  # snapshots, not current value
    assert g["monthly"]["buckets"] == ["2026-10"] and g["monthly"]["completions"] == [3]
    assert (
        sum(g["reset_hours"]["start"]) == 3
        and sum(g["reset_hours"]["extend"]) == 1
        and g["reset_hours"]["start"][8] == 2
    )


def test_filters(loaded):
    e, a, b = loaded
    assert e.graphs(accounts=[b["id"]])["by_account"]["labels"] == ["Beta"]
    assert e.graphs(accounts=[a["id"], b["id"]])["matching_events"] == e.graphs()["matching_events"]
    assert set(e.graphs(project="P2")["by_project"]["labels"]) == {"P2"}
    assert e.graphs(project="(no project)")["by_account"]["labels"] == ["Beta"]
    one_day = e.graphs(date_from="2026-10-15", date_to="2026-10-15")
    assert one_day["activity"]["buckets"] == ["2026-10-15"] and sum(one_day["activity"]["start"]) == 2
    assert e.graphs(date_from="2026-10-16", date_to="2026-10-16")["activity"]["buckets"] == ["2026-10-16"]
    none = e.graphs(date_from="2030-01-01")
    assert none["has_history"] and none["matching_events"] == 0 and none["activity"]["buckets"] == []
    assert none["options"]["accounts"] and "P1" in none["options"]["projects"]
    for bad in ({"group": "year"}, {"date_from": "15/10/2026"}, {"date_to": "x"}):
        with pytest.raises(ValidationFailed):
            e.graphs(**bad)


def test_filter_values_are_bound_not_interpolated(loaded):
    e, a, b = loaded
    g = e.graphs(project="x' OR '1'='1", accounts=["a') OR 1=1 --"])
    assert g["matching_events"] == 0 and e.graphs()["matching_events"] > 0


def test_timezone_drives_day_and_hour_buckets(engine, clock):
    run(engine.update_settings({"timezone": "Asia/Kolkata"}))
    run(engine.set_statistics(True))
    a = new_account(engine, "T")
    clock.now = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)  # 01:30 next day in IST
    run(engine.start(a["id"]))
    g = engine.graphs()
    assert g["activity"]["buckets"] == ["2026-10-16"] and g["reset_hours"]["start"][1] == 1
    assert engine.graphs(date_from="2026-10-16", date_to="2026-10-16")["matching_events"] == 1
    assert engine.graphs(date_from="2026-10-15", date_to="2026-10-15")["matching_events"] == 0


def test_empty_history_and_disabled(engine, data_dir, monkeypatch):
    with pytest.raises(NotFound):
        engine.graphs()  # disabled
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sqlite touched")))
    with pytest.raises(NotFound):
        engine.graphs()
    monkeypatch.undo()
    run(engine.set_statistics(True))
    g = engine.graphs()
    assert g["has_history"] is False and g["matching_events"] == 0 and g["activity"]["buckets"] == []
    assert g["by_account"]["labels"] == [] and g["duration_by_account"]["labels"] == []


def test_disable_then_reenable_does_not_restore_history(loaded, data_dir):
    e, a, b = loaded
    assert e.graphs()["has_history"]
    run(e.set_statistics(False, confirm=True))
    assert not list(data_dir.glob("analytics.sqlite3*"))
    with pytest.raises(NotFound):
        e.graphs()
    run(e.set_statistics(True))
    assert e.graphs()["has_history"] is False


def test_prompt_snapshots_are_stored_per_event(loaded, data_dir):
    with sqlite3.connect(data_dir / "analytics.sqlite3") as c:
        rows = c.execute("SELECT event_type, project, prompt FROM events ORDER BY event_ts, event_type").fetchall()
    assert ("start", "P1", "first prompt") in rows and ("start", "P2", "second prompt") in rows
    assert ("extend", "P1", "first prompt") in rows  # later edits never rewrote this row


def test_sqlite_v1_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "analytics.sqlite3"
    c = sqlite3.connect(path)
    c.executescript(
        """CREATE TABLE accounts (account_id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE events (event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(account_id),
          event_type TEXT NOT NULL, event_ts TEXT NOT NULL, cooldown_id TEXT NOT NULL, ts_start TEXT NOT NULL, ts_expired TEXT NOT NULL,
          previous_ts_expired TEXT, configured_seconds INTEGER NOT NULL, project TEXT, reset_count INTEGER);
        INSERT INTO accounts VALUES ('a','Old','x',0);
        INSERT INTO events VALUES ('e1','a','start','2026-01-02T03:04:05Z','c','2026-01-02T03:04:05Z','2026-01-02T08:04:05Z',NULL,18000,'Proj',1);
        PRAGMA user_version = 1;"""
    )
    c.commit()
    c.close()
    an = Analytics(path)
    an.init()
    g = an.graphs()
    assert g["matching_events"] == 1 and g["by_project"]["labels"] == ["Proj"]
    with sqlite3.connect(path) as c2:
        assert c2.execute("PRAGMA user_version").fetchone()[0] == 2
        assert c2.execute("SELECT prompt FROM events").fetchone() == (None,)


def test_http_graph_endpoint(data_dir, clock):
    with TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock)) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        assert c.get("/api/stats/graphs").status_code == 404  # statistics disabled
        c.post("/api/settings/statistics", json={"enabled": True})
        a = c.post("/api/accounts", json={"name": "H", "duration_seconds": 3600, "project": "X"}).json()
        c.post(f"/api/accounts/{a['id']}/start")
        r = c.get(
            "/api/stats/graphs",
            params={"from": "2026-10-15", "to": "2026-10-15", "group": "week", "account": [a["id"]], "project": "X"},
        )
        assert r.status_code == 200 and r.json()["activity"]["start"] == [1] and r.json()["filters"]["group"] == "week"
        assert c.get("/api/stats/graphs", params={"group": "decade"}).status_code == 422
        assert c.get("/api/stats/graphs", params={"from": "nope"}).status_code == 422


def test_monthly_trend_spans_months(engine, clock):
    run(engine.set_statistics(True))
    a = new_account(engine, "M")
    for _ in range(3):
        run(engine.start(a["id"]))
        clock.advance(days=20)
        run(engine.process_due())
    g = engine.graphs()
    assert (
        g["monthly"]["buckets"] == ["2026-10", "2026-11", "2026-12"][: len(g["monthly"]["buckets"])]
        and len(g["monthly"]["buckets"]) >= 2
    )
    assert sum(g["monthly"]["completions"]) == 3 and sum(g["monthly"]["seconds"]) == 3 * 3600
    assert engine.graphs(group="month")["activity"]["buckets"] == g["monthly"]["buckets"]
