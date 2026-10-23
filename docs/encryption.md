# Encryption

Every persistent operational JSON file is an envelope (UTF-8 JSON):
```json
{"v":1,"alg":"AES-256-GCM",
 "kdf":{"name":"scrypt","n":32768,"r":8,"p":1,"length":32,"salt":"<b64 16B>"},
 "nonce":"<b64 12B>","ct":"<b64 ciphertext||16B GCM tag>"}
```
* Key = scrypt(`SECRET`, salt) (N=2^15, r=8, p=1 ≈ 32 MiB). `SECRET` is never used as a key directly. KDF parameters are range-checked on read so a tampered file cannot demand unbounded memory.
* A fresh random 96-bit nonce per write. The per-file salt is generated once and reused so the derived key can be cached (random nonces are safe far beyond this app's write volume).
* AAD = canonical JSON of `v, alg, kdf, nonce`; modifying any envelope field or the ciphertext fails authentication.
* Authentication failure ⇒ `StoreError`, startup refused, **file untouched**. Missing files are the only case where defaults are created.
* Writes: validate → serialise in memory → encrypt → temp file *in the same directory* (`0600`) → fsync → `os.replace` → fsync directory. Failure removes the temp file and keeps the previous file. No plaintext temp files, backups or dumps exist.
* `SECRET` stays out of the image, repo, logs, API responses and argv. Env vars can be read by root / same-uid processes (`/proc/<pid>/environ`) and by `docker inspect`; use `SECRET_FILE` + a Compose secret if you want to avoid the latter. **Encryption protects data at rest, not decrypted data in process memory nor a privileged host admin.**

## Rotating the secret (offline, all-or-nothing)
```bash
docker compose stop
docker compose run --rm --no-deps -e SECRET_OLD=... -e SECRET_NEW=... countdown python -m app.rotate
# then update SECRET in .env and: docker compose up -d
```
Procedure: decrypt+validate all files with the old secret → write `*.enc.new` encrypted with the new secret → re-read and verify each → create `rotation.commit` → rename into place → delete marker. Interrupted before the marker: staged files are discarded at next start (roll back). Interrupted after: the next start/rotate finishes the renames (roll forward). On any failure it reports `ROTATION FAILED` and changes nothing. (`SECRET_OLD/NEW` are passed via environment, not argv.)

## Image assets
`data/assets/icons/<id>.enc` use the same envelope (plaintext = `content-type\n` + image bytes; the accounts file's KDF salt is reused, nonces are fresh). They are covered by backups and by `app.rotate`. Only the optional `analytics.sqlite3` remains unencrypted.
