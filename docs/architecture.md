# Architecture

Single process, single Uvicorn worker (mandatory: the scheduler and notification queue live in memory + encrypted files; multiple workers would double-deliver).

```
browser ── /api (CSRF) ──► FastAPI ──► Engine ──► EncryptedJSONFile ×4 (/data/*.json.enc)
                                         │  ▲                │
                                         │  └─ asyncio.Event ┘ wake on start/extend/settings/retry
                                         ├──► Analytics (optional) ──► /data/analytics.sqlite3
                                         └──► notify.curl_post ──► curl (no shell) ──► Slack webhook
```
Locks: `Engine.lock` (config/accounts/state) then `Engine.nlock` (queue) – always in that order. Delivery releases both while curl runs, so a slow Slack never blocks starting/extending timers. Every mutation is applied to a deep copy, written, and only then swapped into memory, so a failed write leaves memory and disk consistent.

## Files (all AES-256-GCM encrypted)
| File | Schema v1 content |
|---|---|
| `config.json.enc` | theme, timezone, intervals, retry/timeouts, ts formats, `statistics_enabled`, `analytics_purge_pending`, `slack{enabled,webhook_url,template}` |
| `accounts.json.enc` | `accounts[]{id,name,icon,duration_seconds,project,prompt,created_at,modified_at,archived}` |
| `state.json.enc` | `timers{account_id:{status,ts_start,ts_expired,last_reset,reset_count,cooldown_id,expiry_id,expiry_processed}}` |
| `notifications.json.enc` | `jobs[]{id,account_id,expiry_id,destination,status(pending/sent/failed/skipped),attempts,last_attempt,next_attempt,last_http_status,last_error,snapshot{…}}` |

Schema versions are explicit; `store.py` runs registered migrations (keeping an encrypted `*.premigrate-vN` copy) and refuses files from a *newer* schema.

## Timer / scheduler details
`_delay = min(expiry_check_interval, next_deadline − now)`; the loop clears the wake event *before* working so no wake-up is lost; a failing tick backs off 5 s. Finished jobs are pruned to the latest 100.

## Resource use
Measured on an x86 Linux sandbox (not a Pi 5), idle with one timer: ≈56 MB RSS, 1 thread, ≈0.2 % CPU. scrypt (32 MiB, ~0.1 s) runs once at startup per file salt, then keys are cached. Expect similar order of magnitude on a Pi 5 but measure there.

## Interpretation notes
* **Disabling statistics** deletes the DB and *clears existing* project names and prompts (accounts + queued-notification snapshots). Afterwards, new projects/prompts may still be entered because notifications need them; they are operational JSON data and are purged again at the next disable.
* Notification jobs are only created while Slack is enabled at expiry time.
* Archived accounts expire silently (no notification).

## Ready Signal 2.0 additions
* **Identity vs context:** accounts have a stable `id`, name, image, default duration, created/modified times and archived flag. `project` and `prompt` are mutable context: editing them never touches timers, reset counts, durations or already-queued notification snapshots (taken when the expiry job is queued) or historical analytics snapshots.
* **Modules:** `app/assets.py` (image validation, SVG sanitiser, encrypted asset store), widget methods and graph aggregation in `engine.py`/`analytics.py`, widget routes in `main.py`, `static/{common,charts,app,widget}.js`.
* **Dashboard `null` bug:** root cause was `Element.replaceChildren(...)` receiving a literal `null` (the optional analytics banner) which the DOM stringifies to the text "null". All content now goes through `RS.mount()`/`RS.h()`, which skip null/undefined/boolean children; a jsdom regression test covers the dashboard and other pages.
