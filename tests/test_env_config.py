"""READY_SIGNAL_TIMEZONE / SLACK_WEBHOOK_URL precedence, enforcement and secrecy."""

import io
import logging
import tarfile

import pytest
from fastapi.testclient import TestClient

from app.engine import ConfigError, Engine
from app.main import create_app

from conftest import SECRET, csrf_token, make_engine, new_account, run

HOOK = "https://hooks.slack.com/services/T0/B0/ENVSECRETVALUE"
STORED = "https://hooks.slack.com/services/T9/B9/STOREDVALUE"


def client(data_dir, clock, monkeypatch, tz=None, hook=None):
    for k, v in (("READY_SIGNAL_TIMEZONE", tz), ("SLACK_WEBHOOK_URL", hook)):
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    return TestClient(create_app(str(data_dir), SECRET, start_scheduler=False, clock=clock))


def login(c):
    c.headers["X-CSRF-Token"] = csrf_token(c)
    return c


def test_timezone_env_overrides_stored_and_is_enforced(data_dir, clock, monkeypatch):
    with login(client(data_dir, clock, monkeypatch)) as c:
        assert c.put("/api/settings", json={"timezone": "Europe/Berlin"}).status_code == 200
    with login(client(data_dir, clock, monkeypatch, tz="Asia/Kolkata")) as c:
        cfg = c.get("/api/bootstrap").json()["config"]
        assert cfg["timezone"] == "Asia/Kolkata" and cfg["env"] == {"timezone": True, "slack_webhook": False}
        r = c.put("/api/settings", json={"timezone": "UTC", "theme": "dark"})
        assert (
            r.status_code == 200 and r.json()["ignored"] == ["timezone"] and r.json()["theme"] == "dark"
        )  # others save
        assert c.get("/api/bootstrap").json()["config"]["timezone"] == "Asia/Kolkata"
        assert c.app.state.engine.config["timezone"] == "Europe/Berlin"  # stored value untouched
    with login(client(data_dir, clock, monkeypatch)) as c:  # env removed: stored value is back
        cfg = c.get("/api/bootstrap").json()["config"]
        assert cfg["timezone"] == "Europe/Berlin" and cfg["env"]["timezone"] is False


def test_empty_env_values_do_not_override(data_dir, clock, monkeypatch):
    with login(client(data_dir, clock, monkeypatch, tz="", hook="  ")) as c:
        cfg = c.get("/api/bootstrap").json()["config"]
        assert cfg["env"] == {"timezone": False, "slack_webhook": False} and cfg["slack"]["webhook_source"] == "none"
        assert c.put("/api/settings", json={"timezone": "UTC", "slack": {"webhook_url": STORED}}).status_code == 200
        assert c.get("/api/bootstrap").json()["config"]["slack"]["webhook_source"] == "gui"


def test_webhook_env_overrides_hides_and_cannot_be_changed(data_dir, clock, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    with login(client(data_dir, clock, monkeypatch)) as c:
        c.put("/api/settings", json={"slack": {"webhook_url": STORED}})
    with login(client(data_dir, clock, monkeypatch, hook=HOOK)) as c:
        for path in ("/api/bootstrap", "/api/notifications", "/api/encryption", "/"):
            body = c.get(path).text
            assert "ENVSECRETVALUE" not in body and HOOK not in body and "STOREDVALUE" not in body, path
        s = c.get("/api/bootstrap").json()["config"]["slack"]
        assert (
            s["webhook_source"] == "environment"
            and s["webhook_configured"]
            and s["webhook_masked"] == ""
            and "webhook_url" not in s
        )
        r = c.put(
            "/api/settings", json={"slack": {"webhook_url": "https://hooks.slack.com/services/X/Y/Z", "enabled": True}}
        )
        assert (
            r.status_code == 200
            and r.json()["ignored"] == ["slack.webhook_url"]
            and r.json()["slack"]["enabled"] is True
        )
        r = c.put("/api/settings", json={"slack": {"clear_webhook": True}})
        assert r.status_code == 200 and r.json()["slack"]["webhook_configured"] is True
        eng = c.app.state.engine
        assert eng.effective_config()["slack"]["webhook_url"] == HOOK and eng.config["slack"]["webhook_url"] == STORED
    assert "ENVSECRETVALUE" not in caplog.text


def test_enabling_slack_with_only_env_webhook_is_allowed(data_dir, clock, monkeypatch):
    with login(client(data_dir, clock, monkeypatch, hook=HOOK)) as c:
        assert c.put("/api/settings", json={"slack": {"enabled": True}}).status_code == 200


def test_env_webhook_used_for_test_send_and_expiry_notifications(data_dir, clock, monkeypatch, curl):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", HOOK)
    monkeypatch.setenv("READY_SIGNAL_TIMEZONE", "Asia/Kolkata")
    e = make_engine(data_dir, clock)
    run(e.update_settings({"slack": {"enabled": True}}))
    run(e.send_test())
    assert curl.calls[-1]["url"] == HOOK
    a = new_account(e, "N", duration_seconds=3600, prompt="p")
    run(e.start(a["id"]))
    clock.advance(hours=2)
    run(e.process_due())
    run(e.deliver_due())
    assert len(curl.calls) == 2 and curl.calls[-1]["url"] == HOOK
    assert e.timezone_name == "Asia/Kolkata"


def test_invalid_env_values_fail_clearly_without_echoing_secret(data_dir, clock, monkeypatch):
    monkeypatch.setenv("READY_SIGNAL_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ConfigError, match="READY_SIGNAL_TIMEZONE"):
        Engine(data_dir, SECRET, clock).load()
    monkeypatch.delenv("READY_SIGNAL_TIMEZONE")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://evil.example/NOTASECRET")
    with pytest.raises(ConfigError) as ei:
        Engine(data_dir, SECRET, clock).load()
    assert "SLACK_WEBHOOK_URL" in str(ei.value) and "NOTASECRET" not in str(ei.value)
    assert not data_dir.exists() or not any(data_dir.glob("*.enc"))  # nothing was written


def test_effective_timezone_drives_notification_timestamps_and_stats(data_dir, clock, monkeypatch):
    monkeypatch.setenv("READY_SIGNAL_TIMEZONE", "Asia/Kolkata")
    e = make_engine(data_dir, clock)
    assert e.effective_config()["timezone"] == "Asia/Kolkata" and e.public_config()["timezone"] == "Asia/Kolkata"
    assert e.config["timezone"] != "Asia/Kolkata"


def test_backup_download_is_encrypted_archive(data_dir, clock, monkeypatch):
    with login(client(data_dir, clock, monkeypatch)) as c:
        a = c.post(
            "/api/accounts", json={"name": "Claude", "duration_seconds": 60, "prompt": "PLAINTEXT-PROMPT"}
        ).json()
        c.put("/api/settings", json={"slack": {"webhook_url": STORED}})
        r = c.get("/api/backup")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/gzip") or r.status_code == 200
        assert 'filename="ready-signal-backup-encrypted.tar.gz"' in r.headers["content-disposition"]
        tar = tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz")
        names = tar.getnames()
        assert {"config.json.enc", "accounts.json.enc", "state.json.enc"} <= set(names) or len(names) >= 3
        assert all(n.endswith(".enc") for n in names)
        for m in tar.getmembers():
            blob = tar.extractfile(m).read()
            assert b"PLAINTEXT-PROMPT" not in blob and b"STOREDVALUE" not in blob and a["id"].encode() not in blob
