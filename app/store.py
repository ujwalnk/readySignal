"""Encrypted JSON file persistence with atomic replacement."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .crypto import CryptoError, Keyring, decrypt, encrypt

log = logging.getLogger("countdown.store")


class StoreError(Exception):
    """Sanitised persistence failure. The on-disk file is never modified when raised."""


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, blob: bytes) -> None:
    """Write ``blob`` to a temp file in the same directory, fsync, then os.replace."""
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except OSError as exc:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise StoreError(f"could not write {path.name}: {exc.strerror or 'I/O error'}") from exc


class EncryptedJSONFile:
    def __init__(
        self,
        path: Path,
        keyring: Keyring,
        schema_version: int,
        default: Callable[[], dict[str, Any]],
        validate: Callable[[Any], None],
        migrations: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        self.path = Path(path)
        self.keyring = keyring
        self.schema_version = schema_version
        self.default = default
        self.validate = validate
        self.migrations = migrations or {}
        self._salt: bytes | None = None
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        """Load and authenticate. Creates defaults ONLY if the file does not exist."""
        with self._lock:
            if not self.path.exists():
                data = self.default()
                self._save_locked(data)
                return data
            try:
                blob = self.path.read_bytes()
            except OSError as exc:
                raise StoreError(f"cannot read {self.path.name}: {exc.strerror}") from exc
            try:
                plaintext, self._salt = decrypt(self.keyring, blob)
                data = json.loads(plaintext.decode("utf-8"))
            except CryptoError as exc:
                raise StoreError(f"{self.path.name}: {exc}. The file was left untouched.") from exc
            except ValueError as exc:
                raise StoreError(f"{self.path.name}: decrypted content is not valid JSON") from exc
            if not isinstance(data, dict):
                raise StoreError(f"{self.path.name}: unexpected document structure")
            version = data.get("schema_version")
            if not isinstance(version, int) or version > self.schema_version:
                raise StoreError(f"{self.path.name}: unsupported schema version {version!r}")
            migrated = False
            while data["schema_version"] < self.schema_version:
                fn = self.migrations.get(data["schema_version"])
                if fn is None:
                    raise StoreError(f"{self.path.name}: no migration from schema {data['schema_version']}")
                data = fn(data)
                migrated = True
            try:
                self.validate(data)
            except ValueError as exc:
                raise StoreError(f"{self.path.name}: schema validation failed: {exc}") from exc
            if migrated:
                # Keep the pre-migration (still encrypted) file as a backup.
                atomic_write(self.path.with_name(self.path.name + f".premigrate-v{version}"), blob)
                self._save_locked(data)
            return data

    def save(self, data: dict[str, Any], dest: Path | None = None) -> None:
        with self._lock:
            self._save_locked(data, dest)

    def _save_locked(self, data: dict[str, Any], dest: Path | None = None) -> None:
        try:
            self.validate(data)
        except ValueError as exc:
            raise StoreError(f"{self.path.name}: refusing to save invalid data: {exc}") from exc
        plaintext = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        blob = encrypt(self.keyring, plaintext, self._salt)
        self._salt = self._salt or self._extract_salt(blob)
        atomic_write(dest or self.path, blob)

    @staticmethod
    def _extract_salt(blob: bytes) -> bytes:
        import base64

        return base64.b64decode(json.loads(blob)["kdf"]["salt"])


def snapshot(data: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(data)


def cleanup_tmp(data_dir: Path) -> None:
    for tmp in data_dir.glob("*.tmp"):
        try:
            tmp.unlink()
        except OSError:
            pass


def recover_rotation(data_dir: Path) -> str | None:
    """Finish or roll back an interrupted secret rotation. See docs/encryption.md."""
    marker = data_dir / "rotation.commit"
    staged = sorted(data_dir.rglob("*.enc.new"))  # JSON documents and assets/icons/*.enc.new
    if marker.exists():
        for new in staged:
            os.replace(new, new.with_name(new.name[: -len(".new")]))
        marker.unlink()
        _fsync_dir(data_dir)
        return "rolled-forward"
    if staged:
        for new in staged:
            new.unlink()
        return "rolled-back"
    return None
