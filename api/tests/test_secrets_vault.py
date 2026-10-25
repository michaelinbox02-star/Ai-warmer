"""Encrypted local secret vault: storage, integrity, and reference validation."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.core.errors import CapabilityNotSupported
from app.services.secrets import EncryptedLocalVault, SecretStoreError


@pytest.fixture()
def vault(tmp_path):
    key = Fernet.generate_key().decode("ascii")
    return EncryptedLocalVault(str(tmp_path / "vault"), key)


def test_secret_roundtrip(vault):
    ref = vault.put({"password": "smtp-password", "username": "sender@example.com"})
    assert ref.startswith("vault://")
    assert len(ref.removeprefix("vault://")) == 32
    assert vault.get(ref) == {"password": "smtp-password", "username": "sender@example.com"}

    vault.delete(ref)
    with pytest.raises(SecretStoreError):
        vault.get(ref)
    # Deleting twice is allowed.
    vault.delete(ref)


def test_empty_payload_is_rejected(vault):
    with pytest.raises(SecretStoreError):
        vault.put({})
    with pytest.raises(SecretStoreError):
        vault.put({"password": None})


def test_invalid_references_are_rejected(vault):
    for ref in ("vault://", "vault://..", "vault://not-hex", "file:///etc/passwd", "vault://a/b"):
        with pytest.raises(SecretStoreError):
            vault.get(ref)


def test_tampered_ciphertext_fails_closed(vault, tmp_path):
    ref = vault.put({"refresh_token": "secret-token"})
    name = ref.removeprefix("vault://")
    path = tmp_path / "vault" / f"{name}.token"
    raw = path.read_bytes()
    path.write_bytes(raw[:-4] + b"AAAA")
    with pytest.raises(SecretStoreError):
        vault.get(ref)


def test_wrong_master_key_fails_closed(tmp_path):
    first = EncryptedLocalVault(str(tmp_path / "vault"), Fernet.generate_key().decode("ascii"))
    ref = first.put({"password": "one"})
    second = EncryptedLocalVault(str(tmp_path / "vault"), Fernet.generate_key().decode("ascii"))
    with pytest.raises(SecretStoreError):
        second.get(ref)


def test_invalid_master_key_is_reported_clearly(tmp_path):
    with pytest.raises(SecretStoreError):
        EncryptedLocalVault(str(tmp_path / "vault"), "not-a-fernet-key")


def test_unsupported_backend_raises(tmp_path):
    from app.services.secrets import DockerSecrets

    store = DockerSecrets(str(tmp_path / "missing"))
    with pytest.raises(CapabilityNotSupported):
        store.put({"password": "x"})
