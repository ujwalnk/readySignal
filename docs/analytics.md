# SQLite analytics (optional)

Used only when *Statistics* is on; otherwise the file is never opened, created or queried. JSON is authoritative: the JSON write happens first, then the event is recorded. If recording fails the timer still works and the UI shows "event was not recorded". Completion events use the deterministic ID `complete:<expiry_id>` with `INSERT OR IGNORE`, so replays are idempotent.

Schema (`PRAGMA user_version = 1`; timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ`):
* `accounts(account_id PK, name, icon, archived)`
* `events(event_id PK, account_id FK, event_type CHECK IN ('start','extend','complete'), event_ts, cooldown_id, ts_start, ts_expired, previous_ts_expired, configured_seconds, project, reset_count, prompt)` — `project` and `prompt` are **snapshots taken when the event happened**; later edits never rewrite them
* indexes: `idx_events_ts(event_ts)`, `idx_events_account(account_id,event_ts)`, `idx_events_type(event_type,event_ts)`

**Migrations:** v1 → v2 runs at start (`ALTER TABLE events ADD COLUMN prompt TEXT`; old rows get NULL; history kept). Newer-than-supported databases are refused.

**Retention:** snapshots (including prompt text) are kept until statistics are disabled, which deletes the whole database. There is no automatic expiry.

Semantics: `start` and `extend` record the configured seconds *added*; `complete` records the full scheduled span `ts_expired − ts_start` (including extensions) at the expiry time. None of these is measured service usage. `reset count = #start + #extend`.

```sql
-- monthly cooldown counts
SELECT strftime('%Y-%m', event_ts) month, event_type, COUNT(*) FROM events GROUP BY 1,2;
-- monthly duration by account
SELECT strftime('%Y-%m', e.event_ts) month, a.name, SUM(e.configured_seconds)/3600.0 hours
FROM events e JOIN accounts a USING(account_id) WHERE event_type='complete' GROUP BY 1,a.account_id;
-- monthly duration by project
SELECT strftime('%Y-%m', event_ts) month, COALESCE(NULLIF(project,''),'(none)') project, SUM(configured_seconds)
FROM events WHERE event_type='complete' GROUP BY 1,2;
-- reset counts
SELECT a.name, COUNT(*) FROM events e JOIN accounts a USING(account_id) WHERE event_type IN ('start','extend') GROUP BY a.account_id;
-- activity by month
SELECT strftime('%Y-%m', event_ts) month, COUNT(*) events, COUNT(DISTINCT account_id) accounts FROM events GROUP BY 1;
-- archived account history
SELECT a.name, COUNT(e.event_id), MAX(e.event_ts) FROM accounts a LEFT JOIN events e USING(account_id) WHERE a.archived=1 GROUP BY a.account_id;
```
(The same queries live in `app/analytics.py::REPORTS` and are exercised by the tests.)

**Disabling statistics is destructive:** stops collection, closes/never holds connections, deletes `analytics.sqlite3` plus `-wal/-shm/-journal`, clears existing project names and prompts (accounts and queued notification snapshots), and keeps account names, icons, durations, active timers and all notification state. If a file cannot be deleted the UI reports the error and the purge is retried at next start. Re-enabling creates an empty database; deleted history is not reconstructed.

## Statistics page (graphs)
The UI is graph-first (vendored uPlot, no tables): activity over time (start/extend/complete by day/week/month), activity by account, completed configured hours by account, activity by project snapshot, monthly completed-hours trend, and resets by hour of day. All data comes from `GET /api/stats/graphs?from=YYYY-MM-DD&to=YYYY-MM-DD&group=day|week|month&account=<id>&account=<id>&project=<name|(no project)>` (SQLite only; `404` while statistics are disabled). Dates are calendar days and buckets/hours use the configured time zone; weeks start Monday. Filters are bound parameters. `has_history=false` → "No history yet"; `matching_events=0` → "No events match". Charts never invent data, and cooldown hours are timer lengths, **not** measured service usage.
