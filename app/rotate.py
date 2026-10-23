"""Offline SECRET rotation: ``SECRET_OLD=... SECRET_NEW=... python -m app.rotate``.

Stop the application first. The procedure is all-or-nothing:
stage every file re-encrypted as ``*.enc.new`` -> verify with the new secret ->
write ``rotation.commit`` -> rename into place -> remove marker.
Interrupted runs are recovered by :func:`app.store.recover_rotation`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .crypto import Keyring, decrypt, encrypt
from .engine import ASSETS_SUBDIR, FILE_NAMES
from .models import DEFAULTS, MIGRATIONS, SCHEMA_VERSIONS, VALIDATORS
from .store import EncryptedJSONFile, StoreError, _fsync_dir, atomic_write, recover_rotation


def _file(data_dir: Path, name: str, keyring: Keyring) -> EncryptedJSONFile:
    return EncryptedJSONFile(
        data_dir / FILE_NAMES[name], keyring, SCHEMA_VERSIONS[name], DEFAULTS[name], VALIDATORS[name], MIGRATIONS[name]
    )


def rotate(data_dir: Path, old_secret: str, new_secret: str) -> list[str]:
    if old_secret == new_secret:
        raise StoreError("old and new secrets are identical")
    recover_rotation(data_dir)
    old_kr, new_kr = Keyring(old_secret), Keyring(new_secret)
    plain: dict[str, dict] = {}
    for name in FILE_NAMES:
        if (data_dir / FILE_NAMES[name]).exists():
            plain[name] = _file(data_dir, name, old_kr).load()
    if not plain:
        raise StoreError("no encrypted data files found")
    asset_files = sorted((data_dir / ASSETS_SUBDIR).glob("*.enc")) if (data_dir / ASSETS_SUBDIR).is_dir() else []
    asset_plain = {
        p: decrypt(old_kr, p.read_bytes())[0] for p in asset_files
    }  # fails (nothing staged) on a wrong secret
    new_salt = os.urandom(16)
    staged: list[Path] = []
    try:
        for src, body in asset_plain.items():
            dest = src.with_name(src.name + ".new")
            atomic_write(dest, encrypt(new_kr, body, new_salt))
            staged.append(dest)
        for src, body in asset_plain.items():
            if decrypt(new_kr, src.with_name(src.name + ".new").read_bytes())[0] != body:
                raise StoreError(f"verification mismatch for {src.name}")
        for name, data in plain.items():
            dest = data_dir / (FILE_NAMES[name] + ".new")
            _file(data_dir, name, new_kr).save(data, dest)
            staged.append(dest)
        for name, data in plain.items():  # verify before committing
            check = EncryptedJSONFile(
                data_dir / (FILE_NAMES[name] + ".new"),
                new_kr,
                SCHEMA_VERSIONS[name],
                DEFAULTS[name],
                VALIDATORS[name],
                MIGRATIONS[name],
            ).load()
            if check != data:
                raise StoreError(f"verification mismatch for {FILE_NAMES[name]}")
    except Exception:
        for path in staged:
            path.unlink(missing_ok=True)
        raise
    marker = data_dir / "rotation.commit"
    marker.write_bytes(b"")
    _fsync_dir(data_dir)
    recover_rotation(data_dir)  # rolls forward: renames .new files, removes marker
    return sorted([FILE_NAMES[n] for n in plain] + [p.name for p in asset_files])


def main() -> int:
    old, new = os.environ.get("SECRET_OLD"), os.environ.get("SECRET_NEW")
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    try:
        if not old or not new:
            raise StoreError("set SECRET_OLD and SECRET_NEW environment variables")
        Keyring(new)  # validates strength
        done = rotate(data_dir, old, new)
    except Exception as exc:  # report sanitised message only
        print(f"ROTATION FAILED: {exc}\nNo file was changed.", file=sys.stderr)
        return 1
    print(f"Rotation succeeded for: {', '.join(done)}. Start the app with the new SECRET.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
