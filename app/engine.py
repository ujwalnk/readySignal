"""Timer engine, notification queue and the deadline-driven scheduler."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import notify
from .analytics import Analytics
from .assets import AssetStore, ImageError, validate_image
from .crypto import Keyring
from .models import (
    DEFAULTS,
    MAX_DURATION,
    MIGRATIONS,
    MIN_DURATION,
    SCHEMA_VERSIONS,
    VALIDATORS,
    iso,
    new_id,
    parse_iso,
    utcnow,
)
from .store import EncryptedJSONFile, StoreError, cleanup_tmp, recover_rotation

log = logging.getLogger("countdown.engine")

FILE_NAMES = {
    "config": "config.json.enc",
    "accounts": "accounts.json.enc",
    "state": "state.json.enc",
    "notifications": "notifications.json.enc",
}
ANALYTICS_FILE = "analytics.sqlite3"
ASSETS_SUBDIR = Path("assets") / "icons"
ENV_TIMEZONE = "READY_SIGNAL_TIMEZONE"
ENV_WEBHOOK = "SLACK_WEBHOOK_URL"
KEEP_FINISHED_JOBS = 100


class ValidationFailed(Exception):
    pass


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class AmbiguousTime(ValidationFailed):
    """The local wall-clock time occurs twice (DST fall-back); the caller must say which occurrence is meant."""


class PartialFailure(Exception):
    """Part of a combined operation was saved and part was not; the message says exactly which."""


HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
MAX_PENDING_REMINDERS = 50


class ConfigError(Exception):
    """An environment variable holds an unusable value. Messages never contain secret values."""


def _bounded_int(value: Any, lo: int, hi: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValidationFailed(f"{label} must be an integer between {lo} and {hi}")
    return value


def resolve_local_time(text: str, tz_name: str, occurrence: str | None = None) -> datetime:
    """Interpret ``text`` (ISO 8601) in ``tz_name`` and return the exact UTC instant.

    * An explicit UTC offset (``...Z``/``+05:30``) is honoured as given and is never ambiguous.
    * A naive wall-clock time that does not exist (spring-forward gap) is rejected, never shifted.
    * A naive wall-clock time that occurs twice (fall-back overlap) is rejected with ``AmbiguousTime`` unless
      ``occurrence`` is ``"earlier"`` or ``"later"``.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValidationFailed("enter the completion date and time")
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValidationFailed("completion time must look like 2026-10-16T19:30") from None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    if occurrence not in (None, "earlier", "later"):
        raise ValidationFailed("occurrence must be 'earlier' or 'later'")
    tz = ZoneInfo(tz_name)
    first, second = dt.replace(tzinfo=tz, fold=0), dt.replace(tzinfo=tz, fold=1)
    utc_first, utc_second = first.astimezone(timezone.utc), second.astimezone(timezone.utc)
    if utc_first.astimezone(tz).replace(tzinfo=None) != dt:  # round-trip changes the wall time: it falls in a gap
        raise ValidationFailed(
            f"{dt:%Y-%m-%d %H:%M} is not a valid time in {tz_name}; pick another time"
        )
    if utc_first == utc_second:
        return utc_first
    if occurrence is None:
        raise AmbiguousTime(
            f"{dt:%Y-%m-%d %H:%M} occurs twice in {tz_name}; choose the earlier or later one"
        )
    return utc_first if occurrence == "earlier" else utc_second


class Engine:
    def __init__(
        self,
        data_dir: str | Path,
        secret: str | None,
        clock: Callable[[], datetime] = utcnow,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._env = env  # None == the process environment, read at load()
        self.env_timezone = ""  # effective values from READY_SIGNAL_TIMEZONE / SLACK_WEBHOOK_URL ("" == not managed)
        self.env_webhook = ""
        self.clock = clock
        self.keyring = Keyring(secret)  # raises SecretError when invalid
        self.lock = asyncio.Lock()  # config / accounts / state
        self.nlock = asyncio.Lock()  # notification queue (always taken after self.lock)
        self.wake = asyncio.Event()
        self.files: dict[str, EncryptedJSONFile] = {}
        self.analytics: Analytics | None = None
        self.assets: AssetStore | None = None
        self.analytics_error: str | None = None
        self.scheduler_running = False
        self._stop = False
        self._inflight: set[str] = set()
        self.config: dict[str, Any] = {}
        self.accounts: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.notifs: dict[str, Any] = {}

    # ------------------------------------------------------------ startup
    def load(self) -> None:
        """Synchronous: decrypt + validate everything. Raises StoreError and refuses to start on any failure."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
        outcome = recover_rotation(self.data_dir)
        if outcome:
            log.warning("interrupted secret rotation was %s", outcome)
        cleanup_tmp(self.data_dir)
        for name, fname in FILE_NAMES.items():
            self.files[name] = EncryptedJSONFile(
                self.data_dir / fname,
                self.keyring,
                SCHEMA_VERSIONS[name],
                DEFAULTS[name],
                VALIDATORS[name],
                MIGRATIONS[name],
            )
        self._load_env()  # invalid values stop the start-up before anything is written
        self.config = self.files["config"].load()
        self.accounts = self.files["accounts"].load()
        self.state = self.files["state"].load()
        self.notifs = self.files["notifications"].load()
        self.assets = AssetStore(
            self.data_dir / ASSETS_SUBDIR,
            self.keyring,
            lambda: self.files["accounts"]._salt,  # noqa: SLF001
        )
        refs = {a["icon_asset"]["id"] for a in self.accounts["accounts"] if a.get("icon_asset")}
        removed = self.assets.cleanup(refs)  # orphans from a crash between asset write and account commit
        if removed:
            log.info("removed %d orphaned image asset(s)", removed)
        if self.config["statistics_enabled"]:
            self._open_analytics()
        elif self.config.get("analytics_purge_pending"):
            self._finish_purge()

    def _load_env(self) -> None:
        """Validate the environment-managed settings. Empty/absent == GUI-managed; invalid == refuse to start."""
        env = os.environ if self._env is None else self._env
        tz = (env.get(ENV_TIMEZONE) or "").strip()
        hook = (env.get(ENV_WEBHOOK) or "").strip()
        if tz:
            try:
                notify.validate_timezone(tz)
            except ValueError:
                raise ConfigError(
                    f"{ENV_TIMEZONE} is not a valid IANA time zone name (e.g. Asia/Kolkata or UTC)"
                ) from None
        if hook:
            try:
                notify.validate_webhook_url(hook)
            except ValueError:
                raise ConfigError(f"{ENV_WEBHOOK} is not a valid https:// Slack incoming-webhook URL") from None
        self.env_timezone, self.env_webhook = tz, hook

    @property
    def timezone_name(self) -> str:
        """Effective application time zone: the environment value wins while it is set."""
        return self.env_timezone or str(self.config["timezone"])

    def effective_config(self) -> dict[str, Any]:
        """Deep copy of the stored config with environment-managed values applied (server-side use only)."""
        c = copy.deepcopy(self.config)
        c["timezone"] = self.timezone_name
        if self.env_webhook:
            c["slack"]["webhook_url"] = self.env_webhook
        return c

    def _open_analytics(self) -> None:
        self.analytics = Analytics(self.data_dir / ANALYTICS_FILE)
        try:
            self.analytics.init()
            for a in self.accounts["accounts"]:
                self.analytics.upsert_account(a)
        except Exception as exc:  # analytics must never block operation
            self._analytics_failed(exc)

    def _analytics_failed(self, exc: Exception) -> str:
        self.analytics_error = f"analytics unavailable: {type(exc).__name__}"
        log.error("%s (%s)", self.analytics_error, exc)
        return self.analytics_error

    def _commit(self, name: str, new: dict[str, Any]) -> None:
        self.files[name].save(new)  # raises before touching memory if the write fails
        setattr(
            self, {"config": "config", "accounts": "accounts", "state": "state", "notifications": "notifs"}[name], new
        )

    def _record(self, event: dict[str, Any], account: dict[str, Any]) -> list[str]:
        if self.analytics is None:
            return []
        try:
            self.analytics.record(event, account)
            return []
        except Exception as exc:
            return [self._analytics_failed(exc) + " — this event was not recorded"]

    # ------------------------------------------------------------ helpers
    def _acct(self, account_id: str) -> dict[str, Any]:
        for a in self.accounts["accounts"]:
            if a["id"] == account_id:
                return a
        raise NotFound("account not found")

    def view(self, a: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        now = now or self.clock()
        t = self.state["timers"].get(a["id"])
        cooling = bool(t and t["status"] == "cooling" and t["ts_expired"] and parse_iso(t["ts_expired"]) > now)
        asset = a.get("icon_asset")
        return {
            **a,
            "icon_url": f"/assets/icons/{asset['id']}" if asset else None,
            "status": "cooling" if cooling else "available",
            "ts_start": t["ts_start"] if cooling and t else None,
            "ts_expired": t["ts_expired"] if cooling and t else None,
            "reset_count": t["reset_count"] if t else 0,
            "last_reset": t["last_reset"] if t else None,
        }

    def list_accounts(self) -> list[dict[str, Any]]:
        now = self.clock()
        return [self.view(a, now) for a in self.accounts["accounts"]]

    def public_config(self) -> dict[str, Any]:
        """Config for the browser. The webhook URL (stored or environment) never leaves the server."""
        c = copy.deepcopy(self.config)
        c["timezone"] = self.timezone_name
        stored_url = c["slack"].pop("webhook_url")
        if self.env_webhook:
            source, masked = "environment", ""  # not even a suffix of an environment secret is revealed
        elif stored_url:
            source, masked = "gui", notify.mask_url(stored_url)
        else:
            source, masked = "none", ""
        c["slack"]["webhook_configured"] = source != "none"
        c["slack"]["webhook_masked"] = masked
        c["slack"]["webhook_source"] = source
        c["env"] = {"timezone": bool(self.env_timezone), "slack_webhook": bool(self.env_webhook)}
        return c

    # ------------------------------------------------------------ accounts
    def _clean_account_fields(self, d: dict[str, Any], current_id: str | None = None) -> dict[str, Any]:
        name = str(d.get("name", "")).strip()
        if not 1 <= len(name) <= 60:
            raise ValidationFailed("name must be 1-60 characters")
        icon = str(d.get("icon") or "⏳").strip()
        if not 1 <= len(icon) <= 8:
            raise ValidationFailed("icon must be 1-8 characters (an emoji works well)")
        duration = _bounded_int(d.get("duration_seconds"), MIN_DURATION, MAX_DURATION, "duration_seconds")
        project, prompt = str(d.get("project", "")).strip(), str(d.get("prompt", ""))
        if len(project) > 100:
            raise ValidationFailed("project must be at most 100 characters")
        if len(prompt) > 4000:
            raise ValidationFailed("prompt must be at most 4000 characters")
        for a in self.accounts["accounts"]:
            if a["id"] != current_id and a["name"].casefold() == name.casefold():
                raise Conflict("an account with this name already exists")
        return {"name": name, "icon": icon, "duration_seconds": duration, "project": project, "prompt": prompt}

    async def create_account(self, d: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            fields = self._clean_account_fields(d)
            now = iso(self.clock())
            acct = {
                "id": new_id(),
                **fields,
                "icon_asset": None,
                "created_at": now,
                "modified_at": now,
                "archived": False,
            }
            new = copy.deepcopy(self.accounts)
            new["accounts"].append(acct)
            self._commit("accounts", new)
            warnings = self._sync_account(acct)
            return {**self.view(acct), "warnings": warnings}

    async def update_account(self, account_id: str, d: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            self._acct(account_id)
            fields = self._clean_account_fields(d, account_id)
            new = copy.deepcopy(self.accounts)
            acct = next(a for a in new["accounts"] if a["id"] == account_id)
            acct.update(fields, modified_at=iso(self.clock()))  # timers are untouched by design
            self._commit("accounts", new)
            return {**self.view(acct), "warnings": self._sync_account(acct)}

    async def set_archived(self, account_id: str, archived: bool) -> dict[str, Any]:
        async with self.lock:
            self._acct(account_id)
            new = copy.deepcopy(self.accounts)
            acct = next(a for a in new["accounts"] if a["id"] == account_id)
            acct.update(archived=archived, modified_at=iso(self.clock()))
            self._commit("accounts", new)
            return {**self.view(acct), "warnings": self._sync_account(acct)}

    def _sync_account(self, acct: dict[str, Any]) -> list[str]:
        if self.analytics is None:
            return []
        try:
            self.analytics.upsert_account(acct)
            return []
        except Exception as exc:
            return [self._analytics_failed(exc)]

    # ------------------------------------------------------------ images
    async def set_icon(self, account_id: str, data: bytes) -> dict[str, Any]:
        """Validate, sanitise and store an uploaded image, then point the account at it. Timers are untouched."""
        assert self.assets is not None
        try:
            clean, ctype = validate_image(data)
        except ImageError as exc:
            raise ValidationFailed(str(exc)) from exc
        async with self.lock:
            self._acct(account_id)
            new_id_ = self.assets.save(clean, ctype)  # write first: a crash leaves only an orphan, never a dangling ref
            new = copy.deepcopy(self.accounts)
            acct = next(a for a in new["accounts"] if a["id"] == account_id)
            old = acct.get("icon_asset")
            acct.update(icon_asset={"id": new_id_, "content_type": ctype}, modified_at=iso(self.clock()))
            try:
                self._commit("accounts", new)
            except StoreError:
                self.assets.delete(new_id_)
                raise
            if old:
                self.assets.delete(old["id"])
            return {**self.view(acct), "warnings": self._sync_account(acct)}

    async def clear_icon(self, account_id: str) -> dict[str, Any]:
        assert self.assets is not None
        async with self.lock:
            self._acct(account_id)
            new = copy.deepcopy(self.accounts)
            acct = next(a for a in new["accounts"] if a["id"] == account_id)
            old = acct.get("icon_asset")
            acct.update(icon_asset=None, modified_at=iso(self.clock()))
            self._commit("accounts", new)
            if old:
                self.assets.delete(old["id"])
            return {**self.view(acct), "warnings": self._sync_account(acct)}

    def read_icon(self, asset_id: str) -> tuple[bytes, str]:
        assert self.assets is not None
        if not any(a.get("icon_asset", {}) and a["icon_asset"]["id"] == asset_id for a in self.accounts["accounts"]):
            raise NotFound("image not found")
        try:
            return self.assets.read(asset_id)
        except FileNotFoundError:
            raise NotFound("image not found") from None

    # ------------------------------------------------------------ widget
    def _widget_accounts(self) -> list[dict[str, Any]]:
        w = self.config["widget"]
        wanted = set(w["account_ids"])
        return [a for a in self.accounts["accounts"] if not a["archived"] and (not wanted or a["id"] in wanted)]

    def widget_summary(self) -> dict[str, Any]:
        """Only what the widget needs: no prompts, no settings, no paths, no secrets."""
        w, now = self.config["widget"], self.clock()
        out = []
        for a in self._widget_accounts():
            v = self.view(a, now)
            item = {
                "id": v["id"],
                "name": v["name"],
                "icon": v["icon"],
                "icon_url": v["icon_url"],
                "status": v["status"],
                "ts_start": v["ts_start"],
                "ts_expired": v["ts_expired"],
                "duration_seconds": v["duration_seconds"],
                "has_prompt": bool(v["prompt"]) and w["allow_prompt_copy"],
            }
            if w["show_project"]:
                item["project"] = v["project"]
            out.append(item)
        return {
            "now": iso(now),
            "show_project": w["show_project"],
            "allow_prompt_copy": w["allow_prompt_copy"],
            "accounts": out,
        }

    def _widget_account(self, account_id: str) -> dict[str, Any]:
        for a in self._widget_accounts():
            if a["id"] == account_id:
                return a
        raise NotFound("account not found")

    async def widget_start(self, account_id: str) -> dict[str, Any]:
        self._widget_account(account_id)
        r = await self.start(account_id)  # same timer service, persistence, analytics and notification handling
        return {k: r[k] for k in ("id", "status", "ts_start", "ts_expired", "event")}

    def widget_prompt(self, account_id: str) -> str:
        if not self.config["widget"]["allow_prompt_copy"]:
            raise NotFound("prompt copying is disabled")
        return str(self._widget_account(account_id)["prompt"])

    def _clean_widget_patch(self, patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        w = dict(current)
        for key in ("enabled", "show_project", "allow_prompt_copy"):
            if key in patch:
                if not isinstance(patch[key], bool):
                    raise ValidationFailed(f"widget.{key} must be true or false")
                w[key] = patch[key]
        if "account_ids" in patch:
            ids = patch["account_ids"]
            known = {a["id"] for a in self.accounts["accounts"]}
            if not isinstance(ids, list) or any(not isinstance(i, str) or i not in known for i in ids):
                raise ValidationFailed("widget.account_ids must list existing account ids")
            w["account_ids"] = list(dict.fromkeys(ids))
        return w

    # ------------------------------------------------------------ timers
    async def start(self, account_id: str) -> dict[str, Any]:
        async with self.lock:
            acct = self._acct(account_id)
            if acct["archived"]:
                raise ValidationFailed("archived accounts cannot start a cooldown")
            now = self.clock()
            async with self.nlock:
                self._process_expiry_locked(account_id, now)  # settle a lapsed-but-unprocessed expiry first
            return self._start_locked(acct, now, None)

    async def begin_cooldown(
        self,
        account_id: str,
        mode: str = "default",
        completes_at: str | None = None,
        occurrence: str | None = None,
        project: str | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """Main-site Start cooldown. ``mode`` is ``default`` (default duration; extends an active timer) or ``at``
        (explicit completion time). Empty ``project``/``prompt`` keep the saved values; only non-empty ones replace them."""
        if mode not in ("default", "at"):
            raise ValidationFailed("mode must be 'default' or 'at'")
        async with self.lock:
            acct = self._acct(account_id)
            if acct["archived"]:
                raise ValidationFailed("archived accounts cannot start a cooldown")
            now = self.clock()
            new_project = (project or "").strip()
            new_prompt = prompt if (prompt or "").strip() else ""
            if len(new_project) > 100:
                raise ValidationFailed("project must be at most 100 characters")
            if len(new_prompt) > 4000:
                raise ValidationFailed("prompt must be at most 4000 characters")
            target: datetime | None = None
            if mode == "at":
                target = resolve_local_time(completes_at or "", self.timezone_name, occurrence).replace(microsecond=0)
                if target <= now:
                    raise ValidationFailed("That time has already passed. Choose a time in the future.")
                if target > now + timedelta(seconds=MAX_DURATION):
                    raise ValidationFailed("That time is too far in the future (at most 30 days).")
            async with self.nlock:
                self._process_expiry_locked(account_id, now)
            meta_changed = bool(new_project or new_prompt)
            if meta_changed:
                accts = copy.deepcopy(self.accounts)
                a2 = next(a for a in accts["accounts"] if a["id"] == account_id)
                if new_project:
                    a2["project"] = new_project
                if new_prompt:
                    a2["prompt"] = new_prompt
                a2["modified_at"] = iso(now)
                self._commit("accounts", accts)  # metadata first so the analytics snapshot reflects it
                acct = a2
                self._sync_account(acct)
            try:
                result = self._start_locked(acct, now, target)
            except (StoreError, ValidationFailed) as exc:
                if meta_changed:
                    raise PartialFailure(
                        f"The project/prompt were saved, but the cooldown was not started: {exc}"
                    ) from exc
                raise
            return result

    def _start_locked(self, acct: dict[str, Any], now: datetime, target: datetime | None) -> dict[str, Any]:
        account_id = acct["id"]
        timer = self.state["timers"].get(account_id)
        duration = timedelta(seconds=acct["duration_seconds"])
        new = copy.deepcopy(self.state)
        cur = new["timers"].get(account_id) or {"reset_count": 0}
        active = bool(timer and timer["status"] == "cooling" and parse_iso(timer["ts_expired"]) > now)
        if active:
            assert timer is not None
            previous = timer["ts_expired"]
            end = target if target is not None else parse_iso(previous) + duration
            cur.update(ts_expired=iso(end), expiry_id=new_id(), expiry_processed=False)
            etype = "extend"
        else:
            previous = None
            cur.update(
                status="cooling",
                cooldown_id=new_id(),
                expiry_id=new_id(),
                ts_start=iso(now),
                ts_expired=iso(target if target is not None else now + duration),
                expiry_processed=False,
            )
            etype = "start"
        cur.update(status="cooling", last_reset=iso(now), reset_count=cur["reset_count"] + 1)
        new["timers"][account_id] = cur
        self._commit("state", new)
        warnings = self._record(
            {
                "event_id": new_id(),
                "event_type": etype,
                "event_ts": iso(now),
                "cooldown_id": cur["cooldown_id"],
                "ts_start": cur["ts_start"],
                "ts_expired": cur["ts_expired"],
                "previous_ts_expired": previous,
                "configured_seconds": acct["duration_seconds"],
                "project": acct["project"] or None,
                "prompt": acct["prompt"] or None,
                "reset_count": cur["reset_count"],
            },
            acct,
        )
        self.wake.set()
        return {**self.view(acct, now), "event": etype, "warnings": warnings}

    async def schedule_reminder(self, account_id: str, hhmm: str) -> dict[str, Any]:
        """One-off Slack reminder at HH:MM in the effective zone (next day if already past). Touches no timer or metadata."""
        m = HHMM.match((hhmm or "").strip())
        if not m:
            raise ValidationFailed("Enter the reminder time as HH:MM (24-hour), e.g. 18:30.")
        async with self.lock:
            acct = self._acct(account_id)
            if acct["archived"]:
                raise ValidationFailed("archived accounts cannot schedule reminders")
            async with self.nlock:
                cfg = self.effective_config()
                if not cfg["slack"]["enabled"] or not cfg["slack"]["webhook_url"]:
                    raise ValidationFailed("Slack notifications are not enabled; turn them on in Settings to use reminders.")
                if sum(1 for j in self.notifs["jobs"] if j.get("kind") == "reminder" and j["status"] == "pending") >= MAX_PENDING_REMINDERS:
                    raise ValidationFailed("Too many pending reminders.")
                now = self.clock()
                tz = ZoneInfo(self.timezone_name)
                local = now.astimezone(tz)
                h, mi = int(m.group(1)), int(m.group(2))
                day = local.date()
                for _ in range(2):
                    at = datetime(day.year, day.month, day.day, h, mi, tzinfo=tz).astimezone(timezone.utc)
                    if at > now:
                        break
                    day = day + timedelta(days=1)
                rid = new_id()
                new = copy.deepcopy(self.notifs)
                new["jobs"].append(
                    {
                        "id": rid,
                        "kind": "reminder",
                        "account_id": acct["id"],
                        "expiry_id": f"reminder:{rid}",
                        "destination": "slack",
                        "status": "pending",
                        "attempts": 0,
                        "created_at": iso(now),
                        "last_attempt": None,
                        "next_attempt": iso(at),
                        "remind_at": iso(at),
                        "last_http_status": None,
                        "last_error": None,
                        "snapshot": {
                            "account": acct["name"],
                            "icon": acct["icon"],
                            "project": acct["project"],
                            "prompt": "",
                            "ts_expired": iso(at),
                        },
                    }
                )
                self._commit("notifications", new)
        self.wake.set()
        return {"id": rid, "remind_at": iso(at), "account_id": account_id}

    async def set_expiry(
        self,
        account_id: str,
        completes_at: str | None = None,
        occurrence: str | None = None,
        complete_now: bool = False,
    ) -> dict[str, Any]:
        """Move the completion time of a *running* cooldown. Metadata and the default duration are untouched.

        ``completes_at`` is a wall-clock time in the effective application time zone (or carries its own UTC offset).
        It must lie in the future and within MAX_DURATION. ``complete_now=True`` is the explicit way to finish the
        cooldown immediately: the expiry becomes *now* and goes through the normal completion path (event, Slack job).
        A new ``expiry_id`` supersedes the old one, so the old deadline can never notify; no reset is counted.
        """
        async with self.lock:
            acct = self._acct(account_id)
            now = self.clock()
            async with self.nlock:
                self._process_expiry_locked(account_id, now)  # settle a lapsed-but-unprocessed expiry first
            timer = self.state["timers"].get(account_id)
            if not (timer and timer["status"] == "cooling" and parse_iso(timer["ts_expired"]) > now):
                raise Conflict("this account has no running cooldown to edit")
            if acct["archived"]:
                raise ValidationFailed("archived accounts cannot be edited this way")
            if complete_now:
                target = now
            else:
                target = resolve_local_time(completes_at or "", self.timezone_name, occurrence)
                if target <= now:
                    raise ValidationFailed(
                        "the completion time must be in the future; to finish the cooldown now use complete_now"
                    )
                if target > now + timedelta(seconds=MAX_DURATION):
                    raise ValidationFailed("the completion time is too far in the future (at most 30 days)")
            target = target.replace(microsecond=0)
            if not complete_now and iso(target) == timer["ts_expired"]:
                return {**self.view(acct, now), "changed": False, "warnings": []}
            new = copy.deepcopy(self.state)
            cur = new["timers"][account_id]
            cur.update(ts_expired=iso(target), expiry_id=new_id(), expiry_processed=False)
            self._commit("state", new)  # JSON is authoritative; nothing else changes (no reset is counted)
            if complete_now:
                async with self.nlock:
                    self._process_expiry_locked(account_id, now)  # same path as a natural expiry
            self.wake.set()  # the scheduler recomputes its next deadline
            return {**self.view(acct, now), "changed": True, "warnings": []}

    def _process_expiry_locked(self, account_id: str, now: datetime) -> bool:
        """Caller holds self.lock and self.nlock. Order: job first, then state, so a crash re-runs safely."""
        t = self.state["timers"].get(account_id)
        if not t or t["status"] != "cooling" or t.get("expiry_processed") or parse_iso(t["ts_expired"]) > now:
            return False
        acct = self._acct(account_id)
        if not acct["archived"] and self.config["slack"]["enabled"]:
            self._enqueue(acct, t, now)
        new = copy.deepcopy(self.state)
        new["timers"][account_id].update(status="available", expiry_processed=True)
        self._commit("state", new)
        self._record(
            {
                "event_id": f"complete:{t['expiry_id']}",
                "event_type": "complete",
                "event_ts": t["ts_expired"],
                "cooldown_id": t["cooldown_id"],
                "ts_start": t["ts_start"],
                "ts_expired": t["ts_expired"],
                "configured_seconds": int((parse_iso(t["ts_expired"]) - parse_iso(t["ts_start"])).total_seconds()),
                "project": acct["project"] or None,
                "prompt": acct["prompt"] or None,
                "reset_count": t["reset_count"],
            },
            acct,
        )
        return True

    def _enqueue(self, acct: dict[str, Any], t: dict[str, Any], now: datetime) -> None:
        if any(j["expiry_id"] == t["expiry_id"] and j["destination"] == "slack" for j in self.notifs["jobs"]):
            return  # duplicate prevention
        new = copy.deepcopy(self.notifs)
        new["jobs"].append(
            {
                "id": new_id(),
                "account_id": acct["id"],
                "expiry_id": t["expiry_id"],
                "destination": "slack",
                "status": "pending",
                "attempts": 0,
                "created_at": iso(now),
                "last_attempt": None,
                "next_attempt": iso(now),
                "last_http_status": None,
                "last_error": None,
                "snapshot": {
                    "account": acct["name"],
                    "icon": acct["icon"],
                    "project": acct["project"],
                    "prompt": acct["prompt"],
                    "ts_start": t["ts_start"],
                    "ts_expired": t["ts_expired"],
                    "duration_seconds": acct["duration_seconds"],
                    "reset_count": t["reset_count"],
                },
            }
        )
        self._commit("notifications", new)

    async def process_due(self, now: datetime | None = None) -> int:
        now = now or self.clock()
        n = 0
        async with self.lock:
            async with self.nlock:
                for account_id in list(self.state["timers"]):
                    try:
                        n += self._process_expiry_locked(account_id, now)
                    except (StoreError, NotFound) as exc:
                        log.error("could not process expiry: %s", exc)
        return n

    # ------------------------------------------------------------ delivery
    def _due_jobs(self, now: datetime) -> list[str]:
        return [
            j["id"]
            for j in self.notifs["jobs"]
            if j["status"] == "pending"
            and j["id"] not in self._inflight
            and (j["next_attempt"] is None or parse_iso(j["next_attempt"]) <= now)
        ]

    async def deliver_due(self) -> None:
        for job_id in self._due_jobs(self.clock()):
            await self._attempt(job_id)

    async def _attempt(self, job_id: str) -> None:
        self._inflight.add(job_id)
        try:
            async with self.nlock:
                job = next((j for j in self.notifs["jobs"] if j["id"] == job_id), None)
                if not job or job["status"] != "pending":
                    return
                cfg = self.effective_config()  # environment-managed webhook / time zone applied
                slack = cfg["slack"]
                now = self.clock()
                if not slack["enabled"] or not slack["webhook_url"]:
                    self._finish_job(
                        job_id,
                        now,
                        notify.Outcome(False, None, "Slack is disabled or has no webhook", permanent=True),
                        status="skipped",
                    )
                    return
                try:
                    if job.get("kind") == "reminder":
                        payload = notify.reminder_payload(job["snapshot"], cfg)
                    else:
                        payload = notify.render(slack["template"], notify.build_context(job["snapshot"], cfg))
                except ValueError as exc:
                    self._finish_job(
                        job_id, now, notify.Outcome(False, None, f"template error: {exc}", permanent=True), cfg=cfg
                    )
                    return
                new = copy.deepcopy(self.notifs)  # record the attempt *before* sending (crash-safety)
                nj = next(j for j in new["jobs"] if j["id"] == job_id)
                nj.update(attempts=nj["attempts"] + 1, last_attempt=iso(now))
                self._commit("notifications", new)
            outcome = await notify.send_slack(
                slack["webhook_url"], payload, cfg["connect_timeout"], cfg["request_timeout"]
            )
            async with self.nlock:
                self._finish_job(job_id, self.clock(), outcome, cfg=cfg)
        except StoreError as exc:
            log.error("could not persist delivery result: %s", exc)
        finally:
            self._inflight.discard(job_id)

    def _finish_job(
        self,
        job_id: str,
        now: datetime,
        out: notify.Outcome,
        status: str | None = None,
        cfg: dict[str, Any] | None = None,
    ) -> None:
        cfg = cfg or self.config
        new = copy.deepcopy(self.notifs)
        j = next(x for x in new["jobs"] if x["id"] == job_id)
        j["last_http_status"] = out.status
        if out.ok:
            j.update(status="sent", last_error=None, next_attempt=None, sent_at=iso(now))
        else:
            j["last_error"] = out.error
            if status:
                j.update(status=status, next_attempt=None)
            elif out.permanent or j["attempts"] >= cfg["max_attempts"]:
                j.update(status="failed", next_attempt=None)
            else:
                delay = max(cfg["retry_interval"], out.retry_after or 0)
                j["next_attempt"] = iso(now + timedelta(seconds=delay))
        finished = [x for x in new["jobs"] if x["status"] != "pending"]
        if len(finished) > KEEP_FINISHED_JOBS:
            drop = {x["id"] for x in finished[: len(finished) - KEEP_FINISHED_JOBS]}
            new["jobs"] = [x for x in new["jobs"] if x["id"] not in drop]
        self._commit("notifications", new)
        self.wake.set()

    async def retry_job(self, job_id: str) -> None:
        async with self.nlock:
            if not any(j["id"] == job_id and j["status"] in ("failed", "skipped") for j in self.notifs["jobs"]):
                raise NotFound("no failed notification with that id")
            new = copy.deepcopy(self.notifs)
            j = next(x for x in new["jobs"] if x["id"] == job_id)
            j.update(status="pending", attempts=0, next_attempt=iso(self.clock()), last_error=None)
            self._commit("notifications", new)
        self.wake.set()

    def list_jobs(self) -> list[dict[str, Any]]:
        out = []
        for j in reversed(self.notifs["jobs"]):
            out.append({k: v for k, v in j.items() if k != "snapshot"} | {"account": j["snapshot"]["account"]})
        return out

    async def send_test(self) -> dict[str, Any]:
        """Render with sample data and send. Touches no timers, counters or jobs."""
        cfg = self.effective_config()
        slack = cfg["slack"]
        if not slack["webhook_url"]:
            raise ValidationFailed("no webhook URL configured")
        try:
            payload = notify.render(slack["template"], notify.build_context(notify.SAMPLE_SNAPSHOT, cfg))
        except ValueError as exc:
            raise ValidationFailed(f"template error: {exc}") from exc
        out = await notify.send_slack(slack["webhook_url"], payload, cfg["connect_timeout"], cfg["request_timeout"])
        return {"ok": out.ok, "status": out.status, "error": out.error}

    def preview(self, template: str) -> dict[str, Any]:
        try:
            return notify.render(template, notify.build_context(notify.SAMPLE_SNAPSHOT, self.effective_config()))
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc

    # ------------------------------------------------------------ settings
    async def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            new = copy.deepcopy(self.config)
            if "theme" in patch:
                if patch["theme"] not in ("system", "light", "dark"):
                    raise ValidationFailed("theme must be system, light or dark")
                new["theme"] = patch["theme"]
            ignored: list[str] = []
            if "timezone" in patch:
                if self.env_timezone:
                    ignored.append("timezone")  # environment-managed: the stored value is left untouched
                else:
                    try:
                        notify.validate_timezone(str(patch["timezone"]))
                    except ValueError as exc:
                        raise ValidationFailed(str(exc)) from exc
                    new["timezone"] = str(patch["timezone"])
            for key, lo, hi in (
                ("expiry_check_interval", 5, 3600),
                ("retry_interval", 1, 86400),
                ("max_attempts", 1, 50),
                ("connect_timeout", 1, 60),
                ("request_timeout", 2, 120),
            ):
                if key in patch:
                    new[key] = _bounded_int(patch[key], lo, hi, key)
            for key in ("ts_start_format", "ts_expired_format"):
                if key in patch:
                    try:
                        notify.validate_ts_format(str(patch[key]))
                    except ValueError as exc:
                        raise ValidationFailed(f"{key}: {exc}") from exc
                    new[key] = str(patch[key])
            if "widget" in patch:
                new["widget"] = self._clean_widget_patch(patch["widget"], new["widget"])
            slack = patch.get("slack")
            if slack is not None:
                if "enabled" in slack:
                    new["slack"]["enabled"] = bool(slack["enabled"])
                if self.env_webhook:
                    if slack.get("clear_webhook") or slack.get("webhook_url"):
                        ignored.append("slack.webhook_url")  # environment-managed: never replaced or cleared
                elif slack.get("clear_webhook"):
                    new["slack"]["webhook_url"] = ""
                elif slack.get("webhook_url"):
                    try:
                        notify.validate_webhook_url(slack["webhook_url"].strip())
                    except ValueError as exc:
                        raise ValidationFailed(str(exc)) from exc
                    new["slack"]["webhook_url"] = slack["webhook_url"].strip()
                if "template" in slack:
                    try:
                        notify.render(slack["template"], notify.build_context(notify.SAMPLE_SNAPSHOT, new))
                    except ValueError as exc:
                        raise ValidationFailed(f"template: {exc}") from exc
                    new["slack"]["template"] = slack["template"]
                if new["slack"]["enabled"] and not (self.env_webhook or new["slack"]["webhook_url"]):
                    raise ValidationFailed("enter a webhook URL before enabling Slack")
            self._commit("config", new)
        self.wake.set()
        out = self.public_config()
        if ignored:
            out["ignored"] = ignored  # submitted values for environment-managed settings, not applied
        return out

    # ------------------------------------------------------------ statistics
    async def set_statistics(self, enabled: bool, confirm: bool = False) -> dict[str, Any]:
        async with self.lock:
            async with self.nlock:
                if enabled == self.config["statistics_enabled"]:
                    return {"statistics_enabled": enabled, "errors": []}
                new_cfg = copy.deepcopy(self.config)
                if enabled:
                    new_cfg["statistics_enabled"] = True
                    new_cfg["analytics_purge_pending"] = False
                    self._commit("config", new_cfg)
                    self.analytics_error = None
                    self._open_analytics()
                    return {
                        "statistics_enabled": True,
                        "errors": [self.analytics_error] if self.analytics_error else [],
                    }
                if not confirm:
                    raise ValidationFailed(
                        "confirmation required: disabling statistics permanently deletes history "
                        "and all project names and continuation prompts"
                    )
                self.analytics = None  # stop collection first; no connections are kept open
                new_cfg["statistics_enabled"] = False
                new_cfg["analytics_purge_pending"] = True
                self._commit("config", new_cfg)
                self._purge_text_fields()
                errors = self._finish_purge()
                return {"statistics_enabled": False, "errors": errors}

    def _purge_text_fields(self) -> None:
        accts = copy.deepcopy(self.accounts)
        for a in accts["accounts"]:
            a["project"], a["prompt"] = "", ""
        self._commit("accounts", accts)
        notifs = copy.deepcopy(self.notifs)
        for j in notifs["jobs"]:
            j["snapshot"]["project"], j["snapshot"]["prompt"] = "", ""
        self._commit("notifications", notifs)

    def _finish_purge(self) -> list[str]:
        errors = Analytics(self.data_dir / ANALYTICS_FILE).delete_files()
        if not errors:
            cfg = copy.deepcopy(self.config)
            cfg["analytics_purge_pending"] = False
            try:
                self._commit("config", cfg)
            except StoreError as exc:
                errors.append(str(exc))
        else:
            log.error("analytics deletion failed: %s", errors)
        self.analytics_error = None
        return errors

    def graphs(self, **filters: Any) -> dict[str, Any]:
        if self.analytics is None:
            raise NotFound("statistics are disabled")
        try:
            return self.analytics.graphs(tz_name=self.timezone_name, **filters)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        except Exception as exc:
            raise ValidationFailed(self._analytics_failed(exc)) from exc

    def stats(self) -> dict[str, Any]:
        if self.analytics is None:
            raise NotFound("statistics are disabled")
        try:
            return self.analytics.report()
        except Exception as exc:
            raise ValidationFailed(self._analytics_failed(exc)) from exc

    # ------------------------------------------------------------ scheduler
    def next_deadline(self) -> datetime | None:
        times: list[datetime] = []
        for t in self.state["timers"].values():
            if t["status"] == "cooling" and not t.get("expiry_processed") and t["ts_expired"]:
                times.append(parse_iso(t["ts_expired"]))
        for j in self.notifs["jobs"]:
            if j["status"] == "pending" and j["id"] not in self._inflight:
                times.append(parse_iso(j["next_attempt"]) if j["next_attempt"] else self.clock())
        return min(times) if times else None

    def _delay(self) -> float:
        cap = float(self.config["expiry_check_interval"])  # reconciliation safety net
        nd = self.next_deadline()
        if nd is None:
            return cap
        return min(cap, max(0.25, (nd - self.clock()).total_seconds()))

    async def run(self) -> None:
        self.scheduler_running = True
        log.info("scheduler started")
        try:
            while not self._stop:
                self.wake.clear()  # clear first: a wake during the tick is never lost
                delay = 0.0
                try:
                    await self.process_due()
                    await self.deliver_due()
                    delay = self._delay()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("scheduler tick failed")
                    delay = 5.0
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.scheduler_running = False

    def stop(self) -> None:
        self._stop = True
        self.wake.set()


def backup_blob(data_dir: Path) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for fname in FILE_NAMES.values():
            p = data_dir / fname
            if p.exists():
                tar.add(p, arcname=fname)
        assets = data_dir / ASSETS_SUBDIR
        if assets.is_dir():
            for p in sorted(assets.glob("*.enc")):
                tar.add(p, arcname=f"{ASSETS_SUBDIR}/{p.name}")
    return buf.getvalue()
