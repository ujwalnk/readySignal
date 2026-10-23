# Operations

## Backup / restore
* UI: Settings → **Data management → Encryption and recovery → Download encrypted backup** (file `ready-signal-backup-encrypted.tar.gz`), or `GET /api/backup` – a tar.gz of the `*.json.enc` files and `assets/icons/*.enc` images (all still encrypted; there is deliberately no plaintext export).
* Bind-mount copy (stop the container first): `sudo tar czf ready-signal-data.tgz -C ./data .` (includes the optional SQLite file, which is *not* encrypted).
* Restore: `docker compose down`, extract into the data directory (owner uid/gid 10001, files 600, dirs 700), `docker compose up -d` with the **same SECRET** the backup was made with.

## Environment-managed settings
Two optional variables (see `.env.example`; both are passed through by `docker-compose.yml` and are **empty by default**):

| Variable | Effect while non-empty |
|---|---|
| `READY_SIGNAL_TIMEZONE` | IANA name (e.g. `Asia/Kolkata`). Authoritative time zone for dashboard times, the expiry editor, notification timestamps and statistics. Settings shows the field disabled with "Already set in environment variable". |
| `SLACK_WEBHOOK_URL` | `https://` Slack incoming-webhook URL, used for notifications and test messages. Settings shows the field disabled with the same note, never the URL, and hides *Remove webhook*. The Slack enable switch, template, preview, test and retry settings stay editable. |

Unset or empty means the GUI-managed value (stored encrypted) is used. Environment values are read **at start-up**: edit `.env` and run `docker compose up -d` (recreates the container; `restart` alone does not reload it). An invalid value stops the start-up with a clear message (`READY_SIGNAL_TIMEZONE is not a valid IANA time zone name…` / `SLACK_WEBHOOK_URL is not a valid https:// Slack incoming-webhook URL`; the webhook value is never printed). The backend ignores GUI attempts to change an environment-managed setting (the response lists it under `ignored`); the stored value is left untouched and becomes effective again if the variable is removed.

## Recovery
| Symptom | Action |
|---|---|
| "refusing to start: SECRET is missing…" | supply `SECRET` (≥16 chars) |
| "authentication failed: wrong SECRET or file was modified" | use the secret the data was written with, or restore a backup. Files are never auto-replaced. |
| a single file corrupted | restore that file from a backup (`*.json.enc`), keeping its siblings consistent where possible |
| secret lost | data is unrecoverable by design; empty the data directory to start over (only after you are sure the secret is gone) |
| `rotation.commit` / `*.enc.new` present | automatic roll-forward/back at next start |
| stale `analytics_purge_pending` | startup retries the deletion |

## Upgrades
`docker compose build && docker compose up -d`. Schema migrations run automatically at startup and leave an encrypted `*.premigrate-vN` copy. Take a backup first. Downgrading across a schema bump is refused.

## Notifications
* Slack incoming webhooks only (`https://…`, enforced via `curl --proto =https --tlsv1.2`). They post to the channel chosen when the webhook was created.
* Delivery: `curl` via `subprocess` with an argv list, no shell. The **webhook URL is passed through an inherited pipe (`-K /dev/fd/N`) and the JSON on stdin**, so neither appears in argv/`ps`. Residual exposure: root/same-uid processes can inspect the process's fds/memory briefly; the URL also lives decrypted in app memory.
* Success = exit code 0 **and** HTTP 200 **and** body `ok`. 400/403/404/410 are permanent; 429 honours `Retry-After` (≤1 h); everything else retries every `retry_interval` s up to `max_attempts` (the attempt counter is persisted *before* sending). Final failures are shown under Settings → Delivery status with a Retry button.
* Best effort: if Slack accepts a message and the process dies before the acknowledgement is saved, the job is retried and a **duplicate may be delivered**. Exactly-once is not achievable here.
* The full continuation prompt is sent to Slack.
* Template: a complete Slack JSON payload (`text` fallback required, optional `blocks`). JSON is parsed first; placeholders (`$prompt $project $account $icon $ts_start $ts_expired $duration $reset_count`, also `${name}`) are expanded **only inside string values, in one pass**; `$$` is a literal `$`; unknown `$names` are left as written; JSON keys are not expanded.
* Timestamps: stored UTC, rendered with the configured IANA zone and Python `strftime` formats (default `%Y-%m-%d %H:%M:%S %Z`).

## Testing
`pip install -r requirements-dev.txt && pytest -q && ruff check . && mypy app`. Subprocess/HTTP is mocked; no real Slack message is ever sent by the suite. Frontend tests run the real `static/*.js` in jsdom (`cd tests/js && npm install`, dev-only; skipped if Node is absent).

## Troubleshooting
Check `docker compose logs`. Logs contain no secrets or payloads. `curl executable not found` → install curl (it is in the image). Analytics errors show as a banner and never block timers.

## Known limitations
* No built-in user authentication (private network/Tailscale model).
* Front-end behaviour is tested in jsdom with a stubbed uPlot; real-browser rendering (canvas, CSP enforcement, real clipboard permissions in iframes, touch tooltips) has not been verified automatically.
* Docker image build/health check not executed in the authoring environment.
* SQLite analytics file is not encrypted.

## Docker bind mounts and permissions
Compose now stores state in a **bind mount** (`${DATA_HOST_DIR:-./data}` → `/data`), not a Compose-managed named volume. The container runs as UID/GID **10001**, so the host directory must be owned by that user:
```bash
sudo install -d -o 10001 -g 10001 -m 700 ./data      # or your DATA_HOST_DIR
docker compose up -d --build
```
On NixOS the same applies (create the directory with `systemd.tmpfiles.rules = [ "d /srv/ready-signal 0700 10001 10001 -" ];` and set `DATA_HOST_DIR=/srv/ready-signal`). A `Permission denied` on `/data` means the host owner/mode is wrong: fix it with `chown -R 10001:10001` / `chmod 700` — do **not** delete data to "fix" permissions. Bind mounts are visible and editable from the host; named volumes live under Docker's own directory — the container behaves the same, only the location differs.

### Migrating from the named volume (`countdown-data`, 1.x)
```bash
docker compose down
sudo install -d -o 10001 -g 10001 -m 700 ./data
sudo cp -a "$(docker volume inspect -f '{{ .Mountpoint }}' <project>_countdown-data)/." ./data/
sudo chown -R 10001:10001 ./data
docker compose up -d --build        # same SECRET; the old volume is left untouched
```
If your live deployment already bind-mounts a directory, keep your existing mapping: only the image tag, environment variables and optional `PUBLIC_BASE_URL`/`FORWARDED_ALLOW_IPS` matter.

## Migrations (2.1.0)
`config` moves to schema v3: the widget's `allowed_origin`, `token_hash` and `token_created_at` are removed; accounts, timers, analytics and notification jobs are untouched (an encrypted `*.premigrate-v2` copy of `config` is written first). Rolling back to 2.0 is not supported; restore a pre-upgrade backup instead.

## Migrations (2.0.0)
`config` and `accounts` move to schema v2 on first start (adds `widget` settings and `icon_asset`); `state` and `notifications` are unchanged. Account IDs, expiry timestamps, durations, reset counts and pending jobs are preserved; encrypted `*.premigrate-v1` copies of the changed files are written first. The SQLite file (if present) gains a `prompt` column. Rolling back to 1.x is not supported — restore the pre-upgrade backup instead. If decryption or migration fails nothing is overwritten.

## Secret rotation
Stop the app, then `SECRET_OLD=… SECRET_NEW=… DATA_DIR=./data python -m app.rotate` (in Docker: `docker compose run --rm -e SECRET_OLD -e SECRET_NEW countdown python -m app.rotate`). Documents **and image assets** are re-encrypted all-or-nothing (staged `*.enc.new`, verified, committed via `rotation.commit`).

## Health check
`/healthz` returns 200 only when the scheduler task is running (503 `starting` otherwise); Docker's HEALTHCHECK uses it.
