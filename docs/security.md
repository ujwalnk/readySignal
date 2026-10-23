# Security

* **Network model:** no built-in login. Compose binds `127.0.0.1:8080`. Reach it through Tailscale (`tailscale serve`), an SSH tunnel, or a reverse proxy that adds authentication (e.g. basic auth / OIDC proxy) and TLS; then set `COOKIE_SECURE=1`. Do not expose it to the internet as-is.
* **CSRF:** all state-changing `/api` requests need an `X-CSRF-Token` header equal to a `SameSite=Strict`, HttpOnly cookie (double submit), and a matching `Origin` when present.
* **Input:** server-side validation of all fields; UI builds DOM through `h()`/`mount()` using text nodes (no HTML injection); templates are data (no code execution); curl is run with an argv list, no shell, `--proto =https`.
* **Secrets:** webhook URL masked in every API response; never logged; sanitised out of curl errors. `SECRET` never logged/returned.
* **Container:** non-root uid 10001, read-only root FS, all caps dropped, no-new-privileges, memory/CPU/pids limits, `/data` is `0700`, files `0600`.
* No debug mode, OpenAPI/Docs endpoints disabled, `server` header off.
* **Headers:** every response has `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The app sends `X-Frame-Options: DENY` and a CSP (`script-src 'self'`, `style-src 'self'`, `frame-ancestors 'none'`). Only `/widget*` is frameable, and only by the configured parent origin. Clickjacking protection is never disabled globally.
* **Uploads:** see [images.md](images.md). **Widget:** see [widget.md](widget.md).
* **Reverse proxy:** uvicorn trusts `X-Forwarded-*` only from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`). In Docker set it to your proxy's address (not `*`).
* **Residual risks:** no user login on the main UI (private-network model); the widget credential is a bearer token (anyone holding it can start/extend the shared accounts and read their prompts if copying is enabled — rotate/revoke in Settings); icon URLs are unauthenticated but unguessable (128-bit ids); the optional SQLite file and the 0.5 s-window process memory hold plaintext; no rate limiting on the widget credential (256-bit random, so brute force is infeasible).
