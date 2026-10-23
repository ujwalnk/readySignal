"""Optional SQLite analytics. Never imported/opened while statistics are disabled."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("countdown.analytics")
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    icon       TEXT NOT NULL,
    archived   INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
);
CREATE TABLE IF NOT EXISTS events (
    event_id           TEXT PRIMARY KEY,
    account_id         TEXT NOT NULL REFERENCES accounts(account_id),
    event_type         TEXT NOT NULL CHECK (event_type IN ('start', 'extend', 'complete')),
    event_ts           TEXT NOT NULL,   -- UTC, YYYY-MM-DDTHH:MM:SSZ
    cooldown_id        TEXT NOT NULL,
    ts_start           TEXT NOT NULL,
    ts_expired         TEXT NOT NULL,
    previous_ts_expired TEXT,           -- extend only
    configured_seconds INTEGER NOT NULL,
    project            TEXT,            -- snapshot of the project name when the event happened
    reset_count        INTEGER,
    prompt             TEXT             -- snapshot of the continuation prompt when the event happened (schema v2)
);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(event_ts);
CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_events_type    ON events(event_type, event_ts);
"""

EVENT_TYPES = ("start", "extend", "complete")
NO_PROJECT = "(no project)"
# Bucket expressions use the UTC event_ts; weeks start on Monday.
GROUP_EXPR = {
    "day": "strftime('%Y-%m-%d', e.event_ts)",
    "week": "date(e.event_ts, 'weekday 0', '-6 days')",
    "month": "strftime('%Y-%m', e.event_ts)",
}


def _local_dt(ts: str, tz: Any) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(tz)


def _bucket(dt: datetime, group: str) -> str:
    if group == "month":
        return dt.strftime("%Y-%m")
    if group == "week":
        return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")  # Monday of that week
    return dt.strftime("%Y-%m-%d")


def _tz(name: str) -> Any:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


REPORTS: dict[str, str] = {
    "monthly_counts": """
        SELECT strftime('%Y-%m', event_ts) AS month, event_type, COUNT(*) AS n
        FROM events GROUP BY month, event_type ORDER BY month, event_type""",
    "monthly_duration_by_account": """
        SELECT strftime('%Y-%m', e.event_ts) AS month, a.name AS account, SUM(e.configured_seconds) AS seconds
        FROM events e JOIN accounts a USING (account_id)
        WHERE e.event_type = 'complete' GROUP BY month, a.account_id ORDER BY month, account""",
    "monthly_duration_by_project": """
        SELECT strftime('%Y-%m', event_ts) AS month, COALESCE(NULLIF(project, ''), '(none)') AS project,
               SUM(configured_seconds) AS seconds
        FROM events WHERE event_type = 'complete' GROUP BY month, project ORDER BY month, project""",
    "reset_counts": """
        SELECT a.name AS account, COUNT(*) AS resets
        FROM events e JOIN accounts a USING (account_id)
        WHERE e.event_type IN ('start', 'extend') GROUP BY a.account_id ORDER BY resets DESC""",
    "activity_by_month": """
        SELECT strftime('%Y-%m', event_ts) AS month, COUNT(*) AS events,
               COUNT(DISTINCT account_id) AS active_accounts
        FROM events GROUP BY month ORDER BY month""",
    "archived_history": """
        SELECT a.name AS account, COUNT(e.event_id) AS events, MAX(e.event_ts) AS last_event
        FROM accounts a LEFT JOIN events e USING (account_id)
        WHERE a.archived = 1 GROUP BY a.account_id ORDER BY a.name""",
}


class Analytics:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        with closing(self._conn()) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"analytics schema {version} is newer than supported {SCHEMA_VERSION}")
            if version < 1:
                conn.executescript(SCHEMA)  # fresh database: current schema, including v2 columns
            elif version < 2:
                conn.execute("ALTER TABLE events ADD COLUMN prompt TEXT")  # v1 -> v2: history is kept, old rows NULL
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def upsert_account(self, account: dict[str, Any]) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO accounts(account_id, name, icon, archived) VALUES (?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET name=excluded.name, icon=excluded.icon, "
                "archived=excluded.archived",
                (account["id"], account["name"], account["icon"], int(account["archived"])),
            )

    def record(self, ev: dict[str, Any], account: dict[str, Any]) -> None:
        """Idempotent on event_id (INSERT OR IGNORE)."""
        with closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT INTO accounts(account_id, name, icon, archived) VALUES (?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET name=excluded.name, icon=excluded.icon, "
                "archived=excluded.archived",
                (account["id"], account["name"], account["icon"], int(account["archived"])),
            )
            conn.execute(
                "INSERT OR IGNORE INTO events(event_id, account_id, event_type, event_ts, cooldown_id, ts_start, "
                "ts_expired, previous_ts_expired, configured_seconds, project, reset_count, prompt) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ev["event_id"],
                    account["id"],
                    ev["event_type"],
                    ev["event_ts"],
                    ev["cooldown_id"],
                    ev["ts_start"],
                    ev["ts_expired"],
                    ev.get("previous_ts_expired"),
                    ev["configured_seconds"],
                    ev.get("project"),
                    ev.get("reset_count"),
                    ev.get("prompt"),
                ),
            )

    def report(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        with closing(self._conn()) as conn:
            conn.row_factory = sqlite3.Row
            for name, sql in REPORTS.items():
                out[name] = [dict(r) for r in conn.execute(sql).fetchall()]
        return out

    def graphs(
        self,
        tz_name: str = "UTC",
        date_from: str | None = None,
        date_to: str | None = None,
        group: str = "day",
        accounts: list[str] | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Chart-ready aggregates computed from SQLite only. Dates are calendar days in ``tz_name``."""
        if group not in GROUP_EXPR:
            raise ValueError("group must be day, week or month")
        tz = _tz(tz_name)
        where, params = ["1=1"], []  # type: list[str], list[Any]
        for col, value, op in (("event_ts >=", date_from, 0), ("event_ts <", date_to, 1)):
            if value:
                try:
                    day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=tz)
                except ValueError as exc:
                    raise ValueError("dates must be YYYY-MM-DD") from exc
                utc = (day + timedelta(days=op)).astimezone(timezone.utc)
                where.append(f"e.{col} ?")
                params.append(utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if accounts:
            where.append(f"e.account_id IN ({','.join('?' * len(accounts))})")
            params += accounts
        if project is not None and project != "":
            if project == NO_PROJECT:
                where.append("COALESCE(e.project, '') = ''")
            else:
                where.append("e.project = ?")
                params.append(project)
        cond = " AND ".join(where)
        with closing(self._conn()) as conn:
            conn.row_factory = sqlite3.Row
            q = lambda sql, extra=(): [dict(r) for r in conn.execute(sql, [*params, *extra]).fetchall()]  # noqa: E731
            total = q(f"SELECT COUNT(*) AS n FROM events e WHERE {cond}")[0]["n"]
            any_events = conn.execute("SELECT 1 FROM events LIMIT 1").fetchone() is not None
            options = {
                "accounts": [
                    dict(r) for r in conn.execute("SELECT account_id AS id, name FROM accounts ORDER BY name")
                ],
                "projects": [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT COALESCE(NULLIF(project, ''), ?) FROM events ORDER BY 1", (NO_PROJECT,)
                    )
                ],
            }
            rows = q(
                f"SELECT e.event_ts AS ts, e.event_type AS t, e.configured_seconds AS s FROM events e WHERE {cond}"
            )
            by_account = q(
                f"SELECT a.name AS label, e.event_type AS t, COUNT(*) AS n FROM events e JOIN accounts a USING (account_id) "
                f"WHERE {cond} GROUP BY a.account_id, t ORDER BY a.name"
            )
            dur_account = q(
                f"SELECT a.name AS label, SUM(e.configured_seconds) AS s, COUNT(*) AS n FROM events e JOIN accounts a USING (account_id) "
                f"WHERE {cond} AND e.event_type = 'complete' GROUP BY a.account_id ORDER BY s DESC"
            )
            by_project = q(
                f"SELECT COALESCE(NULLIF(e.project, ''), '{NO_PROJECT}') AS label, e.event_type AS t, COUNT(*) AS n, "
                f"SUM(CASE WHEN e.event_type = 'complete' THEN e.configured_seconds ELSE 0 END) AS s "
                f"FROM events e WHERE {cond} GROUP BY label, t ORDER BY label"
            )
        local = [(_local_dt(r["ts"], tz), r["t"], r["s"]) for r in rows]
        counts: dict[str, dict[str, int]] = {}
        for dt, t, _ in local:
            counts.setdefault(_bucket(dt, group), {k: 0 for k in EVENT_TYPES})[t] += 1
        buckets = sorted(counts)
        act = {t: [counts[b][t] for b in buckets] for t in EVENT_TYPES}
        months: dict[str, list[int]] = {}
        for dt, t, sec in local:
            if t == "complete":
                m = months.setdefault(dt.strftime("%Y-%m"), [0, 0])
                m[0] += sec
                m[1] += 1
        monthly = [{"bucket": k, "s": v[0], "n": v[1]} for k, v in sorted(months.items())]
        labels = sorted({r["label"] for r in by_account})
        acc = {
            t: [next((r["n"] for r in by_account if r["label"] == lb and r["t"] == t), 0) for lb in labels]
            for t in EVENT_TYPES
        }
        plabels = sorted({r["label"] for r in by_project})
        proj = {
            "starts": [
                next((r["n"] for r in by_project if r["label"] == lb and r["t"] == "start"), 0) for lb in plabels
            ],
            "extends": [
                next((r["n"] for r in by_project if r["label"] == lb and r["t"] == "extend"), 0) for lb in plabels
            ],
            "completed_seconds": [
                sum(r["s"] for r in by_project if r["label"] == lb and r["t"] == "complete") for lb in plabels
            ],
        }
        hours = {"start": [0] * 24, "extend": [0] * 24}
        for dt, t, _ in local:
            if t in hours:
                hours[t][dt.hour] += 1
        return {
            "filters": {
                "from": date_from,
                "to": date_to,
                "group": group,
                "accounts": accounts or [],
                "project": project,
            },
            "options": options,
            "timezone": tz_name,
            "has_history": any_events,
            "matching_events": total,
            "activity": {
                "buckets": buckets,
                "start": act["start"],
                "extend": act["extend"],
                "complete": act["complete"],
            },
            "by_account": {
                "labels": labels,
                "start": acc["start"],
                "extend": acc["extend"],
                "complete": acc["complete"],
            },
            "duration_by_account": {
                "labels": [r["label"] for r in dur_account],
                "seconds": [r["s"] for r in dur_account],
                "completions": [r["n"] for r in dur_account],
            },
            "by_project": {"labels": plabels, **proj},
            "monthly": {
                "buckets": [r["bucket"] for r in monthly],
                "seconds": [r["s"] for r in monthly],
                "completions": [r["n"] for r in monthly],
            },
            "reset_hours": {"hours": list(range(24)), **hours},
        }

    def delete_files(self) -> list[str]:
        """Remove DB + sidecars. Returns a list of error strings (empty on success)."""
        errors = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = Path(str(self.path) + suffix)
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{p.name}: {exc.strerror}")
        return errors
