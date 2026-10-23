import json
import shutil

import pytest

from app import notify
from app.engine import ValidationFailed
from app.notify import CurlResult

from conftest import make_engine, run, setup_slack

CTX = {
    "prompt": 'He said "hi" \\ back\nline2 🚀 $x',
    "project": "P",
    "account": "Acc",
    "icon": "🤖",
    "ts_start": "S",
    "ts_expired": "E",
    "duration": "5h",
    "reset_count": "3",
}


def test_default_template_renders_valid_block_kit():
    from app.models import default_config

    p = notify.render(default_config()["slack"]["template"], CTX)
    assert p["text"] == "Cooldown complete for Acc" and p["blocks"][0]["text"]["text"] == "🤖 Acc cooldown complete"
    assert p["blocks"][2]["text"]["text"].endswith(CTX["prompt"])
    json.dumps(p)  # still serialisable; nothing corrupted by quotes/newlines


def test_placeholders_literal_dollars_unknown_and_single_pass():
    assert notify.expand("$$account $account ${account}s $unknown $5", CTX) == "$account Acc Accs $unknown $5"
    ctx = dict(CTX, project="$account")  # substituted values are not re-expanded
    assert notify.expand("$project", ctx) == "$account"


def test_json_keys_not_expanded_and_invalid_templates():
    p = notify.render('{"text": "t", "$account": "$account"}', CTX)
    assert p["$account"] == "Acc"
    for bad in ("{", "[]", '{"blocks": []}', '{"text": "x", "blocks": [1]}', '{"text":"x","blocks":"no"}'):
        with pytest.raises(ValueError):
            notify.render(bad, CTX)


def test_timestamp_format_and_timezone():
    assert notify.format_ts("2026-10-15T08:00:00Z", "Asia/Kolkata", "%Y-%m-%d %H:%M:%S %Z") == "2026-10-15 13:30:00 IST"
    with pytest.raises(ValueError):
        notify.validate_timezone("Mars/Base")
    with pytest.raises(ValueError):
        notify.validate_ts_format("")


def test_mask_and_sanitize():
    url = "https://hooks.slack.com/services/T/B/SECRETTOKEN"
    assert "SECRET" not in notify.mask_url(url) and notify.mask_url(url).endswith("OKEN")
    assert "SECRET" not in notify.sanitize(f"failed to reach {url} !", (url,))


def outcome(status, body, code=0, ra=None):
    async def go():
        async def fake(*a, **k):
            return CurlResult(code, status, body, ra, None if code == 0 else "boom")

        notify.curl_post = fake
        return await notify.send_slack("https://h.example/x", {"text": "t"}, 5, 10)

    return run(go())


def test_outcome_classification(monkeypatch):
    orig = notify.curl_post
    try:
        assert outcome(200, "ok").ok
        o = outcome(200, "weird")
        assert not o.ok and not o.permanent
        assert outcome(400, "invalid_payload").permanent
        o = outcome(429, "rate", ra=120)
        assert not o.ok and not o.permanent and o.retry_after == 120
        assert not outcome(503, "x").permanent
        o = outcome(None, "", code=28)
        assert not o.ok and "exit 28" in o.error
        assert outcome(200, "ok", code=7).ok is False  # exit code must be zero too
    finally:
        notify.curl_post = orig


def test_missing_curl(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    r = run(notify.curl_post("https://example.com/x", b"{}", 1, 2))
    assert r.exit_code == 127 and "not found" in r.error


@pytest.mark.skipif(not shutil.which("curl"), reason="curl required")
def test_real_curl_invocation_against_local_listener_without_leaking_url():
    """Run the genuine curl code path against a local TLS-less port: --proto =https must refuse http."""
    r = run(notify.curl_post("http://127.0.0.1:9/hook", b"{}", 1, 2))
    assert r.exit_code != 0 and r.status is None


def test_retry_schedule_and_max_attempts(engine, clock, curl):
    async def main():
        await engine.update_settings({"max_attempts": 3, "retry_interval": 90})
        await setup_slack(engine)
        a = await engine.create_account({"name": "A", "duration_seconds": 3600, "prompt": "go"})
        await engine.start(a["id"])
        clock.advance(hours=1, seconds=1)
        curl.results += [CurlResult(0, 503, "down")] * 3
        await engine.process_due()
        await engine.deliver_due()
        j = engine.notifs["jobs"][0]
        assert j["status"] == "pending" and j["attempts"] == 1 and j["last_http_status"] == 503
        assert "SECRETTOKEN" not in (j["last_error"] or "")
        await engine.deliver_due()  # not eligible yet
        assert len(curl.calls) == 1
        clock.advance(seconds=91)
        await engine.deliver_due()
        clock.advance(seconds=91)
        await engine.deliver_due()
        j = engine.notifs["jobs"][0]
        assert j["status"] == "failed" and j["attempts"] == 3 and len(curl.calls) == 3
        clock.advance(days=1)
        await engine.deliver_due()
        assert len(curl.calls) == 3  # no more attempts
        assert engine.view(engine._acct(a["id"]))["status"] == "available"  # timer untouched by failures
        await engine.retry_job(j["id"])
        await engine.deliver_due()
        assert engine.notifs["jobs"][0]["status"] == "sent"

    run(main())


def test_429_retry_after_and_permanent_400(engine, clock, curl):
    async def main():
        await setup_slack(engine)
        a = await engine.create_account({"name": "A", "duration_seconds": 3600})
        await engine.start(a["id"])
        clock.advance(hours=2)
        curl.results += [CurlResult(0, 429, "slow", 600)]
        await engine.process_due()
        await engine.deliver_due()
        nxt = engine.notifs["jobs"][0]["next_attempt"]
        assert (notify.parse_iso(nxt) - clock.now).total_seconds() == 600
        clock.advance(seconds=601)
        curl.results += [CurlResult(0, 400, "invalid_blocks")]
        await engine.deliver_due()
        assert engine.notifs["jobs"][0]["status"] == "failed" and engine.notifs["jobs"][0]["attempts"] == 2

    run(main())


def test_pending_job_survives_restart(data_dir, clock, curl):
    async def main():
        e = make_engine(data_dir, clock)
        await setup_slack(e)
        a = await e.create_account({"name": "A", "duration_seconds": 3600})
        await e.start(a["id"])
        clock.advance(hours=2)
        curl.results.append(CurlResult(0, 500, "x"))
        await e.process_due()
        await e.deliver_due()
        clock.advance(seconds=120)
        e2 = make_engine(data_dir, clock)
        assert e2.notifs["jobs"][0]["attempts"] == 1
        await e2.deliver_due()
        assert e2.notifs["jobs"][0]["status"] == "sent"

    run(main())


def test_payload_sent_is_valid_json_with_hostile_prompt(engine, clock, curl):
    async def main():
        await setup_slack(engine)
        prompt = 'quote " backslash \\ newline\n unicode ☃ $account'
        a = await engine.create_account({"name": "A", "duration_seconds": 3600, "prompt": prompt, "project": "p"})
        await engine.start(a["id"])
        clock.advance(hours=2)
        await engine.process_due()
        await engine.deliver_due()
        body = json.loads(curl.calls[0]["body"].decode())
        assert body["blocks"][2]["text"]["text"].endswith(prompt)

    run(main())


def test_test_notification_does_not_touch_timers(engine, clock, curl):
    async def main():
        await setup_slack(engine)
        a = await engine.create_account({"name": "A", "duration_seconds": 3600})
        await engine.start(a["id"])
        before = (json.dumps(engine.state, sort_keys=True), json.dumps(engine.notifs, sort_keys=True))
        r = await engine.send_test()
        assert r["ok"] and len(curl.calls) == 1
        assert before == (json.dumps(engine.state, sort_keys=True), json.dumps(engine.notifs, sort_keys=True))

    run(main())


def test_settings_validation_and_masking(engine):
    async def main():
        with pytest.raises(ValidationFailed):
            await engine.update_settings({"slack": {"enabled": True}})
        with pytest.raises(ValidationFailed):
            await engine.update_settings({"slack": {"webhook_url": "http://insecure"}})
        with pytest.raises(ValidationFailed):
            await engine.update_settings({"slack": {"template": "{bad"}})
        with pytest.raises(ValidationFailed):
            await engine.update_settings({"retry_interval": 0})
        cfg = await engine.update_settings(
            {"slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/services/A/B/TOPSECRETXYZ"}}
        )
        assert "webhook_url" not in cfg["slack"] and "TOPSECRET" not in json.dumps(cfg)

    run(main())


def test_slack_disabled_creates_no_job(engine, clock, curl):
    async def main():
        a = await engine.create_account({"name": "A", "duration_seconds": 3600})
        await engine.start(a["id"])
        clock.advance(hours=2)
        await engine.process_due()
        assert engine.notifs["jobs"] == [] and curl.calls == []

    run(main())
