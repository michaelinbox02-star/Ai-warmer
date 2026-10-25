"""Application-level settings stored in `app_settings`.

Values contain policy only. Secrets never go here.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.core.time import iso, utcnow
from app.services.audit import record_audit, record_safety_event

GLOBAL_SENDING_KEY = "global_sending_enabled"
RETENTION_KEY = "retention_days"
READINESS_ACK_PREFIX = "readiness_ack:"
EVENT_INGEST_KEY = "event_ingest_config"


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    value = await session.scalar(select(AppSetting.setting_value).where(AppSetting.setting_key == key))
    return default if value is None else value


async def set_setting(
    session: AsyncSession, key: str, value: Any, *, updated_by: str
) -> None:
    statement = (
        insert(AppSetting)
        .values(
            setting_key=key,
            setting_value=value,
            updated_by=updated_by[:200],
            updated_at=utcnow(),
        )
        .on_conflict_do_update(
            index_elements=[AppSetting.setting_key],
            set_={
                "setting_value": value,
                "updated_by": updated_by[:200],
                "updated_at": utcnow(),
            },
        )
    )
    await session.execute(statement)


async def is_global_sending_enabled(session: AsyncSession) -> bool:
    return bool(await get_setting(session, GLOBAL_SENDING_KEY, False))


async def set_global_sending(
    session: AsyncSession,
    *,
    enabled: bool,
    actor: str,
    reason: str,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> None:
    await set_setting(session, GLOBAL_SENDING_KEY, enabled, updated_by=actor)
    await record_audit(
        session,
        actor=actor,
        action="system.resume" if enabled else "system.pause",
        target_type="app_setting",
        result="success",
        request_id=request_id,
        source_ip=source_ip,
        details={"reason": reason},
    )
    await record_safety_event(
        session,
        severity="warning" if enabled else "critical",
        event_type="global_sending_enabled" if enabled else "global_sending_paused",
        action_taken="future submissions " + ("allowed" if enabled else "blocked"),
        details={"reason": reason, "actor": actor},
    )


async def acknowledged_warnings(session: AsyncSession, account_id: uuid.UUID) -> set[str]:
    value = await get_setting(session, f"{READINESS_ACK_PREFIX}{account_id}", {})
    if not isinstance(value, dict):
        return set()
    keys = value.get("keys", [])
    return {str(k) for k in keys} if isinstance(keys, list) else set()


async def acknowledge_warnings(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    keys: list[str],
    actor: str,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> None:
    current = await acknowledged_warnings(session, account_id)
    merged = sorted(current | {k for k in keys if k})
    await set_setting(
        session,
        f"{READINESS_ACK_PREFIX}{account_id}",
        {"keys": merged, "acknowledged_at": iso(utcnow()), "actor": actor},
        updated_by=actor,
    )
    await record_audit(
        session,
        actor=actor,
        action="account.acknowledge_readiness_warnings",
        target_type="email_account",
        target_id=account_id,
        request_id=request_id,
        source_ip=source_ip,
        details={"keys": keys},
    )


async def clear_acknowledgements(
    session: AsyncSession, *, account_id: uuid.UUID, actor: str
) -> None:
    await set_setting(
        session,
        f"{READINESS_ACK_PREFIX}{account_id}",
        {"keys": [], "acknowledged_at": iso(utcnow()), "actor": actor},
        updated_by=actor,
    )


async def retention_settings(session: AsyncSession) -> dict[str, int]:
    from app.config import settings as app_settings

    stored = await get_setting(session, RETENTION_KEY, {})
    values = {
        "body_days": app_settings.retention_body_days,
        "event_days": app_settings.retention_event_days,
        "audit_days": app_settings.retention_audit_days,
    }
    if isinstance(stored, dict):
        for key in values:
            if isinstance(stored.get(key), int) and stored[key] > 0:
                values[key] = stored[key]
    return values
