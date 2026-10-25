"""Dated SPF/DKIM/DMARC/MX/TLS diagnostics.

Results are evidence at a point in time. They never prove that a provider is
signing every message or that a receiver will accept it.
"""

from __future__ import annotations

import logging
import ssl
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable

import dns.asyncresolver
import dns.exception
import dns.resolver
from aiosmtplib.errors import SMTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import PolicyViolation
from app.core.time import utcnow
from app.models import DomainAuthCheck, EmailAccount
from app.providers.smtp import probe_endpoint_tls
from app.services.audit import record_audit

logger = logging.getLogger(__name__)

CHECK_TYPES = ("spf", "dkim", "dmarc", "mx", "tls", "sender_alignment")
DEFAULT_SELECTORS = {
    "google_gmail": ("google",),
    "google_workspace": ("google",),
    "microsoft_365": ("selector1", "selector2"),
    "outlook_com": ("selector1", "selector2"),
}


@dataclass
class CheckOutcome:
    check_type: str
    status: str
    observed_value: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    selector: str | None = None


def _resolver() -> dns.asyncresolver.Resolver:
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = settings.dns_timeout_seconds
    resolver.lifetime = settings.dns_timeout_seconds
    return resolver


async def _txt_records(name: str) -> list[str]:
    resolver = _resolver()
    answer = await resolver.resolve(name, "TXT", search=False)
    records: list[str] = []
    for rdata in answer:
        try:
            records.append(b"".join(rdata.strings).decode("utf-8", "replace"))
        except AttributeError:  # pragma: no cover - future dnspython shapes
            records.append(str(rdata).strip('"'))
    return records


def _lookup_count(record: str) -> int:
    count = 0
    for token in record.split()[1:]:
        candidate = token.lstrip("+-~?")
        if candidate.startswith(("include:", "exists:", "redirect=")):
            count += 1
        elif candidate in {"a", "mx", "ptr"} or candidate.startswith(("a:", "a/", "mx:", "mx/", "ptr:")):
            count += 1
    return count


async def check_spf(domain: str) -> CheckOutcome:
    try:
        records = await _txt_records(domain)
    except dns.resolver.NXDOMAIN:
        return CheckOutcome("spf", "fail", None, {"error": "domain does not exist"})
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        return CheckOutcome("spf", "unknown", None, {"error": f"DNS lookup failed: {type(exc).__name__}"})

    spf_records = [r for r in records if r.strip().lower().startswith("v=spf1")]
    if not spf_records:
        return CheckOutcome("spf", "fail", None, {"records": records, "reason": "no SPF record"})
    if len(spf_records) > 1:
        return CheckOutcome(
            "spf", "fail", " | ".join(spf_records), {"reason": "multiple SPF records are invalid"}
        )

    record = spf_records[0]
    lookups = _lookup_count(record)
    has_all = any(token.lstrip("+-~?").lower() == "all" for token in record.split()[1:])
    evidence = {"lookups": lookups, "lookup_limit": 10, "has_all": has_all}
    if lookups > 10:
        return CheckOutcome("spf", "fail", record, {**evidence, "reason": "exceeds the 10-lookup limit"})
    if not has_all:
        return CheckOutcome("spf", "warning", record, {**evidence, "reason": "record has no 'all' mechanism"})
    if lookups >= 8:
        return CheckOutcome("spf", "warning", record, {**evidence, "reason": "close to the 10-lookup limit"})
    return CheckOutcome("spf", "pass", record, evidence)


async def check_dkim(domain: str, selectors: Iterable[str]) -> CheckOutcome:
    selectors = tuple(dict.fromkeys(s.strip() for s in selectors if s and s.strip()))
    if not selectors:
        return CheckOutcome(
            "dkim",
            "unknown",
            None,
            {
                "reason": "no DKIM selectors configured; selector discovery is not reliable",
                "hint": "set provider_metadata.dkim_selectors for custom SMTP domains",
            },
        )

    found: list[dict[str, Any]] = []
    problems: list[str] = []
    for selector in selectors:
        name = f"{selector}._domainkey.{domain}"
        try:
            records = await _txt_records(name)
        except dns.resolver.NXDOMAIN:
            problems.append(f"{selector}: no record")
            continue
        except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
            problems.append(f"{selector}: DNS failure ({type(exc).__name__})")
            continue

        dkim_records = [r for r in records if "v=DKIM1" in r or "p=" in r]
        if not dkim_records:
            problems.append(f"{selector}: no DKIM key record")
            continue

        merged = " ".join(dkim_records)
        tags = _parse_tags(merged, sep=";")
        key = tags.get("p", "").strip()
        if not key:
            problems.append(f"{selector}: public key revoked (empty p=)")
            found.append({"selector": selector, "revoked": True, "record": merged})
            continue
        found.append(
            {
                "selector": selector,
                "revoked": False,
                "key_type": tags.get("k", "rsa"),
                "record": merged,
            }
        )

    if any(item["revoked"] for item in found):
        return CheckOutcome(
            "dkim",
            "fail",
            found[0]["record"],
            {"selectors": found, "problems": problems, "reason": "key revoked"},
            selector=found[0]["selector"],
        )
    if found:
        status = "pass"
        reason = None
        if len(found) < len(selectors):
            status = "warning"
            reason = "some selectors are missing (rotation in progress is normal)"
        return CheckOutcome(
            "dkim",
            status,
            found[0]["record"],
            {"selectors": found, "problems": problems, "reason": reason},
            selector=found[0]["selector"],
        )
    return CheckOutcome(
        "dkim", "fail", None, {"selectors": list(selectors), "problems": problems, "reason": "no valid key"}
    )


def _parse_tags(value: str, *, sep: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in value.split(sep):
        if "=" in part:
            key, _, val = part.partition("=")
            tags[key.strip().lower()] = val.strip()
    return tags


async def check_dmarc(domain: str) -> CheckOutcome:
    name = f"_dmarc.{domain}"
    try:
        records = await _txt_records(name)
    except dns.resolver.NXDOMAIN:
        return CheckOutcome("dmarc", "fail", None, {"reason": "no DMARC record"})
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        return CheckOutcome("dmarc", "unknown", None, {"error": f"DNS lookup failed: {type(exc).__name__}"})

    dmarc_records = [r for r in records if r.strip().lower().startswith("v=dmarc1")]
    if not dmarc_records:
        return CheckOutcome("dmarc", "fail", None, {"records": records, "reason": "no DMARC record"})
    if len(dmarc_records) > 1:
        return CheckOutcome("dmarc", "fail", " | ".join(dmarc_records), {"reason": "multiple records"})

    record = dmarc_records[0]
    tags = _parse_tags(record, sep=";")
    policy = tags.get("p", "").lower()
    pct = tags.get("pct", "100")
    evidence = {
        "policy": policy,
        "subdomain_policy": tags.get("sp"),
        "alignment_dkim": tags.get("adkim", "r"),
        "alignment_spf": tags.get("aspf", "r"),
        "pct": pct,
        "rua": tags.get("rua"),
    }
    if policy not in {"none", "quarantine", "reject"}:
        return CheckOutcome("dmarc", "fail", record, {**evidence, "reason": "missing or invalid p="})
    if policy == "none":
        return CheckOutcome(
            "dmarc", "warning", record, {**evidence, "reason": "policy is p=none (monitor only)"}
        )
    try:
        percent = int(pct)
    except ValueError:
        percent = 100
    if percent < 100:
        return CheckOutcome(
            "dmarc", "warning", record, {**evidence, "reason": f"policy applies to {percent}% of mail"}
        )
    return CheckOutcome("dmarc", "pass", record, evidence)


async def check_mx(domain: str) -> CheckOutcome:
    resolver = _resolver()
    try:
        answer = await resolver.resolve(domain, "MX", search=False)
        hosts = sorted(f"{r.preference} {r.exchange}" for r in answer)
        return CheckOutcome(
            "mx",
            "pass" if hosts else "warning",
            ", ".join(hosts) or None,
            {"records": hosts, "note": "MX records route inbound mail and do not prove outbound delivery"},
        )
    except dns.resolver.NXDOMAIN:
        return CheckOutcome("mx", "fail", None, {"reason": "domain does not exist"})
    except dns.resolver.NoAnswer:
        try:
            await resolver.resolve(domain, "A", search=False)
            return CheckOutcome(
                "mx",
                "warning",
                None,
                {"reason": "no MX records; an address record exists (implicit MX)"},
            )
        except Exception:  # noqa: BLE001
            return CheckOutcome("mx", "fail", None, {"reason": "no MX and no address record"})
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        return CheckOutcome("mx", "unknown", None, {"error": f"DNS lookup failed: {type(exc).__name__}"})


async def check_tls(account: EmailAccount) -> CheckOutcome:
    if account.transport != "smtp":
        return CheckOutcome(
            "tls",
            "not_applicable",
            None,
            {"reason": "managed by the provider API transport"},
        )

    host = str((account.provider_metadata or {}).get("smtp_host") or "")
    port = (account.provider_metadata or {}).get("smtp_port")
    security = str((account.provider_metadata or {}).get("smtp_security") or "starttls")
    if not host or not port:
        return CheckOutcome("tls", "unknown", None, {"reason": "SMTP host is not configured"})

    try:
        details = await probe_endpoint_tls(host, int(port), security)
    except PolicyViolation as exc:
        return CheckOutcome("tls", "fail", host, {**exc.details, "reason": exc.message})
    except ssl.SSLCertVerificationError as exc:
        return CheckOutcome(
            "tls",
            "fail",
            host,
            {"reason": "certificate verification failed", "detail": exc.verify_message},
        )
    except ssl.SSLError as exc:
        return CheckOutcome(
            "tls", "fail", host, {"reason": "TLS handshake failed", "detail": str(exc)[:300]}
        )
    except SMTPException as exc:
        return CheckOutcome(
            "tls", "fail", host, {"reason": "SMTP negotiation failed", "detail": str(exc)[:300]}
        )
    except OSError as exc:
        return CheckOutcome(
            "tls", "fail", host, {"reason": "connection failed", "detail": str(exc)[:300]}
        )

    return CheckOutcome("tls", "pass", host, details)


def default_selectors(account: EmailAccount) -> tuple[str, ...]:
    metadata = account.provider_metadata or {}
    configured = metadata.get("dkim_selectors")
    if isinstance(configured, list) and configured:
        return tuple(str(s) for s in configured)
    if isinstance(configured, str) and configured.strip():
        return tuple(part.strip() for part in configured.split(",") if part.strip())
    return DEFAULT_SELECTORS.get(account.provider, ())


def account_domain(account: EmailAccount) -> str:
    if account.sending_domain:
        return account.sending_domain.strip().lower().rstrip(".")
    _, _, domain = account.mailbox_email.rpartition("@")
    return domain.strip().lower().rstrip(".")


async def run_checks(
    session: AsyncSession,
    *,
    account: EmailAccount,
    actor: str,
    check_types: Iterable[str] | None = None,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> list[DomainAuthCheck]:
    domain = account_domain(account)
    if not domain:
        raise PolicyViolation("Account has no sending domain", code="sending_domain_missing")

    requested = tuple(check_types) if check_types else ("spf", "dkim", "dmarc", "mx", "tls")
    unknown = [c for c in requested if c not in CHECK_TYPES]
    if unknown:
        raise PolicyViolation(
            f"Unsupported check types: {', '.join(unknown)}", code="invalid_check_type"
        )

    outcomes: list[CheckOutcome] = []
    for check_type in requested:
        if check_type == "spf":
            outcomes.append(await check_spf(domain))
        elif check_type == "dkim":
            outcomes.append(await check_dkim(domain, default_selectors(account)))
        elif check_type == "dmarc":
            outcomes.append(await check_dmarc(domain))
        elif check_type == "mx":
            outcomes.append(await check_mx(domain))
        elif check_type == "tls":
            outcomes.append(await check_tls(account))
        elif check_type == "sender_alignment":
            outcomes.append(
                CheckOutcome(
                    "sender_alignment",
                    "not_applicable",
                    None,
                    {"reason": "requires a message observed in a controlled test mailbox"},
                )
            )

    moment = utcnow()
    expires = moment + timedelta(seconds=settings.dns_check_ttl_seconds)
    rows: list[DomainAuthCheck] = []
    for outcome in outcomes:
        row = DomainAuthCheck(
            account_id=account.id,
            domain=domain,
            check_type=outcome.check_type,
            selector=outcome.selector,
            status=outcome.status,
            observed_value=(outcome.observed_value or "")[:4000] or None,
            evidence=outcome.evidence,
            checked_at=moment,
            expires_at=expires if outcome.check_type != "sender_alignment" else None,
        )
        session.add(row)
        rows.append(row)

    await session.flush()
    await record_audit(
        session,
        actor=actor,
        action="account.domain_checks",
        target_type="email_account",
        target_id=account.id,
        request_id=request_id,
        source_ip=source_ip,
        details={
            "domain": domain,
            "results": {row.check_type: row.status for row in rows},
        },
    )
    return rows
