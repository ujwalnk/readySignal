# CLAUDE.md — Ready Signal

Self-hosted cooldown manager: one card per account, absolute-timestamp timers, Slack Block Kit notifications with persistent retries, optional SQLite analytics, a compact credential-free iframe widget. Optimised for a Raspberry Pi 5 in Docker (NixOS host). No Node at runtime, no CDN.

## Stack & layout
FastAPI + a **single** uvicorn worker (scheduler and queue live in-process), asyncio, stdlib `sqlite3`, `cryptography`. Frontend: plain JS/CSS in `static/`, vendored uPlot in `static/vendor/uplot`.
- `app/main.py` — `create_app()`, routes, CSRF guard (`/api` only), security-header middleware, widget router (`/widget`, `/widget/api`; gated only by `widget_enabled`), icon route (`/assets/icons/{id}`), `run()` (catches `ConfigError`).
- `app/engine.py` — `Engine`: all state + logic (accounts, `start()`, `set_expiry()`, expiry processing, notification queue, scheduler `run()`, settings, icons, widget summary, graphs, env overrides). Everything mutating takes `self.lock` (then `self.nlock`). Module helpers: `resolve_local_time()`, `backup_blob()`.
- `app/models.py` — schemas, defaults, validators, **migrations** (`SCHEMA_VERSIONS`: config 3, accounts 2, state 1, notifications 1). `app/store.py` encrypted atomic files; `app/crypto.py`; `app/rotate.py`.
- `app/assets.py` — image validation/SVG sanitiser/`AssetStore`. `app/analytics.py` — SQLite (schema v2), `graphs()`. `app/notify.py` — templates, `validate_timezone/validate_webhook_url`, curl.
- `static/theme.css` (design tokens: light / dark / system, chart colours `--c1..--c4`), `app.css` (app), `widget.css` (widget); `common.js` (`RS.h`, `RS.mount`, `RS.icon/ibtn` inline SVG icons, formatting), `app.js` (dashboard/settings/stats), `charts.js`, `widget.js`.

## Run / test / quality
```bash
pip install -r requirements-dev.txt
SECRET=<>=16 chars> DATA_DIR=./data python -m app.main      # needs curl on PATH; http://127.0.0.1:8080
pytest -q                      # ~1.5 min; (cd tests/js && npm install) enables jsdom frontend tests, else they skip
ruff check . && ruff format --check . && mypy app
```
Tests mock curl (`conftest.curl`) and use an injectable `Clock`; never send real Slack messages. jsdom (`tests/js/harness.cjs`) does not do layout and has no named form-property access (use `form.elements.x` in app code).

## Docker & data
`docker compose up -d --build`; service `countdown`, image `ready-signal:2.1.0`. State is a **bind mount** `${DATA_HOST_DIR:-./data}:/data`, host dir owned by UID/GID **10001**, mode 700 (never "fix" permissions by deleting data; inspect the live compose file before changing mounts). Root FS read-only, caps dropped. `/healthz` is 503 until the scheduler runs. Env: `SECRET`/`SECRET_FILE` (required, ≥16 chars), `DATA_DIR`, `COOKIE_SECURE`, `PUBLIC_BASE_URL` (widget snippet only), `FORWARDED_ALLOW_IPS`, optional `READY_SIGNAL_TIMEZONE`, `SLACK_WEBHOOK_URL` (empty in compose by default; changes need `docker compose up -d`).
Files in `/data`: `config|accounts|state|notifications.json.enc`, `assets/icons/<id>.enc`, optional `analytics.sqlite3*` (the only unencrypted file), `*.premigrate-vN` backups.

## Persistence rules (do not break)
- Operational JSON is authoritative and always AES-256-GCM encrypted (scrypt from `SECRET`). Never write plaintext, never fall back to defaults on decrypt/validation failure. Rotate with `python -m app.rotate`. `GET /api/backup` returns only the encrypted files (`ready-signal-backup-encrypted.tar.gz`).
- Change schemas by bumping `SCHEMA_VERSIONS`, adding a `MIGRATIONS[name][old]` function and updating validators/defaults; add a v-old fixture test (see `tests/test_accounts_meta.py`, `tests/test_widget.py`).
- Commit pattern: deep copy → `_commit(name, new)` (writes first, then swaps memory).
- SQLite is analytics only; when statistics are off it must not be opened. Disabling deletes DB+sidecars and clears project/prompt text.

## Timers & scheduler
Timers store absolute UTC `ts_start/ts_expired`. `start()` on an available account starts fresh with the default duration; on an active timer it extends `ts_expired` by the full default duration with a new `expiry_id` (widget **Start/Extend** use this directly; the main site goes through `begin_cooldown()`). `set_expiry()` (`PUT /api/accounts/{id}/expiry`, body `completes_at` [+ `occurrence`] or `complete_now`) moves a *running* expiry: wall-clock time in the effective zone → UTC; a nonexistent local time is rejected, a time occurring twice raises `AmbiguousTime` (422 `code: ambiguous_time`) unless `occurrence` is `earlier|later` (UI wording never mentions DST); must be in the future (≤ 30 days); same value = no-op; new `expiry_id`, no reset counted; `complete_now` runs normal expiry processing. Editing duration/project/prompt never touches a timer, and expiry edits never touch the duration. Scheduler sleeps until the next deadline (min with `expiry_check_interval`), wakes via `engine.wake`. Expiry: enqueue job **before** flipping state; jobs de-duplicated per `expiry_id`; superseded expiries never notify.

## Start-cooldown dialog & reminders (main site only)
`POST /api/accounts/{id}/cooldown` → `Engine.begin_cooldown(mode, completes_at, occurrence, project, prompt)`: `mode=default` starts `now+default` or, if already cooling, extends `ts_expired` by the default (new `expiry_id`); `mode=at` sets the exact completion (effective zone; must be future, ≤30 days, past → explicit 422; default duration untouched; on an active timer it moves the expiry). Empty/whitespace `project`/`prompt` **keep** the saved values; only non-empty ones replace them (no new account). Metadata is committed first, then the timer; if the timer step fails `PartialFailure` (500, `code: partial`) says the metadata was saved. `POST /api/accounts/{id}/reminder` (`{time:"HH:MM"}`) → `schedule_reminder()`: next occurrence in the effective zone (tomorrow if passed), requires Slack enabled + webhook, stored as a `kind: "reminder"` job (`remind_at`, `next_attempt`, retries) in `notifications.json.enc`, delivered by the normal scheduler/queue with `notify.reminder_payload`; it never touches timers. The dialog asks for project/prompt in all three options with the same rule (empty keeps, non-empty replaces); for the reminder they are saved to the account when it is scheduled. Frontend: `cooldownDialog()` in `app.js` (radio options Use default duration / Set completion time / Remind me at, errors inline, submit disabled while busy). Dashboard Start cooldown opens it; the widget does not.

## Groups & favicon
Accounts have an optional `group` string (trimmed, ≤40 chars, no schema bump; missing == ""), edited in the account dialog (datalist of existing groups). The main dashboard renders one titled section per group (ungrouped last as "Ungrouped"; no headings when no account has a group); the widget ignores groups. `static/favicon.svg` (the header mark) is the favicon for the app and widget pages and is served at `/favicon.ico`.

## Configuration precedence
`READY_SIGNAL_TIMEZONE` / `SLACK_WEBHOOK_URL` are validated in `Engine.load()` (`ConfigError` → refuse to start; messages never contain the webhook). Non-empty env wins over the stored config **without modifying it**. Server code must use `engine.timezone_name` / `engine.effective_config()` (never `config["timezone"]` / `config["slack"]["webhook_url"]` directly) for delivery, previews, graphs. `public_config()` returns the effective zone, `env.{timezone,slack_webhook}` flags and `slack.webhook_source` (`environment|gui|none`); the webhook (stored or env) is never returned. `update_settings` ignores env-managed changes (lists them in `ignored`) while saving the rest. The UI disables those inputs with "Already set in environment variable" and never submits them.

## Notifications
`notify.render` parses the template JSON then expands `$placeholders` only in string values (single pass, `$$` literal). `curl` runs via an argv list; URL via inherited fd (`-K /dev/fd/N`), body on stdin. Success = exit 0 + HTTP 200 + body `ok`.

## Adding an API endpoint
`Engine` method (validate → lock → deep copy → `_commit`), raise `ValidationFailed/NotFound/Conflict` (422/404/409), expose on `api` (CSRF-protected) — or on `wapi` for the widget (no credential; return only what the widget needs). Add pytest coverage.

## Frontend conventions
- Build DOM only with `RS.h()`/`RS.mount()`/`RS.icon()`; **never pass possibly-null values to `replaceChildren`/`createTextNode`** (the old stray `null`). No `innerHTML`, no inline `style`/scripts (CSP `script-src/style-src 'self'`).
- Header: brand link `#/` (dashboard), optional Statistics **icon** link (`aria-label="Statistics"`, chart icon), `+` (opens `accountDialog()`), gear link to Settings. No Dashboard/Archived links; *Archived accounts* is a link in Settings. Dashboard body = cards + empty state only.
- Cards: available = prominent + **Start cooldown** (opens `cooldownDialog`) + Edit/Copy icons; cooling = muted, countdown, Edit only (no Extend, no Copy). Icon-only controls need `aria-label` + `title` (`RS.ibtn`).
- Account editor: "Cooldown completes at" (`datetime-local`, effective zone) appears only for running timers; it is sent only if changed; metadata and expiry are separate requests and a partial failure keeps the dialog open with an explicit message.
- Themes: `data-theme` rendered server-side on `<html>`; tokens only in `theme.css` (dark block exists twice: system-dark and forced dark). Settings → *Data management* holds the backup link (`/api/backup`).
- Widget: row height 34 px / 12 px padding (`widget.css --row`, mirrored by `WIDGET_ROW` in `app.js` for the embed height). Never add a credential, token, origin allow-list, `postMessage` handshake or `localStorage` to it. `/widget*` omits `X-Frame-Options`/`frame-ancestors`; all other responses keep `DENY`/`'none'`. Errors use the fixed overlay so rows never reflow.

## Pitfalls
- No extra workers, per-account tasks or per-second browser polling (countdowns are client-side; widget polls once a minute).
- Complete events are stamped at their expiry time, not when processed. Uploads always go through `validate_image`/`AssetStore`.
- Not covered by automated tests (check by eye after UI changes): real layout/pixel fit of the 280×150 widget, colours/contrast, uPlot canvas, iframe clipboard permission, Docker build.
- Logs never contain secrets/prompts; "authentication failed" = wrong `SECRET`; leftover `*.enc.new` + `rotation.commit` are recovered at start.
