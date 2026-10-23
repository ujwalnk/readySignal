# Ready Signal

Self-hosted cooldown timers for AI/cloud accounts: per-account duration, start, editable completion time, project + continuation prompt, Slack Block Kit notification when a cooldown ends. FastAPI + one Uvicorn worker, encrypted JSON state, optional SQLite analytics, no Node, no CDN.

> **A cooldown is a reminder, not a measurement.** The app never talks to the external services; "duration" is the cooldown *you configured*, not how long you actually used a service.

## Quick start (Docker)
```bash
echo "SECRET=$(openssl rand -base64 36)" > .env && chmod 600 .env   # back this value up somewhere safe
sudo install -d -o 10001 -g 10001 -m 700 ./data     # bind-mounted state; owner must match the container UID
docker compose up -d --build
# open http://127.0.0.1:8080  (see docs/security.md for Tailscale / reverse-proxy access)
```
Without Docker: `pip install -r requirements.txt && SECRET=... DATA_DIR=./data python -m app.main` (needs `curl` on PATH).

`SECRET` (≥16 chars) is mandatory. The app **refuses to start** if it is missing/short, or if any existing data file cannot be authenticated/decrypted. Losing the only valid secret makes the data **unrecoverable**.

## Layout
```
app/crypto.py    AES-256-GCM envelope + scrypt keyring      app/engine.py    timers, queue, scheduler, settings, widget, icons
app/store.py     atomic encrypted JSON files, recovery      app/notify.py    templates, curl delivery, retry classification
app/models.py    schemas/defaults/validators/migrations     app/analytics.py optional SQLite, graph aggregation, reporting SQL
app/assets.py    image validation, SVG sanitiser, assets    app/main.py      FastAPI routes, CSRF, headers, widget router
app/rotate.py    offline secret rotation CLI                static/           theme.css app.css widget.css, common/app/charts/widget.js, vendor/uplot (no build)
tests/           pytest (curl mocked) + jsdom frontend tests docs/            architecture, encryption, operations, analytics, security, images, widget
```

## What's in 2.1
* **Redesigned UI** (cool slate/ink palette, teal accent; System/Light/Dark). The header holds the Ready Signal brand (click = dashboard), *Statistics* (when enabled), a `+` icon (add account) and a gear (Settings). *Archived accounts* now lives in Settings.
* **Compact cards:** icon, name, project, status/countdown, and small Edit / Copy-prompt icons. Available cards are prominent with a one-click **Start cooldown** (default duration, no dialog); cooling cards are muted, show the countdown and have **no Extend button**: to change when a running cooldown ends, open *Edit* and set **Cooldown completes at**.
* **Compact credential-free widget** (~280 × 150 px) with direct Start/Extend and Copy prompt → [docs/widget.md](docs/widget.md). Intended for a private network.
* **Environment overrides** `READY_SIGNAL_TIMEZONE` and `SLACK_WEBHOOK_URL` (empty by default) → [docs/operations.md](docs/operations.md#environment-managed-settings).
* **Settings → Data management:** *Download encrypted backup* has its own section.
* From 2.0: accounts are permanent identities with editable project/prompt; custom images ([docs/images.md](docs/images.md)); graph statistics ([docs/analytics.md](docs/analytics.md)).

## Behaviour summary
* Timers store **absolute UTC expiry timestamps**; nothing is persisted per second. The browser recomputes `expiry − now` every second from the timestamp (correct after sleep/suspend) and never polls the server.
* **Start** on an available account → fresh cooldown from now. The widget's **Extend** (and the API start on a cooling account) → `expiry += full configured duration` (3 h left + 5 h → 8 h). Each counts as a reset; scheduler checks and retries do not. **Editing the completion time** in the account editor moves the running expiry (in the effective time zone) without counting a reset.
* Editing duration/project/prompt never changes an active expiry, and editing the completion time never changes the default duration.
* One asyncio scheduler for *all* accounts: sleeps until the next deadline (expiry or notification retry), is woken by `asyncio.Event` on changes, with a configurable reconciliation interval (default 60 s) as a safety net. No per-account tasks, no per-second backend polling.
* Expired timers become *available*, and (if Slack is enabled) a persistent job is queued **before** the state flips, so a crash re-runs safely; jobs are de-duplicated per expiry ID. An extension supersedes the old expiry, which never notifies.
* Missed expiries and pending jobs are recovered on startup (a notification cannot be sent while the container is down; it is sent afterwards).
* Settings → everything is configurable in the UI (theme System/Light/Dark, time zone, timestamp formats, intervals, retries, timeouts, Slack, template editor with validate/preview/test, statistics toggle, encrypted backup download); the time zone and Slack webhook can instead be pinned by environment variables.

## Verification status
Run `pip install -r requirements-dev.txt && pytest && ruff check . && mypy app` yourself. Frontend tests need `cd tests/js && npm install` (dev-only; skipped if absent) and run the real `static/*.js` in jsdom, which does **not** compute layout: pixel-level fit of the widget, colours and real-browser clipboard behaviour must be checked by eye. The Docker image has not been built in the authoring environment (no Docker daemon).

More: [images](docs/images.md) · [widget](docs/widget.md) · [architecture](docs/architecture.md) · [encryption](docs/encryption.md) · [operations](docs/operations.md) · [analytics](docs/analytics.md) · [security](docs/security.md)
