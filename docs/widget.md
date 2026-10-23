# Embeddable widget (iframe)

Compact cooldown view for an existing dashboard: image, name, status, countdown, project (optional), Start/Extend and Copy prompt (optional). It uses the same timer service as the main UI (persistence, locking, analytics, notifications).

## Enable
1. Settings → *Embeddable widget*: tick **Enable**, enter the **exact parent-dashboard origin** (e.g. `https://dash.example.com`; scheme+host[+port], no path, no wildcard), choose accounts / project / prompt-copy options, Save.
2. **Generate credential** and copy it (shown once; only its SHA-256 is stored). *Rotate* replaces it, *Revoke* disables it.
3. Embed (set `PUBLIC_BASE_URL` so the snippet shows your address):
```html
<iframe src="https://signal.example.ts.net/widget" title="Ready Signal" allow="clipboard-write"
        width="100%" height="320" style="border:0" loading="lazy"></iframe>
```
4. Give the widget its credential **without a URL**: either paste it into the widget once (kept in that frame's `localStorage`), or let the parent send it:
```js
const f = document.querySelector('iframe'), ORIGIN = 'https://signal.example.ts.net';
window.addEventListener('message', e => {
  if (e.origin === ORIGIN && e.data?.type === 'ready-signal:ready')
    f.contentWindow.postMessage({type: 'ready-signal:credential', credential: '<token>'}, ORIGIN);
});
```
The widget ignores messages not sent by its parent window from the authorised origin.

## Endpoints
| Method & path | Auth | Returns |
|---|---|---|
| `GET /widget` | none (404 unless enabled) | HTML page; CSP `frame-ancestors <origin>` |
| `GET /widget/api/summary` | `Authorization: Bearer <credential>` | `{now, show_project, allow_prompt_copy, accounts:[{id,name,icon,icon_url,status,ts_start,ts_expired,duration_seconds,has_prompt,project?}]}` |
| `POST /widget/api/accounts/{id}/start` | Bearer | `{id,status,ts_start,ts_expired,event:"start"\|"extend"}` |
| `GET /widget/api/accounts/{id}/prompt` | Bearer | `{prompt}` (404 if copying disabled / account not shared) |
Errors: `401` (missing/invalid/revoked/disabled, `WWW-Authenticate: Bearer`), `404`, `409`, `422`. Nothing else (settings, webhook, paths, encryption info) is exposed. No CORS headers are sent — the page and API share one origin, so none are needed.

## Security model
* Framing is restricted by CSP `frame-ancestors` to the configured origin; the rest of the app stays `DENY`/`'none'`.
* Authorisation is the scoped bearer credential (can only list/start/extend the chosen accounts and read their prompts when allowed). `Origin` is never used as authentication; credentials are never in URLs, API responses or logs.
* No CSRF token is required for widget actions by design (bearer header instead of cookies).
* Clipboard: uses the Clipboard API; inside an iframe the parent must grant `allow="clipboard-write"`. If blocked, the widget shows the prompt in a selectable box.

## Troubleshooting
*Blank/blocked frame*: origin mismatch (compare with the browser's address bar of the parent page, incl. port), widget disabled, or a parent CSP `frame-src` that excludes this host. *"Enter the widget credential" again*: credential revoked/rotated or storage blocked (third-party storage partitioning) → use the postMessage handshake. *Mixed content*: serve both over HTTPS. *Clock/countdown off*: it follows the server clock via the `now` field.
