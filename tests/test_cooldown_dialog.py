"""Start-cooldown dialog semantics (options A/B), reminders (option C) and superseded expiries."""

from datetime import timedelta

import pytest

from app import notify
from app.engine import PartialFailure, ValidationFailed

from conftest import make_engine, new_account, run, setup_slack


def acct(e, **kw):
    return new_account(e, "A", project="Old", prompt="old prompt", **kw)


def timer(e, a):
    return e.state["timers"][a["id"]]


def test_default_duration_starts_from_now_and_empty_fields_keep_values(engine, clock):
    a = acct(engine)
    r = run(engine.begin_cooldown(a["id"], "default", project="", prompt="  "))
    assert r["event"] == "start" and r["project"] == "Old" and r["prompt"] == "old prompt"
    assert timer(engine, a)["ts_expired"] == "2026-10-15T09:00:00Z"
    assert engine._acct(a["id"])["duration_seconds"] == 3600


def test_default_duration_extends_active_and_updates_nonempty_metadata(engine, clock):
    a = acct(engine)
    run(engine.begin_cooldown(a["id"], "default"))
    first = timer(engine, a)["expiry_id"]
    clock.advance(minutes=10)
    r = run(engine.begin_cooldown(a["id"], "default", project="New", prompt=""))
    assert r["event"] == "extend" and r["project"] == "New" and r["prompt"] == "old prompt"
    t = timer(engine, a)
    assert t["ts_expired"] == "2026-10-15T10:00:00Z" and t["expiry_id"] != first and t["reset_count"] == 2
    assert len(engine.accounts["accounts"]) == 1  # same permanent account


def test_set_completion_time_uses_zone_and_keeps_duration(engine, clock):
    run(engine.update_settings({"timezone": "Asia/Kolkata"}))
    a = acct(engine)
    run(engine.begin_cooldown(a["id"], "at", completes_at="2026-10-15T20:00"))  # 14:30Z
    assert timer(engine, a)["ts_expired"] == "2026-10-15T14:30:00Z"
    assert engine._acct(a["id"])["duration_seconds"] == 3600


def test_past_or_far_completion_time_rejected_without_side_effects(engine, clock):
    a = acct(engine)
    with pytest.raises(ValidationFailed, match="already passed"):
        run(engine.begin_cooldown(a["id"], "at", completes_at="2026-10-15T07:00Z", project="Changed"))
    assert a["id"] not in engine.state["timers"] and engine._acct(a["id"])["project"] == "Old"
    with pytest.raises(ValidationFailed):
        run(engine.begin_cooldown(a["id"], "at", completes_at="2027-10-15T07:00Z"))


def test_error_messages_do_not_mention_dst(engine):
    a = acct(engine)
    run(engine.update_settings({"timezone": "America/New_York"}))
    for text in ("2026-03-08T02:30", "2026-11-01T01:30"):
        with pytest.raises(ValidationFailed) as ei:
            run(engine.begin_cooldown(a["id"], "at", completes_at=text))
        assert "daylight" not in str(ei.value).lower()


def test_superseded_expiry_does_not_notify(engine, clock, curl):
    run(setup_slack(engine))
    a = acct(engine)
    run(engine.begin_cooldown(a["id"], "default"))
    run(engine.begin_cooldown(a["id"], "at", completes_at="2026-10-15T12:00Z"))
    clock.advance(hours=5)  # past both the original (09:00) and the new (12:00) expiry
    run(engine.process_due())
    run(engine.deliver_due())
    assert len(engine.notifs["jobs"]) == 1 and len(curl.calls) == 1


def test_partial_failure_is_reported_honestly(engine, monkeypatch):
    a = acct(engine)

    def boom(*_a, **_k):
        raise ValidationFailed("boom")

    monkeypatch.setattr(engine, "_start_locked", boom)
    with pytest.raises(PartialFailure, match="saved"):
        run(engine.begin_cooldown(a["id"], "default", project="New"))
    assert engine._acct(a["id"])["project"] == "New"


# ---------------------------------------------------------------- reminders
def test_reminder_requires_valid_time_and_slack(engine):
    a = acct(engine)
    with pytest.raises(ValidationFailed, match="HH:MM"):
        run(engine.schedule_reminder(a["id"], "25:00"))
    with pytest.raises(ValidationFailed, match="Slack"):
        run(engine.schedule_reminder(a["id"], "18:30"))


def test_reminder_today_or_next_day_and_touches_nothing_else(engine, clock, curl):
    run(setup_slack(engine))
    a = acct(engine)
    before = (engine.accounts, engine.state)
    r = run(engine.schedule_reminder(a["id"], "18:30"))  # UTC zone, now 08:00
    assert r["remind_at"] == "2026-10-15T18:30:00Z"
    r2 = run(engine.schedule_reminder(a["id"], "07:00"))  # already passed -> tomorrow
    assert r2["remind_at"] == "2026-10-16T07:00:00Z"
    assert engine.accounts is before[0] and engine.state is before[1]  # no metadata given: nothing else changes
    assert a["id"] not in engine.state["timers"]
    assert engine._acct(a["id"])["project"] == "Old" and engine._acct(a["id"])["prompt"] == "old prompt"


def test_reminder_project_prompt_nonempty_saved_empty_kept(engine, clock, curl):
    run(setup_slack(engine))
    a = acct(engine)
    run(engine.schedule_reminder(a["id"], "18:30", project="New", prompt=" "))
    assert engine._acct(a["id"])["project"] == "New" and engine._acct(a["id"])["prompt"] == "old prompt"
    run(engine.schedule_reminder(a["id"], "19:30", project="", prompt="fresh"))
    assert engine._acct(a["id"])["project"] == "New" and engine._acct(a["id"])["prompt"] == "fresh"
    assert a["id"] not in engine.state["timers"] and len(engine.accounts["accounts"]) == 1


def test_groups_are_stored_trimmed_and_optional(engine):
    a = run(engine.create_account({"name": "G", "duration_seconds": 3600, "group": "  Work   Stuff "}))
    assert a["group"] == "Work Stuff"
    b = run(engine.create_account({"name": "H", "duration_seconds": 3600}))
    assert b["group"] == ""
    c = run(engine.update_account(a["id"], {"name": "G", "duration_seconds": 3600, "group": ""}))
    assert c["group"] == ""
    with pytest.raises(ValidationFailed):
        run(engine.update_account(a["id"], {"name": "G", "duration_seconds": 3600, "group": "x" * 41}))


def test_reminder_survives_restart_and_is_delivered_once(engine, data_dir, clock, curl):
    run(setup_slack(engine))
    a = acct(engine)
    run(engine.schedule_reminder(a["id"], "18:30"))
    e2 = make_engine(data_dir, clock)  # restart: reloaded from encrypted JSON
    (job,) = e2.notifs["jobs"]
    assert job["kind"] == "reminder" and job["status"] == "pending"
    assert e2.next_deadline().isoformat().startswith("2026-10-15T18:30")
    run(e2.deliver_due())
    assert curl.calls == []  # not due yet
    clock.advance(hours=11)
    run(e2.deliver_due())
    run(e2.deliver_due())
    assert len(curl.calls) == 1 and b"Reminder" in curl.calls[0]["body"]
    assert e2.notifs["jobs"][0]["status"] == "sent"


def test_reminder_retries_persist(engine, data_dir, clock, curl):
    run(setup_slack(engine))
    a = acct(engine)
    run(engine.schedule_reminder(a["id"], "09:00"))
    curl.results.append(notify.CurlResult(0, 500, "err"))
    clock.advance(hours=2)
    run(engine.deliver_due())
    e2 = make_engine(data_dir, clock)
    (job,) = e2.notifs["jobs"]
    assert job["status"] == "pending" and job["attempts"] == 1 and job["last_http_status"] == 500


def test_reminder_does_not_change_active_cooldown(engine, clock, curl):
    run(setup_slack(engine))
    a = acct(engine)
    run(engine.begin_cooldown(a["id"], "default"))
    snap = dict(timer(engine, a))
    run(engine.schedule_reminder(a["id"], "23:00"))
    assert timer(engine, a) == snap
