"""Provider credential lifecycle.

Raw secrets live only in the configured secret store. PostgreSQL holds a
reference, a version, scopes, and expiry metadata.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, PolicyViolation
from app.core.time import utcnow
from app.models import EmailAccount, ProviderCredential
from app.providers.base import ProviderContext
from app.services.secrets import get_secret_store


async def get_credential(session: AsyncSession, account_id: uuid.UUID) -> ProviderCredential | None:
    return await session.scalar(
        select(ProviderCredential).where(ProviderCredential.account_id == account_id)
    )


async def store_credentials(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    payload: Mapping[str, Any],
    scopes: list[str] | None = None,
    access_token_expires_at: datetime | None = None,
    secret_backend: str | None = None,
) -> ProviderCredential:
    """Create or replace the credential for an account.

    The previous secret is intentionally left in the store and pruned by
    `python -m app.cli prune-secrets`, so a rolled-back transaction can never
    destroy the only working credential.
    """

    store = get_secret_store()
    secret_ref = store.put(payload)

    credential = await get_credential(session, account_id)
    if credential is None:
        credential = ProviderCredential(
            account_id=account_id,
            secret_backend=secret_backend or store.backend,
            secret_ref=secret_ref,
            credential_version=1,
            granted_scopes=scopes or [],
            access_token_expires_at=access_token_expires_at,
        )
        session.add(credential)
    else:
        credential.secret_backend = secret_backend or store.backend
        credential.secret_ref = secret_ref
        credential.credential_version = int(credential.credential_version) + 1
        if scopes is not None:
            credential.granted_scopes = scopes
        credential.access_token_expires_at = access_token_expires_at
        credential.updated_at = utcnow()
    await session.flush()
    return credential


async def rotate_credentials(
    session: AsyncSession,
    *,
    credential: ProviderCredential,
    payload: Mapping[str, Any],
    access_token_expires_at: datetime | None = None,
) -> ProviderCredential:
    store = get_secret_store()
    secret_ref = store.put(payload)
    credential.secret_ref = secret_ref
    credential.secret_backend = store.backend
    credential.credential_version = int(credential.credential_version) + 1
    credential.access_token_expires_at = access_token_expires_at
    credential.last_refresh_at = utcnow()
    credential.updated_at = utcnow()
    await session.flush()
    return credential


async def delete_credentials(
    session: AsyncSession, *, account_id: uuid.UUID, purge_secret: bool = True
) -> None:
    credential = await get_credential(session, account_id)
    if credential is None:
        return
    secret_ref = credential.secret_ref
    await session.delete(credential)
    await session.flush()
    if purge_secret:
        try:
            get_secret_store().delete(secret_ref)
        except Exception:  # noqa: BLE001 - never fail an account removal on cleanup
            pass


async def build_context(
    session: AsyncSession, account: EmailAccount, *, require_secret: bool = True
) -> ProviderContext:
    credential = await get_credential(session, account.id)
    secret: dict[str, Any] = {}
    if credential is None:
        if require_secret:
            raise PolicyViolation(
                "No credentials are configured for this account",
                code="credentials_missing",
            )
    else:
        secret = get_secret_store().get(credential.secret_ref)

    return ProviderContext(
        account_id=account.id,
        provider=account.provider,
        transport=account.transport,
        mailbox_email=account.mailbox_email,
        display_name=account.display_name,
        sending_domain=account.sending_domain,
        metadata=dict(account.provider_metadata or {}),
        secret=secret,
    )


async def persist_token_refresh(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    update: Mapping[str, Any] | None,
) -> None:
    """Persist a token payload returned by a provider adapter, if any."""

    if not update:
        return
    credential = await get_credential(session, account_id)
    if credential is None:
        raise NotFound("Credential record disappeared during refresh", code="credentials_missing")

    expires_raw = update.get("expires_at")
    expires_at: datetime | None = None
    if isinstance(expires_raw, str):
        try:
            expires_at = datetime.fromisoformat(expires_raw)
        except ValueError:
            expires_at = None
    elif isinstance(expires_raw, datetime):
        expires_at = expires_raw

    await rotate_credentials(
        session,
        credential=credential,
        payload=update,
        access_token_expires_at=expires_at,
    )


async def mark_validated(session: AsyncSession, *, account_id: uuid.UUID) -> None:
    credential = await get_credential(session, account_id)
    if credential is not None:
        credential.last_validated_at = utcnow()
        credential.updated_at = utcnow()
