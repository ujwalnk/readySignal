"""Authenticated encryption for persistent JSON files.

Envelope (UTF-8 JSON, see docs/encryption.md)::

    {"v": 1, "alg": "AES-256-GCM",
     "kdf": {"name": "scrypt", "n": 32768, "r": 8, "p": 1, "length": 32, "salt": "<b64>"},
     "nonce": "<b64, 12 bytes>", "ct": "<b64 ciphertext || 16-byte GCM tag>"}

Everything except ``ct`` is bound to the ciphertext as AES-GCM associated data.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT_VERSION = 1
ALGORITHM = "AES-256-GCM"
KDF_DEFAULT: dict[str, Any] = {"name": "scrypt", "n": 2**15, "r": 8, "p": 1, "length": 32}
MIN_SECRET_LENGTH = 16
SALT_BYTES = 16
NONCE_BYTES = 12


class CryptoError(Exception):
    """Sanitised cryptographic failure (never contains secrets or plaintext)."""


class SecretError(CryptoError):
    """SECRET is missing or unacceptable."""


def validate_secret(secret: str | None) -> str:
    if secret is None or not secret.strip():
        raise SecretError("SECRET is missing or empty")
    if len(secret) < MIN_SECRET_LENGTH:
        raise SecretError(f"SECRET must be at least {MIN_SECRET_LENGTH} characters long")
    return secret


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: Any) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise CryptoError("malformed base64 field in envelope") from exc


class Keyring:
    """Derives (and caches) AES keys from SECRET. Never prints the secret."""

    def __init__(self, secret: str | None) -> None:
        self._secret = validate_secret(secret).encode("utf-8")
        self._cache: dict[tuple[bytes, int, int, int], bytes] = {}

    def __repr__(self) -> str:
        return "Keyring(<redacted>)"

    def key(self, salt: bytes, kdf: dict[str, Any]) -> bytes:
        if kdf.get("name") != "scrypt":
            raise CryptoError("unsupported key derivation function")
        try:
            n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CryptoError("invalid KDF parameters") from exc
        # Bound parameters so a tampered file cannot demand unbounded memory/CPU.
        if not (2**10 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
            raise CryptoError("KDF parameters out of accepted range")
        if len(salt) < 8:
            raise CryptoError("salt too short")
        ck = (salt, n, r, p)
        if ck not in self._cache:
            self._cache[ck] = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(self._secret)
        return self._cache[ck]


def _aad(envelope: dict[str, Any]) -> bytes:
    head = {k: envelope[k] for k in ("v", "alg", "kdf", "nonce")}
    return json.dumps(head, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encrypt(keyring: Keyring, plaintext: bytes, salt: bytes | None = None) -> bytes:
    """Encrypt with a fresh random nonce. ``salt`` may be reused (key is cached)."""
    salt = salt or os.urandom(SALT_BYTES)
    kdf = dict(KDF_DEFAULT, salt=_b64e(salt))
    key = keyring.key(salt, kdf)
    envelope: dict[str, Any] = {
        "v": FORMAT_VERSION,
        "alg": ALGORITHM,
        "kdf": kdf,
        "nonce": _b64e(os.urandom(NONCE_BYTES)),
    }
    ct = AESGCM(key).encrypt(_b64d(envelope["nonce"]), plaintext, _aad(envelope))
    envelope["ct"] = _b64e(ct)
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def decrypt(keyring: Keyring, blob: bytes) -> tuple[bytes, bytes]:
    """Authenticate and decrypt. Returns (plaintext, salt)."""
    try:
        envelope = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CryptoError("file is not a valid encrypted envelope") from exc
    if not isinstance(envelope, dict) or not all(k in envelope for k in ("v", "alg", "kdf", "nonce", "ct")):
        raise CryptoError("file is not a valid encrypted envelope")
    if envelope["v"] != FORMAT_VERSION or envelope["alg"] != ALGORITHM:
        raise CryptoError("unsupported encryption format version or algorithm")
    kdf = envelope["kdf"]
    if not isinstance(kdf, dict):
        raise CryptoError("invalid KDF block")
    salt = _b64d(kdf.get("salt"))
    nonce = _b64d(envelope["nonce"])
    if len(nonce) != NONCE_BYTES:
        raise CryptoError("invalid nonce length")
    key = keyring.key(salt, kdf)
    try:
        plaintext = AESGCM(key).decrypt(nonce, _b64d(envelope["ct"]), _aad(envelope))
    except InvalidTag as exc:
        raise CryptoError("authentication failed: wrong SECRET or file was modified/corrupted") from exc
    return plaintext, salt
