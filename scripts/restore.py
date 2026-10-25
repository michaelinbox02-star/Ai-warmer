#!/usr/bin/env python
"""Restore an encrypted PostgreSQL backup with docker compose.

This is destructive: `pg_restore --clean --if-exists` drops and recreates the
objects contained in the backup. Run it only in a maintenance window with the
api and worker stopped.

Usage (from the repository root):

    python scripts/restore.py --input backups/deliverability-....dump.enc
    python scripts/restore.py --input ... --vault backups/vault-....tar.enc
    python scripts/restore.py --input ... --yes       # non-interactive
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.services.backup_crypto import decrypt_file, MIN_PASSPHRASE_LENGTH  # noqa: E402


def read_passphrase() -> str:
    value = os.environ.get("DLP_BACKUP_PASSPHRASE", "")
    if value:
        return value
    value = getpass.getpass("Backup passphrase: ")
    if len(value) < MIN_PASSPHRASE_LENGTH:
        raise SystemExit("Invalid passphrase length")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--vault",
        type=Path,
        default=None,
        help="optional encrypted vault archive (vault-*.tar.enc) to restore",
    )
    parser.add_argument("--database", default="deliverability")
    parser.add_argument("--user", default="deliverability")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    if not args.input.is_file():
        raise SystemExit(f"Backup not found: {args.input}")
    if args.vault is not None and not args.vault.is_file():
        raise SystemExit(f"Vault archive not found: {args.vault}")

    if not args.yes:
        answer = input(
            f"This will overwrite data in database '{args.database}'. Type RESTORE to continue: "
        )
        if answer.strip() != "RESTORE":
            raise SystemExit("Aborted")

    passphrase = read_passphrase()
    with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as handle:
        temporary = Path(handle.name)

    try:
        decrypt_file(args.input, temporary, passphrase)
        command = [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "pg_restore",
            "-U",
            args.user,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-acl",
            "--dbname",
            args.database,
        ]
        print("Running:", " ".join(command))
        with temporary.open("rb") as stdin:
            completed = subprocess.run(command, cwd=ROOT, stdin=stdin)
        if completed.returncode != 0:
            raise SystemExit(f"pg_restore failed with exit code {completed.returncode}")

        if args.vault is not None:
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as vault_handle:
                vault_temporary = Path(vault_handle.name)
            try:
                decrypt_file(args.vault, vault_temporary, passphrase)
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
                    "import sys, tarfile; t = tarfile.open(fileobj=sys.stdin.buffer, mode='r'); "
                    "t.extractall('/data'); t.close()",
                ]
                print("Running:", " ".join(vault_command))
                with vault_temporary.open("rb") as stdin:
                    vault_completed = subprocess.run(vault_command, cwd=ROOT, stdin=stdin)
                if vault_completed.returncode != 0:
                    raise SystemExit(
                        f"Vault restore failed with exit code {vault_completed.returncode}"
                    )
                print("Provider token vault restored.")
            finally:
                vault_temporary.unlink(missing_ok=True)

        print("Restore finished. Restart api and worker, then run readiness checks.")
        return 0
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
