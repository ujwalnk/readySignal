import json
import os

import pytest

from app import crypto
from app.crypto import CryptoError, Keyring, SecretError
from app.engine import FILE_NAMES, Engine
from app.models import SCHEMA_VERSIONS, default_config, validate_config
from app.rotate import rotate
from app.store import EncryptedJSONFile, StoreError, atomic_write, recover_rotation

from conftest import SECRET, make_engine, run

KR = Keyring(SECRET)


@pytest.mark.parametrize("bad", [None, "", "   ", "short"])
def test_secret_validation(bad):
    with pytest.raises(SecretError):
        Keyring(bad)


def test_missing_secret_blocks_engine(tmp_path):
    with pytest.raises(SecretError):
        Engine(tmp_path, None)


def test_round_trip_and_fresh_ciphertext():
    a, b = crypto.encrypt(KR, b"hello"), crypto.encrypt(KR, b"hello")
    assert a != b and json.loads(a)["nonce"] != json.loads(b)["nonce"]
    assert crypto.decrypt(KR, a)[0] == b"hello"
    env = json.loads(a)
    assert env["v"] == 1 and env["alg"] == "AES-256-GCM" and env["kdf"]["name"] == "scrypt" and env["kdf"]["salt"]


def test_tamper_wrong_secret_garbage():
    blob = crypto.encrypt(KR, b"data")
    env = json.loads(blob)
    env["ct"] = env["ct"][:-4] + ("AAAA" if not env["ct"].endswith("AAAA") else "BBBB")
    with pytest.raises(CryptoError):
        crypto.decrypt(KR, json.dumps(env).encode())
    env = json.loads(blob)
    env["kdf"]["n"] = 2**14  # header is authenticated / changes the key
    with pytest.raises(CryptoError):
        crypto.decrypt(KR, json.dumps(env).encode())
    with pytest.raises(CryptoError):
        crypto.decrypt(Keyring("another-long-secret-value"), blob)
    with pytest.raises(CryptoError):
        crypto.decrypt(KR, b"not json")
    env = json.loads(blob)
    env["kdf"]["n"] = 2**30
    with pytest.raises(CryptoError):
        crypto.decrypt(KR, json.dumps(env).encode())


def test_no_plaintext_on_disk(data_dir, clock):
    e = make_engine(data_dir, clock)
    run(e.create_account({"name": "UniqueAccountName", "duration_seconds": 3600, "prompt": "TOPSECRETPROMPT"}))
    for f in data_dir.iterdir():
        raw = f.read_bytes()
        assert b"UniqueAccountName" not in raw and b"TOPSECRETPROMPT" not in raw
        assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert not list(data_dir.glob("*.tmp"))


def test_restart_recovery_and_wrong_secret_refusal(data_dir, clock):
    e = make_engine(data_dir, clock)
    acct = run(e.create_account({"name": "A", "duration_seconds": 3600}))
    before = {f.name: f.read_bytes() for f in data_dir.iterdir()}
    e2 = make_engine(data_dir, clock)
    assert e2.accounts["accounts"][0]["id"] == acct["id"]
    with pytest.raises(StoreError):
        Engine(data_dir, "a-completely-different-secret", clock).load()
    assert {f.name: f.read_bytes() for f in data_dir.iterdir()} == before  # nothing overwritten


def test_corrupted_file_preserved(data_dir, clock):
    make_engine(data_dir, clock)
    p = data_dir / FILE_NAMES["state"]
    p.write_bytes(b"corrupt")
    with pytest.raises(StoreError):
        make_engine(data_dir, clock)
    assert p.read_bytes() == b"corrupt"


def test_failed_write_preserves_previous(tmp_path, monkeypatch):
    f = EncryptedJSONFile(tmp_path / "c.enc", KR, 1, default_config, validate_config)
    data = f.load()
    before = (tmp_path / "c.enc").read_bytes()
    data["theme"] = "dark"

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(StoreError):
        f.save(data)
    monkeypatch.undo()
    assert (tmp_path / "c.enc").read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))
    data["theme"] = "bogus"
    with pytest.raises(StoreError):
        f.save(data)  # validation before saving


def test_engine_memory_unchanged_when_commit_fails(data_dir, clock, monkeypatch):
    e = make_engine(data_dir, clock)

    def boom(*a, **k):
        raise StoreError("disk full")

    monkeypatch.setattr(e.files["accounts"], "save", boom)
    with pytest.raises(StoreError):
        run(e.create_account({"name": "X", "duration_seconds": 3600}))
    assert e.accounts["accounts"] == []


def test_schema_migration(tmp_path):
    def mig(d):
        d["schema_version"] = 2
        d["theme"] = "dark"
        return d

    def v1():
        return {**default_config(), "schema_version": 1}

    old = EncryptedJSONFile(tmp_path / "c.enc", KR, 1, v1, validate_config)
    old.load()
    new = EncryptedJSONFile(tmp_path / "c.enc", KR, 2, default_config, validate_config, {1: mig})
    assert new.load()["theme"] == "dark"
    assert list(tmp_path.glob("c.enc.premigrate-v1"))
    newer = EncryptedJSONFile(tmp_path / "c.enc", KR, 1, v1, validate_config)
    with pytest.raises(StoreError):
        newer.load()  # file schema is newer than the code


def test_rotation(data_dir, clock):
    e = make_engine(data_dir, clock)
    run(e.create_account({"name": "Rot", "duration_seconds": 3600}))
    new = "brand-new-secret-value-123"
    done = rotate(data_dir, SECRET, new)
    assert len(done) == 4
    with pytest.raises(StoreError):
        make_engine(data_dir, clock, SECRET)
    assert make_engine(data_dir, clock, new).accounts["accounts"][0]["name"] == "Rot"
    with pytest.raises(StoreError):
        rotate(data_dir, "wrong-old-secret-value-xx", "yet-another-new-secret-1")  # wrong old: nothing changed
    assert make_engine(data_dir, clock, new)


def test_rotation_interrupted_rolls_back_and_forward(data_dir, clock):
    make_engine(data_dir, clock)
    stale = data_dir / (FILE_NAMES["state"] + ".new")
    stale.write_bytes(b"partial")
    assert recover_rotation(data_dir) == "rolled-back" and not stale.exists()
    # roll-forward: marker + staged files
    kr2 = Keyring("brand-new-secret-value-123")
    for name, fname in FILE_NAMES.items():
        src = data_dir / fname
        ver = SCHEMA_VERSIONS[name]
        plain = EncryptedJSONFile(src, KR, ver, lambda: {}, lambda d: None).load()
        EncryptedJSONFile(src, kr2, ver, lambda: {}, lambda d: None).save(plain, data_dir / (fname + ".new"))
    (data_dir / "rotation.commit").write_bytes(b"")
    assert recover_rotation(data_dir) == "rolled-forward"
    assert make_engine(data_dir, clock, "brand-new-secret-value-123")


def test_atomic_write_no_leftovers(tmp_path):
    atomic_write(tmp_path / "x", b"1")
    atomic_write(tmp_path / "x", b"2")
    assert (tmp_path / "x").read_bytes() == b"2" and [p.name for p in tmp_path.iterdir()] == ["x"]
