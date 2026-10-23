# Embeddable widget (iframe)

A compact cooldown view for a dashboard you already use (designed for roughly **2.5 × 1.5 inches**, i.e. about **280 × 150 CSS px**). One 34 px row per account: icon, name, project (optional), status or countdown, and small icon actions. It uses the same timer service as the main UI (persistence, locking, analytics, notifications).

**There is no credential.** The widget loads and works as soon as it is enabled. It is meant for a deployment on a private network (Tailscale, LAN, VPN): anyone who can open the Ready Signal address can open the widget, start or extend its accounts and, if prompt copying is allowed, read their prompts. Do not expose Ready Signal on the public internet (the main UI has no login either).

## Enable and embed
1. Settings → *Embeddable widget*: tick **Enable**, choose the accounts, whether to show project names and whether prompt copying is allowed, Save. (Settings shows the live preview link and the embed code.)
2. Paste the embed code into your dashboard (set `PUBLIC_BASE_URL` so the snippet shows your real address):
```html
<iframe src="https://signal.example.ts.net/widget" title="Ready Signal" allow="clipboard-write"
        width="280" height="150" style="border:0" loading="lazy"></iframe>
```
`allow="clipboard-write"` is required for **Copy prompt** inside a frame. The browser also only allows the Clipboard API on HTTPS/localhost pages; otherwise the widget falls back to the legacy copy command and finally to a small selectable text box.

**Height:** `12 px + 34 px × accounts shown` (4 accounts → 148 px, so 150 fits; 5 accounts → 182 px). The snippet in Settings computes this for you. The widget never scrolls and never hides accounts; if you add more accounts, make the iframe taller.

## What each row shows and does
| State | Shown | Actions |
|---|---|---|
| Available | `Ready` | **Start cooldown** (default duration) and **Copy prompt** |
| Cooling down | live `HH:MM:SS` countdown | **Extend** (adds the configured default duration to the current expiry) |

Actions are direct, with no dialog. Errors and the clipboard fallback appear in a transient strip over the bottom edge, so the layout never shifts. Countdowns use absolute timestamps and tick locally; the widget re-reads the summary once a minute, after an action and when a countdown ends. Copy prompt is offered only while the account is available.

## Endpoints
| Method & path | Returns |
|---|---|
| `GET /widget` | HTML page (404 unless enabled). No `X-Frame-Options`, no `frame-ancestors` restriction |
| `GET /widget/api/summary` | `{now, show_project, allow_prompt_copy, accounts:[{id,name,icon,icon_url,status,ts_start,ts_expired,duration_seconds,has_prompt,project?}]}` |
| `POST /widget/api/accounts/{id}/start` | `{id,status,ts_start,ts_expired,event:"start"\|"extend"}` |
| `GET /widget/api/accounts/{id}/prompt` | `{prompt}` (404 if copying disabled / account not shared) |

All return `404` while the widget is disabled; otherwise `404/409/422` as usual. No CORS headers are sent (the page and API share one origin) and no CSRF token is needed for widget requests. Nothing else (settings, webhook, paths, encryption info) is exposed.

## Security model
* Only `/widget*` may be framed; every other page keeps `X-Frame-Options: DENY` and `frame-ancestors 'none'`.
* The widget has no token, origin allow-list or login. Its reach is limited to the accounts you select and to start/extend/prompt operations.
* Upgrading from 2.0: the old token hash, creation time and allowed-origin settings are dropped automatically (config schema v3); everything else is untouched. Remove any `postMessage` credential code from your parent page.

## Troubleshooting
*Blank/blocked frame*: widget disabled (404), or a parent CSP `frame-src` that excludes this host. *Copy prompt shows a text box*: add `allow="clipboard-write"` and serve over HTTPS. *Mixed content*: serve the parent and the widget over the same scheme. *Countdown off*: it follows the server clock via the `now` field.
