"""Timer engine, notification queue and the deadline-driven scheduler."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import logging
import re
import secrets as _secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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
TOKEN_PREFIX = "rsw_"
ORIGIN_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:\d{1,5})?")
KEEP_FINISHED_JOBS = 100


class ValidationFailed(Exception):
    pass


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _bounded_int(value: Any, lo: int, hi: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValidationFailed(f"{label} must be an integer between {lo} and {hi}")
    return value


class Engine:
    def __init__(self, data_dir: str | Path, secret: str | None, clock: Callable[[], datetime] = utcnow) -> None:
        self.data_dir = Path(data_dir)
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
        c = copy.deepcopy(self.config)
        url = c["slack"].pop("webhook_url")
        c["slack"]["webhook_configured"] = bool(url)
        c["slack"]["webhook_masked"] = notify.mask_url(url)  # template stays; URL never leaves the server
        c["widget"].pop("token_hash")
        c["widget"]["token_configured"] = bool(self.config["widget"]["token_hash"])
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
    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def create_widget_token(self) -> str:
        """Issue a new widget credential (replacing any previous one). Only its hash is stored; shown once."""
        async with self.lock:
            token = TOKEN_PREFIX + _secrets.token_urlsafe(32)
            new = copy.deepcopy(self.config)
            new["widget"].update(token_hash=self._token_hash(token), token_created_at=iso(self.clock()))
            self._commit("config", new)
            return token

    async def revoke_widget_token(self) -> None:
        async with self.lock:
            new = copy.deepcopy(self.config)
            new["widget"].update(token_hash="", token_created_at=None)
            self._commit("config", new)

    def widget_authorised(self, token: str | None) -> bool:
        w = self.config["widget"]
        if not (w["enabled"] and w["token_hash"] and token):
            return False
        return hmac.compare_digest(self._token_hash(token), w["token_hash"])

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
        if "allowed_origin" in patch:
            origin = str(patch["allowed_origin"]).strip().rstrip("/")
            if origin and not ORIGIN_RE.fullmatch(origin):
                raise ValidationFailed(
                    "allowed origin must look like https://dashboard.example.com (scheme, host, optional port)"
                )
            w["allowed_origin"] = origin
        if "account_ids" in patch:
            ids = patch["account_ids"]
            known = {a["id"] for a in self.accounts["accounts"]}
            if not isinstance(ids, list) or any(not isinstance(i, str) or i not in known for i in ids):
                raise ValidationFailed("widget.account_ids must list existing account ids")
            w["account_ids"] = list(dict.fromkeys(ids))
        if w["enabled"] and not w["allowed_origin"]:
            raise ValidationFailed("set the authorised parent-dashboard origin before enabling the widget")
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
            timer = self.state["timers"].get(account_id)
            duration = timedelta(seconds=acct["duration_seconds"])
            new = copy.deepcopy(self.state)
            cur = new["timers"].get(account_id) or {"reset_count": 0}
            active = bool(timer and timer["status"] == "cooling" and parse_iso(timer["ts_expired"]) > now)
            if active:
                assert timer is not None
                previous = timer["ts_expired"]
                cur.update(ts_expired=iso(parse_iso(previous) + duration), expiry_id=new_id())
                etype = "extend"
            else:
                previous = None
                cur.update(
                    status="cooling",
                    cooldown_id=new_id(),
                    expiry_id=new_id(),
                    ts_start=iso(now),
                    ts_expired=iso(now + duration),
                    expiry_processed=False,
                )
                etype = "start"
            cur.update(status="cooling", last_reset=iso(now), reset_count=cur["reset_count"] + 1)
            new["timers"][account_id] = cur
            self._commit("state", new)  # JSON is authoritative; analytics follows
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
                cfg = copy.deepcopy(self.config)
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
        cfg = copy.deepcopy(self.config)
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
            return notify.render(template, notify.build_context(notify.SAMPLE_SNAPSHOT, self.config))
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
            if "timezone" in patch:
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
                if slack.get("clear_webhook"):
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
                if new["slack"]["enabled"] and not new["slack"]["webhook_url"]:
                    raise ValidationFailed("enter a webhook URL before enabling Slack")
            self._commit("config", new)
        self.wake.set()
        return self.public_config()

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
            return self.analytics.graphs(tz_name=self.config["timezone"], **filters)
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
