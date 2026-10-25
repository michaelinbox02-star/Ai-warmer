#!/usr/bin/env python
"""Set up an isolated local PostgreSQL 16 server and a local `.env`.

This is a convenience for running the platform on a workstation without
Docker. It assumes a PostgreSQL server is already listening on 127.0.0.1:55432
(started by scripts/local-db.ps1) and uses the bundled `postgres` superuser.

It creates the `deliverability` role/database and writes a local-only `.env`
with random secrets. It prints the generated operator password and also stores
it in `var/LOCAL_LOGIN.txt` (ignored by git). Use scripts/init-env.ps1 for a
production Compose deployment instead.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"
LOGIN_PATH = ROOT / "var" / "LOCAL_LOGIN.txt"
HOST = "127.0.0.1"
PORT = 55432
ROLE = "deliverability"
DATABASE = "deliverability"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
    return f"pbkdf2_sha256$600000${b64(salt)}${b64(digest)}"


def build_env(db_password: str, admin_password: str) -> dict[str, str]:
    from cryptography.fernet import Fernet

    return {
        "APP_ENV": "local",
        "PUBLIC_BASE_URL": "http://localhost:3000",
        "DATABASE_URL": (
            f"postgresql+asyncpg://{ROLE}:{db_password}@{HOST}:{PORT}/{DATABASE}"
        ),
        # The bundled pgserver build omits PostgreSQL's zoneinfo database, so
        # named zones such as UTC are unavailable. GMT has the same offset.
        "DB_TIMEZONE": "GMT",
        "POSTGRES_PASSWORD": db_password,
        "ADMIN_PASSWORD_HASH": password_hash(admin_password),
        "ADMIN_PASSWORD": "",
        "SESSION_SECRET": secrets.token_urlsafe(48),
        "API_TOKEN": secrets.token_urlsafe(32),
        "EVENT_INGEST_TOKEN": secrets.token_urlsafe(32),
        "SECRET_BACKEND": "encrypted_local_vault",
        "VAULT_DIR": str((ROOT / "var" / "secrets").as_posix()),
        "VAULT_MASTER_KEY": Fernet.generate_key().decode("ascii"),
        "DLP_BACKUP_PASSPHRASE": "",
    }


def main() -> int:
    if not EXAMPLE_PATH.is_file():
        raise SystemExit(f"Missing {EXAMPLE_PATH}")

    try:
        import psycopg2
    except ImportError:
        raise SystemExit("psycopg2 is required; install the API dependencies first")

    try:
        connection = psycopg2.connect(
            host=HOST, port=PORT, user="postgres", dbname="postgres", connect_timeout=5
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"PostgreSQL is not reachable at {HOST}:{PORT}. "
            "Run scripts\\local-db.ps1 start first. "
            f"({type(exc).__name__})"
        )

    connection.autocommit = True
    db_password = secrets.token_urlsafe(24)
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
        if cursor.fetchone() is None:
            cursor.execute(
                f"CREATE ROLE {ROLE} LOGIN PASSWORD %s",  # noqa: S608 - constant identifier
                (db_password,),
            )
        else:
            cursor.execute(f"ALTER ROLE {ROLE} WITH LOGIN PASSWORD %s", (db_password,))

        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,))
        if cursor.fetchone() is None:
            cursor.execute(f"CREATE DATABASE {DATABASE} OWNER {ROLE}")  # noqa: S608
    connection.close()

    admin_password = secrets.token_urlsafe(15)
    values = build_env(db_password, admin_password)

    if ENV_PATH.exists():
        print(f"{ENV_PATH} already exists; left untouched.")
        print("Delete it and re-run this script to rotate local secrets.")
    else:
        lines: list[str] = []
        for line in EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key = line.split("=", 1)[0]
                if key in values:
                    line = f"{key}={values[key]}"
            lines.append(line)
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

        LOGIN_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOGIN_PATH.write_text(
            "Local operator console\n"
            "URL: http://localhost:3000\n"
            f"Password: {admin_password}\n\n"
            "This password is stored as a PBKDF2 hash in .env. Delete this file\n"
            "after you have saved the password somewhere safe.\n",
            encoding="utf-8",
        )
        print(f"Wrote {ENV_PATH}")
        print(f"Operator password: {admin_password}")
        print(f"Also saved to {LOGIN_PATH}")

    print(f"Database ready: postgresql://{ROLE}:***@{HOST}:{PORT}/{DATABASE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
