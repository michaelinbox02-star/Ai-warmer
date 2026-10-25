"""Encrypted backup format (spec section 15: encrypted backup and restore)."""

from __future__ import annotations

import pytest

from app.services.backup_crypto import (
    BackupCryptoError,
    MAGIC,
    decrypt_bytes,
    decrypt_file,
    encrypt_bytes,
    encrypt_file,
)

PASSPHRASE = "a-long-backup-passphrase"


def test_roundtrip_uses_magic_and_random_salt():
    first = encrypt_bytes(b"pg_dump-bytes", PASSPHRASE)
    second = encrypt_bytes(b"pg_dump-bytes", PASSPHRASE)
    assert first.startswith(MAGIC)
    assert first != second  # fresh salt and nonce
    assert decrypt_bytes(first, PASSPHRASE) == b"pg_dump-bytes"


def test_wrong_passphrase_and_tampering_fail_closed():
    blob = encrypt_bytes(b"database", PASSPHRASE)
    with pytest.raises(BackupCryptoError):
        decrypt_bytes(blob, "a-different-passphrase")
    with pytest.raises(BackupCryptoError):
        decrypt_bytes(blob[:-4] + b"AAAA", PASSPHRASE)


def test_malformed_files_are_rejected():
    with pytest.raises(BackupCryptoError):
        decrypt_bytes(b"not-a-backup", PASSPHRASE)
    with pytest.raises(BackupCryptoError):
        decrypt_bytes(MAGIC + b"short", PASSPHRASE)


def test_short_passphrase_is_rejected():
    with pytest.raises(ValueError):
        encrypt_bytes(b"data", "short")


def test_file_helpers(tmp_path):
    source = tmp_path / "dump.bin"
    source.write_bytes(b"12345")
    encrypted = tmp_path / "backup" / "dump.enc"
    restored = tmp_path / "restore" / "dump.bin"

    encrypt_file(source, encrypted, PASSPHRASE)
    assert encrypted.read_bytes().startswith(MAGIC)
    assert b"12345" not in encrypted.read_bytes()

    decrypt_file(encrypted, restored, PASSPHRASE)
    assert restored.read_bytes() == b"12345"
