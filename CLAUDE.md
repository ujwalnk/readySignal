# CLAUDE.md — Ready Signal

Self-hosted cooldown manager: one card per account, absolute-timestamp timers, Slack Block Kit notifications with persistent retries, optional SQLite analytics, iframe widget. Optimised for a Raspberry Pi 5 in Docker. No Node at runtime, no CDN.

## Stack & layout
FastAPI + a **single** uvicorn worker (scheduler and queue live in-process), asyncio, stdlib `sqlite3`, `cryptography`. Frontend: plain JS (`static/`), vendored uPlot in `static/vendor/uplot`.
- `app/main.py` — app factory `create_app()`, routes, CSRF guard (`/api` only), security-header middleware, widget router (`/widget`, `/widget/api`), icon route (`/assets/icons/{id}`).
- `app/engine.py` — `Engine`: all state + logic (accounts, `start()`, expiry processing, notification queue, scheduler `run()`, settings, icons, widget token/summary, graphs). Everything mutating takes `self.lock` (then `self.nlock`).
- `app/models.py` — schemas, defaults, validators, **migrations** (`SCHEMA_VERSIONS`: config 2, accounts 2, state 1, notifications 1). `app/store.py` encrypted atomic files; `app/crypto.py`; `app/rotate.py`.
- `app/assets.py` — image validation/SVG sanitiser/`AssetStore`. `app/analytics.py` — SQLite (schema v2), `REPORTS` SQL, `graphs()`. `app/notify.py` — templates + curl.
- `static/common.js` (`RS.h`, `RS.mount`, formatting), `app.js` (dashboard/settings/stats), `charts.js` (uPlot wrapper), `widget.js`.

## Run / test / quality
```bash
pip install -r requirements-dev.txt
SECRET=<>=16 chars> DATA_DIR=./data python -m app.main      # needs curl on PATH; http://127.0.0.1:8080
pytest -q                      # ~1 min; (cd tests/js && npm install) enables the jsdom frontend tests, else they skip
ruff check . && ruff format --check . && mypy app
```
Tests mock curl (`conftest.curl` fixture) and use an injectable `Clock`; never send real Slack messages.

## Docker & data
`docker compose up -d --build`; service `countdown`, image `ready-signal`. State is a **bind mount** `${DATA_HOST_DIR:-./data}:/data`, host dir owned by UID/GID **10001**, mode 700 (never "fix" permissions by deleting data). Root FS read-only, caps dropped. `/healthz` is 503 until the scheduler runs. Env: `SECRET`/`SECRET_FILE` (required, ≥16 chars), `DATA_DIR`, `COOKIE_SECURE`, `PUBLIC_BASE_URL` (widget snippet only), `FORWARDED_ALLOW_IPS`.
Files in `/data`: `config|accounts|state|notifications.json.enc`, `assets/icons/<id>.enc`, optional `analytics.sqlite3*` (the only unencrypted file), `*.premigrate-vN` backups.

## Persistence rules (do not break)
- Operational JSON is authoritative and always AES-256-GCM encrypted (scrypt from `SECRET`). Never write plaintext, never fall back to defaults on decrypt/validation failure. `SECRET` is unrecoverable if lost; rotate with `python -m app.rotate` (covers JSON + images).
- Change schemas by bumping `SCHEMA_VERSIONS`, adding a `MIGRATIONS[name][old]` function and updating validators/defaults; add a v-old fixture test (see `tests/test_accounts_meta.py`).
- Commit pattern: build a deep copy → `_commit(name, new)` (writes first, then swaps memory).
- SQLite is analytics only; when statistics are off it must not be opened (a test patches `sqlite3.connect`). Disabling deletes DB+sidecars and clears project/prompt text.

## Timers & scheduler
Timers store absolute UTC `ts_start/ts_expired`; `start()` on an active timer extends `ts_expired` by the full configured duration and issues a new `expiry_id` (old expiry never notifies); after expiry it starts fresh. Editing duration/project/prompt never touches a timer. Scheduler (`Engine.run`) sleeps until the next deadline (min with `expiry_check_interval`, default 60 s), wakes via `engine.wake`; first tick = restart recovery. Expiry: enqueue job (snapshot of account context) **before** flipping state; jobs are de-duplicated per `expiry_id`. Delivery persists the attempt count before calling curl; duplicates after a crash are possible and documented.

## Notifications
`notify.render` parses the template JSON then expands `$placeholders` only in string values (single pass, `$$` literal). `curl` is run via an argv list; URL via inherited fd (`-K /dev/fd/N`), body on stdin. Success = exit 0 + HTTP 200 + body `ok`.

## Adding an API endpoint
Add the logic as an `Engine` method (validate → lock → deep copy → `_commit`), raise `ValidationFailed/NotFound/Conflict` (mapped to 422/404/409), expose it in `create_app()` on `api` (CSRF-protected) — or on `wapi` for the widget (Bearer auth, return only what the widget needs). Add pytest coverage; mutating routes must keep the CSRF header.

## Frontend conventions
- Build DOM only with `RS.h()` and replace content only with `RS.mount()`; **never pass possibly-null values to `replaceChildren`/`createTextNode`** (that caused the stray `null`). No `innerHTML`, no inline `style` attributes or inline scripts (CSP is `script-src/style-src 'self'`; setting `el.style.x` from JS is fine).
- Themes: `data-theme` on `<html>` is rendered server-side (no flash); CSS variables in `app.css` (incl. chart colours `--c1..--c4`) for system/light/dark; `applyTheme()` rebuilds charts.
- Stats: server returns chart-ready aggregates from `Analytics.graphs()`; `app.js::renderCharts` builds six charts via `RS.charts.chart(host, spec)` (`mode`: line/bar/stack). Always show empty states instead of zero-filled charts.
- Images: `iconEl()` shows the server asset (`icon_url`) with emoji fallback; uploads are raw-body `PUT /api/accounts/{id}/icon`; never inline uploaded SVG.
- Widget: bearer credential via header only (typed once → frame `localStorage`, or parent `postMessage` from the configured origin); no CSRF by design; `frame-ancestors` = configured origin.

## Pitfalls
- Don't add worker processes or per-account tasks. Don't poll per second from the browser (countdown is client-side from timestamps).
- Complete events are stamped at their expiry time, not when processed.
- Uploaded files/ids: always go through `validate_image` and `AssetStore` (32-hex ids); don't serve files by user-supplied names.
- Real-browser rendering (uPlot canvas, CSP, iframe clipboard) is not covered by automated tests; check manually after UI changes.
- Debugging: logs never contain secrets/prompts; `docker compose logs`; "authentication failed" = wrong `SECRET`; leftover `*.enc.new` + `rotation.commit` are recovered at start.
