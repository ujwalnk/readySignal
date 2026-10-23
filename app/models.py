"""Schemas, defaults and validators for the encrypted JSON documents."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSIONS = {"config": 3, "accounts": 2, "state": 1, "notifications": 1}

DEFAULT_TEMPLATE: dict[str, Any] = {
    "text": "Cooldown complete for $account",
    "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "$icon $account cooldown complete", "emoji": True}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": "*Project:*\n$project"},
                {"type": "mrkdwn", "text": "*Started:*\n$ts_start"},
                {"type": "mrkdwn", "text": "*Expired:*\n$ts_expired"},
                {"type": "mrkdwn", "text": "*Reset count:*\n$reset_count"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Continuation prompt:*\n$prompt"}},
    ],
}

DEFAULT_TS_FORMAT = "%Y-%m-%d %H:%M:%S %Z"
MIN_DURATION = 60
MAX_DURATION = 30 * 24 * 3600


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


def default_widget() -> dict[str, Any]:
    return {
        "enabled": False,
        "account_ids": [],  # empty == every non-archived account
        "show_project": True,
        "allow_prompt_copy": True,
    }


def default_config() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSIONS["config"],
        "theme": "system",
        "timezone": "UTC",
        "expiry_check_interval": 60,
        "retry_interval": 60,
        "max_attempts": 5,
        "connect_timeout": 10,
        "request_timeout": 30,
        "ts_start_format": DEFAULT_TS_FORMAT,
        "ts_expired_format": DEFAULT_TS_FORMAT,
        "statistics_enabled": False,
        "analytics_purge_pending": False,
        "widget": default_widget(),
        "slack": {"enabled": False, "webhook_url": "", "template": json.dumps(DEFAULT_TEMPLATE, indent=2)},
    }


def default_accounts() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSIONS["accounts"], "accounts": []}


def default_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSIONS["state"], "timers": {}}


def default_notifications() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSIONS["notifications"], "jobs": []}


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _typed(d: dict[str, Any], key: str, types: type | tuple[type, ...]) -> Any:
    _check(key in d, f"missing key '{key}'")
    _check(isinstance(d[key], types) and not (types is int and isinstance(d[key], bool)), f"invalid type for '{key}'")
    return d[key]


def validate_config(d: Any) -> None:
    _check(isinstance(d, dict), "config must be an object")
    for k, t in (
        ("theme", str),
        ("timezone", str),
        ("expiry_check_interval", int),
        ("retry_interval", int),
        ("max_attempts", int),
        ("connect_timeout", int),
        ("request_timeout", int),
        ("ts_start_format", str),
        ("ts_expired_format", str),
        ("statistics_enabled", bool),
        ("slack", dict),
        ("widget", dict),
    ):
        _typed(d, k, t)
    _check(d["theme"] in ("system", "light", "dark"), "invalid theme")
    widget_types: tuple[tuple[str, type], ...] = (
        ("enabled", bool),
        ("account_ids", list),
        ("show_project", bool),
        ("allow_prompt_copy", bool),
    )
    for wk, wt in widget_types:
        _typed(d["widget"], wk, wt)
    for k, t in (("enabled", bool), ("webhook_url", str), ("template", str)):
        _typed(d["slack"], k, t)


def validate_accounts(d: Any) -> None:
    _check(isinstance(d, dict), "accounts document must be an object")
    items = _typed(d, "accounts", list)
    seen: set[str] = set()
    for a in items:
        _check(isinstance(a, dict), "account must be an object")
        for k, t in (
            ("id", str),
            ("name", str),
            ("icon", str),
            ("duration_seconds", int),
            ("project", str),
            ("prompt", str),
            ("created_at", str),
            ("modified_at", str),
            ("archived", bool),
        ):
            _typed(a, k, t)
        ic = a.get("icon_asset")
        _check(
            ic is None
            or (isinstance(ic, dict) and isinstance(ic.get("id"), str) and isinstance(ic.get("content_type"), str)),
            "invalid icon_asset",
        )
        _check(a["id"] not in seen, "duplicate account id")
        seen.add(a["id"])


def validate_state(d: Any) -> None:
    _check(isinstance(d, dict), "state must be an object")
    timers = _typed(d, "timers", dict)
    for tid, t in timers.items():
        _check(isinstance(t, dict), "timer must be an object")
        _check(t.get("status") in ("available", "cooling"), f"invalid timer status for {tid}")
        for k in ("ts_start", "ts_expired"):
            _check(t.get(k) is None or isinstance(t[k], str), f"invalid {k}")
        _typed(t, "reset_count", int)


def validate_notifications(d: Any) -> None:
    _check(isinstance(d, dict), "notifications must be an object")
    for j in _typed(d, "jobs", list):
        _check(isinstance(j, dict), "job must be an object")
        for k, t in (
            ("id", str),
            ("account_id", str),
            ("expiry_id", str),
            ("destination", str),
            ("attempts", int),
            ("snapshot", dict),
        ):
            _typed(j, k, t)
        _check(j.get("status") in ("pending", "sent", "failed", "skipped"), "invalid job status")


VALIDATORS = {
    "config": validate_config,
    "accounts": validate_accounts,
    "state": validate_state,
    "notifications": validate_notifications,
}
DEFAULTS = {
    "config": default_config,
    "accounts": default_accounts,
    "state": default_state,
    "notifications": default_notifications,
}


def _accounts_v1_to_v2(d: dict[str, Any]) -> dict[str, Any]:
    """v2 adds ``icon_asset`` (None == use the emoji ``icon``). Nothing else changes: ids, durations, timers kept."""
    for a in d["accounts"]:
        a.setdefault("icon_asset", None)
    d["schema_version"] = 2
    return d


def _config_v1_to_v2(d: dict[str, Any]) -> dict[str, Any]:
    """v2 adds the ``widget`` block (disabled by default)."""
    d.setdefault("widget", default_widget())
    d["schema_version"] = 2
    return d


def _config_v2_to_v3(d: dict[str, Any]) -> dict[str, Any]:
    """v3 removes the widget credential machinery (token hash/timestamp and the parent-origin allow-list)."""
    for key in ("allowed_origin", "token_hash", "token_created_at"):
        d["widget"].pop(key, None)
    d["schema_version"] = 3
    return d


# Migrations: {name: {from_version: fn(data) -> data}}. The pre-migration (encrypted) file is kept as *.premigrate-vN.
MIGRATIONS: dict[str, dict[int, Any]] = {
    "config": {1: _config_v1_to_v2, 2: _config_v2_to_v3},
    "accounts": {1: _accounts_v1_to_v2},
    "state": {},
    "notifications": {},
}


def duration_text(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else ""]
    return " ".join(p for p in parts if p) or "0m"
