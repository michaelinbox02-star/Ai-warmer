#!/usr/bin/env python
"""Create encrypted PostgreSQL and vault backups with docker compose.

Usage (from the repository root):

    python scripts/backup.py
    python scripts/backup.py --output backups/nightly.dump.enc
    python scripts/backup.py --skip-vault

The passphrase is read from DLP_BACKUP_PASSPHRASE or prompted for. The
intermediate plaintext dump exists only in the system temporary directory and
is removed before the script exits. The vault archive contains the encrypted
secret files, never the master key.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.services.backup_crypto import (  # noqa: E402
    MIN_PASSPHRASE_LENGTH,
    encrypt_bytes,
    encrypt_file,
)


def read_passphrase(confirm: bool) -> str:
    value = os.environ.get("DLP_BACKUP_PASSPHRASE", "")
    if value:
        return value
    value = getpass.getpass("Backup passphrase: ")
    if len(value) < MIN_PASSPHRASE_LENGTH:
        raise SystemExit(f"Passphrase must be at least {MIN_PASSPHRASE_LENGTH} characters")
    if confirm and getpass.getpass("Confirm passphrase: ") != value:
        raise SystemExit("Passphrases do not match")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "backups" / f"deliverability-{stamp}.dump.enc",
    )
    parser.add_argument("--database", default="deliverability")
    parser.add_argument("--user", default="deliverability")
    parser.add_argument(
        "--skip-vault",
        action="store_true",
        help="do not archive /data/secrets (provider tokens will not be restorable)",
    )
    args = parser.parse_args()

    passphrase = read_passphrase(confirm=True)

    with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as handle:
        temporary = Path(handle.name)

    try:
        command = [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "pg_dump",
            "-U",
            args.user,
            "--format=custom",
            "--no-owner",
            "--no-acl",
            args.database,
        ]
        print("Running:", " ".join(command))
        with temporary.open("wb") as stdout:
            completed = subprocess.run(command, cwd=ROOT, stdout=stdout)
        if completed.returncode != 0:
            raise SystemExit(f"pg_dump failed with exit code {completed.returncode}")
        if temporary.stat().st_size == 0:
            raise SystemExit("pg_dump produced an empty file; nothing was written")

        encrypt_file(temporary, args.output, passphrase)
        print(f"Encrypted database backup written: {args.output}")

        if not args.skip_vault:
            vault_command = [
                "docker",
                "compose",
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "api",
                "python",
                "-c",
                "import sys, tarfile; t = tarfile.open(fileobj=sys.stdout.buffer, mode='w'); "
                "t.add('/data/secrets', arcname='secrets'); t.close()",
            ]
            print("Running:", " ".join(vault_command))
            archived = subprocess.run(vault_command, cwd=ROOT, capture_output=True)
            if archived.returncode != 0:
                detail = archived.stderr.decode("utf-8", "replace")[-800:]
                raise SystemExit(f"Vault archive failed: {detail}")
            if not archived.stdout:
                raise SystemExit("Vault archive is empty; nothing was written")
            vault_output = args.output.with_name(f"vault-{stamp}.tar.enc")
            vault_output.write_bytes(encrypt_bytes(archived.stdout, passphrase))
            print(f"Encrypted vault archive written: {vault_output}")
            print("Keep VAULT_MASTER_KEY separately; it is not included in either file.")
        else:
            print("Skipped the vault archive (--skip-vault); stored tokens are not backed up.")

        print("Store the passphrase separately from the backup files.")
        return 0
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
