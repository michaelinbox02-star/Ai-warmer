"""Operator CLI: `python -m app.cli <command>`."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import secrets
import sys
from pathlib import Path

from app.config import settings


def _cmd_hash_password() -> int:
    password = getpass.getpass("New operator password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match", file=sys.stderr)
        return 2
    from app.security import hash_password

    try:
        print(hash_password(password))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _cmd_gen_key() -> int:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode("ascii"))
    return 0


def _cmd_gen_token() -> int:
    print(secrets.token_urlsafe(48))
    return 0


def _cmd_config() -> int:
    from app.providers.registry import provider_catalog
    from app.services.secrets import get_secret_store

    summary = {
        "app_env": settings.app_env,
        "database": settings.database_url.split("@")[-1],
        "public_base_url": settings.public_base_url,
        "secret_backend": settings.secret_backend,
        "vault_dir": settings.vault_dir if settings.secret_backend == "encrypted_local_vault" else None,
        "admin_password_hash_set": bool(settings.admin_password_hash),
        "admin_password_set": bool(settings.admin_password),
        "api_token_set": bool(settings.api_token),
        "event_ingest_token_set": bool(settings.event_ingest_token),
        "llm_enabled": settings.llm_enabled,
        "providers": provider_catalog(),
    }
    try:
        get_secret_store().probe()
        summary["secret_store"] = "ok"
    except Exception as exc:  # noqa: BLE001
        summary["secret_store"] = f"error: {type(exc).__name__}"
    print(json.dumps(summary, indent=2))
    return 0


async def _prune_secrets(apply: bool) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ProviderCredential
    from app.services.secrets import EncryptedLocalVault, get_secret_store

    store = get_secret_store()
    if not isinstance(store, EncryptedLocalVault):
        print("Orphan pruning is only implemented for the encrypted local vault", file=sys.stderr)
        return 2

    async with SessionLocal() as session:
        refs = set((await session.scalars(select(ProviderCredential.secret_ref))).all())

    files = sorted(store.root.glob("*.token"))
    orphans = [path for path in files if f"vault://{path.stem}" not in refs]
    for path in orphans:
        print(("deleting " if apply else "would delete ") + path.name)
        if apply:
            path.unlink(missing_ok=True)
    print(f"{len(orphans)} orphaned secret file(s); {len(refs)} referenced.")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    return asyncio.run(_prune_secrets(args.apply))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Deliverability platform operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("hash-password", help="Hash an operator password for ADMIN_PASSWORD_HASH")
    sub.add_parser("gen-key", help="Generate a Fernet key for VAULT_MASTER_KEY")
    sub.add_parser("gen-token", help="Generate a random API or event-ingest token")
    sub.add_parser("config", help="Print effective non-secret configuration")

    prune = sub.add_parser("prune-secrets", help="Remove vault files that no account references")
    prune.add_argument("--apply", action="store_true", help="Actually delete; default is a dry run")

    args = parser.parse_args(argv)
    if args.command == "hash-password":
        return _cmd_hash_password()
    if args.command == "gen-key":
        return _cmd_gen_key()
    if args.command == "gen-token":
        return _cmd_gen_token()
    if args.command == "config":
        return _cmd_config()
    if args.command == "prune-secrets":
        return _cmd_prune(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
