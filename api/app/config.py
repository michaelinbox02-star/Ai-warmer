"""Application configuration.

Environment driven (12-factor). Secrets are supplied by the operator and must
never be committed: see `.env.example` and `docker-compose.yml`.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Runtime -----------------------------------------------------------
    app_env: Literal["local", "production"] = "local"
    log_level: str = "INFO"
    # Base URL of the operator UI. Used for OAuth redirects and CSRF checks.
    public_base_url: str = "http://localhost:3000"
    # Where the API can reach itself / the UI proxy (unused by the UI build).
    api_internal_url: str = "http://localhost:8000"
    extra_allowed_origins: list[str] = Field(default_factory=list)

    # --- Database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://deliverability:deliverability@localhost:5432/deliverability"
    db_pool_size: int = 5
    db_pool_max_overflow: int = 5
    db_statement_timeout_ms: int = 15_000
    # Session time zone used on every connection. Standard PostgreSQL builds
    # accept "UTC"; bare-bones local builds without the zoneinfo database may
    # only accept fixed-offset names such as "GMT".
    db_timezone: str = "UTC"

    # --- Operator authentication ------------------------------------------
    # Exactly one of admin_password_hash / admin_password must be set.
    admin_password_hash: str | None = None
    admin_password: str | None = None
    # HMAC key for the session cookie. Required in production (>=32 chars).
    session_secret: str = ""
    session_ttl_minutes: int = 8 * 60
    # Optional bearer token for local automation (scripts, cron).
    api_token: str | None = None
    # Shared token required by the local provider-event relay endpoint.
    event_ingest_token: str | None = None
    login_max_attempts: int = 5
    login_window_seconds: int = 300

    # --- Secret store ------------------------------------------------------
    # os_keyring | docker_secret | encrypted_local_vault
    secret_backend: Literal["os_keyring", "docker_secret", "encrypted_local_vault"] = "encrypted_local_vault"
    # Directory holding the encrypted local vault (one file per secret).
    vault_dir: str = "./var/secrets"
    # Fernet key (base64, 32 bytes). Not the same as a password.
    vault_master_key: str | None = None
    docker_secret_dir: str = "/run/secrets"

    # --- OAuth providers ---------------------------------------------------
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None
    microsoft_tenant: str = "common"

    # --- Worker ------------------------------------------------------------
    worker_id: str = Field(default_factory=lambda: f"worker-{secrets.token_hex(4)}")
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 120
    worker_send_timeout_seconds: int = 45
    worker_housekeeping_seconds: int = 30

    # --- Sending defaults --------------------------------------------------
    # The specification scopes v1 to 1–5 operator-owned mailboxes.
    max_accounts: int = Field(default=5, ge=1, le=50)
    max_message_bytes: int = 1024 * 1024
    default_max_per_minute: int = 3
    default_max_rolling_24h: int = 25
    default_max_attempts: int = 5
    default_max_in_flight: int = 1
    # Do not submit messages older than this without an explicit expiry.
    scheduled_grace_seconds: int = 0

    # --- DNS / TLS diagnostics --------------------------------------------
    dns_timeout_seconds: float = 5.0
    dns_check_ttl_seconds: int = 24 * 60 * 60

    # --- Optional AI diagnostics (advisory only) ---------------------------
    llm_enabled: bool = False
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0
    llm_max_input_chars: int = 1200

    # --- Retention (documentation + optional cleanup job) -----------------
    retention_body_days: int = 30
    retention_event_days: int = 90
    retention_audit_days: int = 180

    # --- Rate limiting (in-process) ---------------------------------------
    send_rate_limit_per_minute: int = 30

    @field_validator("public_base_url", "api_internal_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not self.session_secret:
            if self.app_env == "production":
                raise ValueError("SESSION_SECRET is required in production")
            # Local/dev convenience: ephemeral key, sessions reset on restart.
            self.session_secret = secrets.token_urlsafe(48)
        if self.app_env == "production":
            if len(self.session_secret) < 32:
                raise ValueError("SESSION_SECRET must be at least 32 characters")
            if self.admin_password and len(self.admin_password) < 12:
                raise ValueError("ADMIN_PASSWORD must be at least 12 characters in production")
            if not (self.admin_password or self.admin_password_hash):
                raise ValueError("ADMIN_PASSWORD or ADMIN_PASSWORD_HASH is required")
            if self.secret_backend == "encrypted_local_vault" and not self.vault_master_key:
                raise ValueError("VAULT_MASTER_KEY is required for the encrypted local vault")
            if not self.public_base_url.startswith("https://") and "localhost" not in self.public_base_url:
                raise ValueError("PUBLIC_BASE_URL must use HTTPS outside loopback")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        origins = {
            self.public_base_url,
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            *self.extra_allowed_origins,
        }
        return sorted(origins)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
