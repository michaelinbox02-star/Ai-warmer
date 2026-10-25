"""Provider registry."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.core.errors import CapabilityNotSupported
from app.providers.base import EmailProvider
from app.providers.gmail import GmailProvider
from app.providers.graph import GraphProvider
from app.providers.smtp import SMTPProvider


@lru_cache
def _instances() -> dict[str, EmailProvider]:
    return {
        "gmail_api": GmailProvider(),
        "microsoft_graph": GraphProvider(),
        "smtp": SMTPProvider(),
    }


def provider_for_transport(transport: str) -> EmailProvider:
    provider = _instances().get(transport)
    if provider is None:
        raise CapabilityNotSupported(f"Unsupported transport: {transport}")
    return provider


def provider_catalog() -> list[dict[str, Any]]:
    labels = {
        "gmail_api": "Gmail API",
        "microsoft_graph": "Microsoft Graph",
        "smtp": "Authenticated SMTP",
    }
    return [
        {
            "transport": name,
            "label": labels.get(name, name),
            "capabilities": provider.capabilities.as_dict(),
        }
        for name, provider in _instances().items()
    ]
