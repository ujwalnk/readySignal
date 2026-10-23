"""Editing the completion time of a running cooldown (PUT /api/accounts/{id}/expiry)."""

import io
import tarfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.engine import AmbiguousTime, Conflict, ValidationFailed, resolve_local_time
from app.main import create_app
from app.models import parse_iso

from conftest import SECRET, csrf_token, make_engine, new_account, run, setup_slack


@pytest.fixture
def admin(data_dir, clock):
    with TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock)) as c:
        c.headers["X-CSRF-Token"] = csrf_token(c)
        yield c


def started(admin, **kw):
    a = admin.post("/api/accounts", json={"name": "Claude", "duration_seconds": 18000, **kw}).json()
    admin.post(f"/api/accounts/{a['id']}/start")
    return a


def test_moves_expiry_earlier_and_later_without_touching_other_fields(admin, clock):
    a = started(admin, project="P", prompt="keep me")
    eng = admin.app.state.engine
    admin.put("/api/settings", json={"timezone": "UTC"})
    before = dict(eng.state["timers"][a["id"]])
    target = (clock.now.replace(microsecond=0)).isoformat()[:10] + "T23:30"
    r = admin.put(f"/api/accounts/{a['id']}/expiry", json={"completes_at": target})
    assert r.status_code == 200 and r.json()["changed"] and r.json()["status"] == "cooling"
    t = eng.state["timers"][a["id"]]
    assert t["ts_expired"].endswith("T23:30:00Z") or "23:30:00" in t["ts_expired"]
    assert t["expiry_id"] != before["expiry_id"] and t["cooldown_id"] == before["cooldown_id"]
    assert t["ts_start"] == before["ts_start"] and t["reset_count"] == before["reset_count"]
    acct = eng.accounts["accounts"][0]
    assert acct["duration_seconds"] == 18000 and acct["project"] == "P" and acct["prompt"] == "keep me"
    earlier = (clock.now.replace(microsecond=0) + __import__("datetime").timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    assert admin.put(f"/api/accounts/{a['id']}/expiry", json={"completes_at": earlier}).json()["changed"]


def test_metadata_edits_never_move_expiry_and_expiry_never_changes_duration(admin, clock):
    a = started(admin)
    eng = admin.app.state.engine
    exp = eng.state["timers"][a["id"]]["ts_expired"]
    admin.put(
        f"/api/accounts/{a['id']}", json={"name": "Claude", "duration_seconds": 3600, "project": "Z", "prompt": "q"}
    )
    assert eng.state["timers"][a["id"]]["ts_expired"] == exp
    admin.put("/api/settings", json={"timezone": "UTC"})
    admin.put(f"/api/accounts/{a['id']}/expiry", json={"completes_at": "2099-01-01T00:00"}) if False else None
    new = (clock.now.replace(microsecond=0) + __import__("datetime").timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    admin.put(f"/api/accounts/{a['id']}/expiry", json={"completes_at": new})
    assert eng.accounts["accounts"][0]["duration_seconds"] == 3600


def test_unchanged_value_is_a_noop(admin):
    a = started(admin)
    eng = admin.app.state.engine
    admin.put("/api/settings", json={"timezone": "UTC"})
    exp = eng.state["timers"][a["id"]]["ts_expired"]
    r = admin.put(f"/api/accounts/{a['id']}/expiry", json={"completes_at": exp})
    assert r.status_code == 200 and r.json()["changed"] is False
    assert eng.state["timers"][a["id"]]["expiry_id"] == eng.state["timers"][a["id"]]["expiry_id"]


def test_validation_and_state_errors(admin, clock):
    a = started(admin)
    p = f"/api/accounts/{a['id']}/expiry"
    assert admin.put("/api/accounts/nope/expiry", json={"completes_at": "2099-01-01T00:00"}).status_code == 404
    for bad in ("", "tomorrow", "2026-13-45T99:00"):
        assert admin.put(p, json={"completes_at": bad}).status_code == 422, bad
    assert admin.put(p, json={}).status_code == 422
    assert admin.put(p, json={"completes_at": "2000-01-01T00:00"}).status_code == 422  # past
    assert "complete_now" in admin.put(p, json={"completes_at": "2000-01-01T00:00"}).json()["detail"]
    assert admin.put(p, json={"completes_at": "2099-01-01T00:00"}).status_code == 422  # beyond 30 days
    assert admin.put(p, json={"completes_at": "2099-01-01T00:00", "occurrence": "x"}).status_code == 422
    idle = admin.post("/api/accounts", json={"name": "Idle", "duration_seconds": 60}).json()
    assert admin.put(f"/api/accounts/{idle['id']}/expiry", json={"completes_at": "2099-01-01T00:00"}).status_code == 409


def test_explicit_complete_now_path_notifies_once(data_dir, clock, curl):
    e = make_engine(data_dir, clock)
    run(setup_slack(e))
    a = new_account(e, "N", prompt="go on")
    run(e.start(a["id"]))
    run(e.set_expiry(a["id"], complete_now=True))
    assert e.state["timers"][a["id"]]["status"] == "available"
    run(e.process_due())
    run(e.deliver_due())
    clock.advance(hours=10)
    run(e.process_due())
    run(e.deliver_due())
    assert len(curl.calls) == 1 and len(e.notifs["jobs"]) == 1


def test_superseded_deadline_never_notifies_and_new_one_notifies_once(data_dir, clock, curl):
    e = make_engine(data_dir, clock)
    run(setup_slack(e))
    run(e.update_settings({"timezone": "UTC"}))
    a = new_account(e, "N", duration_seconds=3600)
    run(e.start(a["id"]))
    original = clock.now + __import__("datetime").timedelta(hours=1)
    later = (clock.now + __import__("datetime").timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    run(e.set_expiry(a["id"], later))
    clock.advance(hours=2)  # the ORIGINAL deadline has passed
    assert clock.now > original
    run(e.process_due())
    run(e.deliver_due())
    assert curl.calls == [] and e.state["timers"][a["id"]]["status"] == "cooling"
    clock.advance(hours=2)  # the edited deadline passes
    run(e.process_due())
    run(e.deliver_due())
    run(e.process_due())
    run(e.deliver_due())
    assert len(curl.calls) == 1 and len(e.notifs["jobs"]) == 1


def test_shorter_expiry_completes_early_exactly_once(data_dir, clock, curl):
    e = make_engine(data_dir, clock)
    run(setup_slack(e))
    run(e.update_settings({"timezone": "UTC"}))
    a = new_account(e, "N", duration_seconds=5 * 3600)
    run(e.start(a["id"]))
    run(e.set_expiry(a["id"], (clock.now + __import__("datetime").timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")))
    clock.advance(hours=1)
    for _ in range(3):
        run(e.process_due())
        run(e.deliver_due())
    assert len(curl.calls) == 1
    clock.advance(hours=6)
    run(e.process_due())
    run(e.deliver_due())
    assert len(curl.calls) == 1


def test_scheduler_deadline_recalculated_and_woken(data_dir, clock):
    e = make_engine(data_dir, clock)
    run(e.update_settings({"timezone": "UTC"}))
    a = new_account(e, "N", duration_seconds=5 * 3600)
    run(e.start(a["id"]))
    assert e.next_deadline() == clock.now + __import__("datetime").timedelta(hours=5)
    e.wake.clear()
    target = (clock.now + __import__("datetime").timedelta(hours=1)).replace(second=0, microsecond=0)
    run(e.set_expiry(a["id"], target.strftime("%Y-%m-%dT%H:%M")))
    assert e.wake.is_set() and e.next_deadline() == target


def test_restart_recovery_after_edit(data_dir, clock, curl):
    e = make_engine(data_dir, clock)
    run(setup_slack(e))
    run(e.update_settings({"timezone": "UTC"}))
    a = new_account(e, "N", duration_seconds=3600)
    run(e.start(a["id"]))
    target = (clock.now + __import__("datetime").timedelta(hours=4)).replace(second=0, microsecond=0)
    run(e.set_expiry(a["id"], target.strftime("%Y-%m-%dT%H:%M")))
    e2 = make_engine(data_dir, clock)  # "restart": state comes back from the encrypted JSON
    assert parse_iso(e2.state["timers"][a["id"]]["ts_expired"]) == target
    clock.advance(hours=1, minutes=30)
    run(e2.process_due())
    run(e2.deliver_due())
    assert curl.calls == []
    clock.advance(hours=3)
    run(e2.process_due())
    run(e2.deliver_due())
    assert len(curl.calls) == 1


def test_expiry_is_interpreted_in_effective_zone(data_dir, clock):
    e = make_engine(data_dir, clock)
    run(e.update_settings({"timezone": "Asia/Kolkata"}))
    a = new_account(e, "N", duration_seconds=3600)
    run(e.start(a["id"]))
    day = (clock.now + __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
    run(e.set_expiry(a["id"], f"{day}T18:00"))
    got = parse_iso(e.state["timers"][a["id"]]["ts_expired"])
    assert got.strftime("%H:%M") == "12:30" and got.tzinfo is not None  # 18:00 IST == 12:30 UTC
    run(e.set_expiry(a["id"], f"{day}T18:00:00+00:00"))  # explicit offsets are honoured
    assert parse_iso(e.state["timers"][a["id"]]["ts_expired"]).strftime("%H:%M") == "18:00"


def test_dst_gap_and_overlap_are_explicit():
    # US spring-forward 2026-03-08 02:30 does not exist; fall-back 2026-11-01 01:30 happens twice.
    with pytest.raises(ValidationFailed, match="does not exist"):
        resolve_local_time("2026-03-08T02:30", "America/New_York")
    with pytest.raises(AmbiguousTime):
        resolve_local_time("2026-11-01T01:30", "America/New_York")
    first = resolve_local_time("2026-11-01T01:30", "America/New_York", "earlier")
    second = resolve_local_time("2026-11-01T01:30", "America/New_York", "later")
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc) and second == datetime(
        2026, 11, 1, 6, 30, tzinfo=timezone.utc
    )
    assert resolve_local_time("2026-03-08T03:30", "America/New_York") == datetime(
        2026, 3, 8, 7, 30, tzinfo=timezone.utc
    )
    assert resolve_local_time("2026-11-01T01:30-05:00", "America/New_York") == second  # explicit offset resolves it


def test_ambiguous_time_api_code(admin, clock):
    a = started(admin)
    admin.put("/api/settings", json={"timezone": "America/New_York"})
    # force the clock near the overlap so the target is within 30 days
    clock.now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    admin.post(f"/api/accounts/{a['id']}/start")
    r = admin.put(f"/api/accounts/{a['id']}/expiry", json={"completes_at": "2026-11-01T01:30"})
    assert r.status_code == 422 and r.json()["code"] == "ambiguous_time"
    ok = admin.put(f"/api/accounts/{a['id']}/expiry", json={"completes_at": "2026-11-01T01:30", "occurrence": "later"})
    assert ok.status_code == 200 and "2026-11-01T06:30:00" in ok.json()["ts_expired"]


def test_archived_and_conflict_types():
    assert issubclass(Conflict, Exception) and issubclass(AmbiguousTime, ValidationFailed)


def test_backup_unaffected_by_expiry_edit(admin):
    a = started(admin)
    admin.put("/api/settings", json={"timezone": "UTC"})
    r = admin.get("/api/backup")
    names = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz").getnames()
    assert names and all(n.endswith(".enc") for n in names) and a["id"]
