# Account images

Accounts keep a permanent identity; an image is optional. Without one the card shows the account's emoji (default ⏳).

* **Formats:** SVG, PNG, WebP, JPEG. Max 512 KB (SVG 256 KB), ≤ 4096 px per side. API: `PUT /api/accounts/{id}/icon` with the raw bytes as body (needs the CSRF header), `DELETE` to remove. The file picker previews before saving.
* **Validation is by content**, never by filename or declared MIME type: magic bytes, header dimensions and terminators for raster files. The `Content-Length` and the streamed body are both capped.
* **SVG** is rebuilt from an allow-list: only basic shapes/gradients/text; no `script`, `style`, `foreignObject`, `image`, `animate*`, `a`; no `on*` attributes; no external references (`href` must be `#fragment`, `url()` only to `#fragment`); DOCTYPE/ENTITY rejected. It is only ever shown through `<img>` (never injected into the DOM) and served with `nosniff` and `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox`.
* **Storage:** `data/assets/icons/<32-hex id>.enc`, server-generated ids (no user filenames, no traversal). Each file is encrypted with the same AES-256-GCM envelope as the JSON documents (content type + bytes), so the encryption boundary holds. Accounts store only `{id, content_type}`. Included in backups and secret rotation. Replacing/removing deletes the old file; orphans from a crash are removed at start. Missing/unreferenced ids return 404 and the UI falls back to the emoji.
* **Access:** `GET /assets/icons/{id}` is not authenticated (ids are unguessable; icons are not secret) and is cached privately/immutably.
