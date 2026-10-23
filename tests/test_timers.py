import asyncio

import pytest

from app.engine import Conflict, ValidationFailed
from app.models import parse_iso

from conftest import make_engine, run, setup_slack


async def mk(e, name="A", seconds=18000, **kw):
    return await e.create_account({"name": name, "duration_seconds": seconds, **kw})


def test_start_extend_fresh(engine, clock):
    async def main():
        a = await mk(engine)
        r = await engine.start(a["id"])
        assert r["event"] == "start" and r["status"] == "cooling"
        assert parse_iso(r["ts_expired"]) - parse_iso(r["ts_start"]) == (parse_iso(r["ts_expired"]) - clock.now)
        clock.advance(hours=2)  # 3h remaining
        r2 = await engine.start(a["id"])
        assert r2["event"] == "extend" and r2["reset_count"] == 2
        assert (parse_iso(r2["ts_expired"]) - clock.now).total_seconds() == 8 * 3600
        assert r2["ts_start"] == r["ts_start"]
        clock.advance(hours=9)  # expired
        r3 = await engine.start(a["id"])
        assert r3["event"] == "start" and r3["reset_count"] == 3
        assert (parse_iso(r3["ts_expired"]) - clock.now).total_seconds() == 5 * 3600

    run(main())


def test_independent_accounts_and_edits_do_not_touch_timer(engine, clock):
    async def main():
        a, b = await mk(engine, "A"), await mk(engine, "B", 3600)
        ra = await engine.start(a["id"])
        assert engine.view(engine._acct(b["id"]))["status"] == "available"
        up = await engine.update_account(
            a["id"], {"name": "A2", "duration_seconds": 7200, "project": "P", "prompt": "x"}
        )
        assert up["ts_expired"] == ra["ts_expired"] and up["duration_seconds"] == 7200

    run(main())


def test_validation_and_duplicates(engine):
    async def main():
        for bad in (0, 59, 10**9, "x", True):
            with pytest.raises(ValidationFailed):
                await engine.create_account({"name": "A", "duration_seconds": bad})
        with pytest.raises(ValidationFailed):
            await engine.create_account({"name": " ", "duration_seconds": 3600})
        await mk(engine, "Same")
        with pytest.raises(Conflict):
            await mk(engine, "same")

    run(main())


def test_archive_restore(engine):
    async def main():
        a = await mk(engine)
        assert (await engine.set_archived(a["id"], True))["archived"]
        with pytest.raises(ValidationFailed):
            await engine.start(a["id"])
        assert not (await engine.set_archived(a["id"], False))["archived"]

    run(main())


def test_expiry_creates_single_job_and_superseded_expiry_does_not(engine, clock, curl):
    async def main():
        await setup_slack(engine)
        a = await mk(engine, seconds=3600)
        await engine.start(a["id"])
        clock.advance(minutes=30)
        await engine.start(a["id"])  # extended -> old expiry (T+1h) superseded
        clock.advance(minutes=45)  # past the old expiry, before the new one
        assert await engine.process_due() == 0 and engine.notifs["jobs"] == []
        clock.advance(hours=1)
        assert await engine.process_due() == 1
        assert await engine.process_due() == 0  # idempotent
        assert len(engine.notifs["jobs"]) == 1 and engine.view(engine._acct(a["id"]))["status"] == "available"

    run(main())


def test_missed_expiry_recovered_after_restart(data_dir, clock, curl):
    async def main():
        e = make_engine(data_dir, clock)
        await setup_slack(e)
        a = await mk(e, seconds=3600)
        await e.start(a["id"])
        clock.advance(hours=5)  # "container was down"
        e2 = make_engine(data_dir, clock)
        assert e2.next_deadline() is not None and e2.next_deadline() <= clock.now
        await e2.process_due()
        await e2.deliver_due()
        assert e2.notifs["jobs"][0]["status"] == "sent" and len(curl.calls) == 1
        e3 = make_engine(data_dir, clock)
        await e3.process_due()
        await e3.deliver_due()
        assert len(e3.notifs["jobs"]) == 1 and len(curl.calls) == 1  # no duplicate after another restart

    run(main())


def test_crash_between_job_and_state_does_not_duplicate(engine, clock, curl):
    async def main():
        await setup_slack(engine)
        a = await mk(engine, seconds=3600)
        await engine.start(a["id"])
        clock.advance(hours=2)
        t = engine.state["timers"][a["id"]]
        engine._enqueue(engine._acct(a["id"]), t, clock.now)  # simulate: job saved, state not yet
        await engine.process_due()
        assert len(engine.notifs["jobs"]) == 1

    run(main())


def test_start_settles_unprocessed_expiry(engine, clock, curl):
    async def main():
        await setup_slack(engine)
        a = await mk(engine, seconds=3600)
        await engine.start(a["id"])
        clock.advance(hours=2)
        await engine.start(a["id"])  # scheduler had not run yet
        assert len(engine.notifs["jobs"]) == 1

    run(main())


def test_concurrent_starts_are_serialised(engine):
    async def main():
        a = await mk(engine, seconds=3600)
        await asyncio.gather(*[engine.start(a["id"]) for _ in range(10)])
        t = engine.state["timers"][a["id"]]
        assert t["reset_count"] == 10
        assert (parse_iso(t["ts_expired"]) - parse_iso(t["ts_start"])).total_seconds() == 10 * 3600

    run(main())


def test_scheduler_wakes_on_start_and_sleeps_until_deadline(data_dir, clock):
    async def main():
        e = make_engine(data_dir, clock)
        a = await mk(e, seconds=3600)
        assert e._delay() == 60  # nothing pending: only the reconciliation interval
        await e.start(a["id"])
        assert e._delay() == 60  # deadline far away, capped by reconcile interval
        clock.advance(minutes=59, seconds=50)
        assert e._delay() == pytest.approx(10)
        task = asyncio.create_task(e.run())
        await asyncio.sleep(0.05)
        assert e.scheduler_running
        e.stop()
        await asyncio.wait_for(task, 2)

    run(main())
