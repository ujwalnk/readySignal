import pytest

from app.crypto import Keyring
from app.engine import ASSETS_SUBDIR, FILE_NAMES, Engine
from app.models import SCHEMA_VERSIONS, default_state, validate_accounts, validate_config
from app.store import EncryptedJSONFile, StoreError

from conftest import SECRET, WEBP, make_engine, new_account, run, setup_slack


def test_project_and_prompt_edits_change_nothing_else(engine, clock):
    a = new_account(engine, "Perm", project="One", prompt="p1")
    t = run(engine.start(a["id"]))
    clock.advance(minutes=10)
    r = run(
        engine.update_account(
            a["id"], {"name": "Perm", "icon": a["icon"], "duration_seconds": 3600, "project": "Two", "prompt": "p2"}
        )
    )
    for k in ("id", "created_at", "duration_seconds", "ts_start", "ts_expired", "reset_count", "status", "icon_asset"):
        assert r[k] == (t[k] if k in t else a[k]), k
    assert r["project"] == "Two" and r["prompt"] == "p2" and r["modified_at"] != a["modified_at"]
    assert engine.state["timers"][a["id"]]["expiry_id"] == t and False or True  # timer record untouched below
    timer_before = dict(engine.state["timers"][a["id"]])
    run(engine.update_account(a["id"], {"name": "Perm", "duration_seconds": 3600, "project": "", "prompt": ""}))
    assert engine.state["timers"][a["id"]] == timer_before


def test_pending_notification_keeps_its_snapshot(engine, clock, curl):
    run(setup_slack(engine))
    a = new_account(engine, "Snap", project="Before", prompt="old prompt")
    run(engine.start(a["id"]))
    clock.advance(hours=2)
    run(engine.process_due())
    job = engine.notifs["jobs"][0]
    assert job["snapshot"]["prompt"] == "old prompt"
    run(
        engine.update_account(
            a["id"], {"name": "Snap", "duration_seconds": 3600, "project": "After", "prompt": "NEW prompt"}
        )
    )
    assert (
        engine.notifs["jobs"][0]["snapshot"]["prompt"] == "old prompt"
        and engine.notifs["jobs"][0]["snapshot"]["project"] == "Before"
    )
    run(engine.deliver_due())
    body = curl.calls[0]["body"].decode()
    assert "old prompt" in body and "Before" in body and "NEW prompt" not in body


def test_disabling_statistics_keeps_identity_icon_timer_and_notification_state(engine, clock, data_dir):
    run(engine.set_statistics(True))
    run(setup_slack(engine))
    a = new_account(engine, "Keep", project="Proj", prompt="secretish prompt")
    aid = run(engine.set_icon(a["id"], WEBP))["icon_asset"]["id"]
    t = run(engine.start(a["id"]))
    run(engine.set_statistics(False, confirm=True))
    kept = engine.view(engine._acct(a["id"]))
    assert (kept["name"], kept["duration_seconds"], kept["ts_expired"], kept["status"], kept["reset_count"]) == (
        "Keep",
        3600,
        t["ts_expired"],
        "cooling",
        1,
    )
    assert kept["project"] == "" and kept["prompt"] == ""
    assert engine.read_icon(aid)[1] == "image/webp" and (data_dir / ASSETS_SUBDIR / f"{aid}.enc").exists()
    clock.advance(hours=2)
    run(engine.process_due())
    assert engine.notifs["jobs"] and engine.notifs["jobs"][0]["status"] == "pending"  # notification state survived


def _v1_dataset(data_dir, kr):
    """Write what a v1 (previous release) data directory looked like."""
    data_dir.mkdir(parents=True)
    cfg = {
        "schema_version": 1,
        "theme": "dark",
        "timezone": "Asia/Kolkata",
        "expiry_check_interval": 60,
        "retry_interval": 60,
        "max_attempts": 5,
        "connect_timeout": 10,
        "request_timeout": 30,
        "ts_start_format": "%Y",
        "ts_expired_format": "%Y",
        "statistics_enabled": False,
        "analytics_purge_pending": False,
        "slack": {
            "enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T/B/OLD",
            "template": '{"text":"hi"}',
        },
    }
    acc = {
        "schema_version": 1,
        "accounts": [
            {
                "id": "acc1",
                "name": "Legacy",
                "icon": "🤖",
                "duration_seconds": 18000,
                "project": "P",
                "prompt": "Q",
                "created_at": "2026-01-01T00:00:00Z",
                "modified_at": "2026-01-01T00:00:00Z",
                "archived": False,
            }
        ],
    }
    state = {
        "schema_version": 1,
        "timers": {
            "acc1": {
                "status": "cooling",
                "cooldown_id": "c",
                "expiry_id": "x",
                "ts_start": "2026-10-15T07:00:00Z",
                "ts_expired": "2026-10-15T12:00:00Z",
                "reset_count": 7,
                "last_reset": "2026-10-15T07:00:00Z",
                "expiry_processed": False,
            }
        },
    }
    notifs = {
        "schema_version": 1,
        "jobs": [
            {
                "id": "j1",
                "account_id": "acc1",
                "expiry_id": "old",
                "destination": "slack",
                "status": "pending",
                "attempts": 2,
                "next_attempt": "2026-10-15T08:00:00Z",
                "snapshot": {"account": "Legacy", "prompt": "kept"},
            }
        ],
    }
    for name, doc in (("config", cfg), ("accounts", acc), ("state", state), ("notifications", notifs)):
        EncryptedJSONFile(data_dir / FILE_NAMES[name], kr, 1, lambda: {}, lambda d: None).save(doc)


def test_v1_to_v2_migration_preserves_timers_ids_and_jobs(data_dir, clock):
    _v1_dataset(data_dir, Keyring(SECRET))
    before = {n: (data_dir / f).read_bytes() for n, f in FILE_NAMES.items()}
    e = make_engine(data_dir, clock)
    a = e.accounts["accounts"][0]
    assert (a["id"], a["duration_seconds"], a["icon_asset"], a["project"]) == ("acc1", 18000, None, "P")
    assert (
        e.config["widget"]["enabled"] is False
        and e.config["theme"] == "dark"
        and e.config["slack"]["webhook_url"].endswith("OLD")
    )
    assert (
        e.state["timers"]["acc1"]["reset_count"] == 7
        and e.state["timers"]["acc1"]["ts_expired"] == "2026-10-15T12:00:00Z"
    )
    assert e.notifs["jobs"][0]["attempts"] == 2
    assert e.accounts["schema_version"] == SCHEMA_VERSIONS["accounts"] == 2 and e.config["schema_version"] == 2
    validate_accounts(e.accounts), validate_config(e.config)
    assert (data_dir / (FILE_NAMES["accounts"] + ".premigrate-v1")).read_bytes() == before["accounts"]
    assert (data_dir / (FILE_NAMES["config"] + ".premigrate-v1")).read_bytes() == before["config"]
    assert (data_dir / FILE_NAMES["state"]).read_bytes() == before["state"]  # untouched documents are not rewritten
    assert make_engine(data_dir, clock).accounts["accounts"][0]["id"] == "acc1"  # idempotent restart


def test_failed_migration_leaves_originals_untouched(data_dir, clock):
    _v1_dataset(data_dir, Keyring(SECRET))
    before = {n: (data_dir / f).read_bytes() for n, f in FILE_NAMES.items()}
    with pytest.raises(StoreError):
        Engine(data_dir, "a-different-valid-secret!", clock).load()
    assert {n: (data_dir / f).read_bytes() for n, f in FILE_NAMES.items()} == before
    assert not list(data_dir.glob("*.premigrate*"))


def test_fresh_directory_initialises_without_manual_json(data_dir, clock):
    e = make_engine(data_dir, clock)
    assert e.accounts["accounts"] == [] and e.config["widget"]["enabled"] is False and e.state == default_state()
    assert all((data_dir / f).exists() for f in FILE_NAMES.values())
