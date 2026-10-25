"""Parsing of received-message authentication headers.

Only used for controlled test mailboxes where the operator can supply the
headers of a message they received. Results are evidence, not a guarantee.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MechanismResult = Literal["pass", "fail", "softfail", "neutral", "none", "temperror", "permerror", "unknown"]

_MECHANISM_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-zA-Z]+)", re.IGNORECASE)
_PROPERTY_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9_.-]*)\s*=\s*([^\s;]+)")


@dataclass(frozen=True)
class Mechanism:
    name: str
    result: MechanismResult
    properties: dict[str, str]


@dataclass(frozen=True)
class AuthenticationSummary:
    mechanisms: dict[str, Mechanism]
    from_domain: str | None
    spf_domain: str | None
    dkim_domain: str | None
    dmarc_domain: str | None
    alignment: dict[str, str]
    raw: str

    def as_dict(self) -> dict[str, object]:
        return {
            "mechanisms": {
                name: {"result": m.result, "properties": m.properties}
                for name, m in self.mechanisms.items()
            },
            "from_domain": self.from_domain,
            "spf_domain": self.spf_domain,
            "dkim_domain": self.dkim_domain,
            "dmarc_domain": self.dmarc_domain,
            "alignment": self.alignment,
        }


def _clean_result(raw: str) -> MechanismResult:
    value = raw.lower()
    if value in {
        "pass",
        "fail",
        "softfail",
        "neutral",
        "none",
        "temperror",
        "permerror",
    }:
        return value  # type: ignore[return-value]
    return "unknown"


def _domain(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().strip("<>").lower().rstrip(".")


def _domain_of_mailbox(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().strip("<>")
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[1]
    return _domain(candidate)


def strict_align(sender_domain: str | None, auth_domain: str | None) -> bool:
    if not sender_domain or not auth_domain:
        return False
    return sender_domain.lower().rstrip(".") == auth_domain.lower().rstrip(".")


def relaxed_align(sender_domain: str | None, auth_domain: str | None) -> bool:
    if not sender_domain or not auth_domain:
        return False
    sender = sender_domain.lower().rstrip(".")
    auth = auth_domain.lower().rstrip(".")
    if sender == auth:
        return True
    return sender.endswith("." + auth) or auth.endswith("." + sender)


def parse_authentication_results(raw: str) -> AuthenticationSummary:
    """Parse one or more `Authentication-Results` header values.

    Tolerates folded headers, comments, and multiple authserv-ids. The last
    occurrence of each mechanism wins, which is the conservative choice when a
    header is repeated.
    """

    flattened = " ".join(line.strip() for line in raw.replace("\r\n", "\n").split("\n"))
    mechanisms: dict[str, Mechanism] = {}

    for segment in flattened.split(";"):
        match = _MECHANISM_RE.search(segment)
        if not match:
            continue
        name = match.group(1).lower()
        result = _clean_result(match.group(2))
        props = {
            key.lower(): value.strip('"')
            for key, value in _PROPERTY_RE.findall(segment[match.end() :])
        }
        mechanisms[name] = Mechanism(name=name, result=result, properties=props)

    dkim_props = mechanisms.get("dkim", Mechanism("dkim", "unknown", {})).properties
    # RFC 8601 allows either header.d (signing domain) or header.i (AUID);
    # Gmail typically reports only ``header.i=@domain``.
    dkim_domain = _domain(dkim_props.get("header.d")) or _domain_of_mailbox(
        dkim_props.get("header.i")
    )

    from_domain = _domain(
        mechanisms.get("dmarc", Mechanism("dmarc", "unknown", {})).properties.get("header.from")
        or dkim_props.get("header.d")
    ) or _domain_of_mailbox(dkim_props.get("header.i"))
    spf_domain = _domain_of_mailbox(
        mechanisms.get("spf", Mechanism("spf", "unknown", {})).properties.get("smtp.mailfrom")
    )
    dmarc_domain = _domain(
        mechanisms.get("dmarc", Mechanism("dmarc", "unknown", {})).properties.get("header.from")
    )

    alignment = {
        "spf_aligned": "pass" if relaxed_align(from_domain, spf_domain) else "fail",
        "dkim_aligned": "pass" if relaxed_align(from_domain, dkim_domain) else "fail",
        "dmarc_aligned": "pass" if relaxed_align(from_domain, dmarc_domain) else "fail",
    }

    return AuthenticationSummary(
        mechanisms=mechanisms,
        from_domain=from_domain,
        spf_domain=spf_domain,
        dkim_domain=dkim_domain,
        dmarc_domain=dmarc_domain,
        alignment=alignment,
        raw=flattened,
    )


def header_slice(raw_headers: str) -> str | None:
    """Extract the Authentication-Results value(s) from a raw header block."""

    values: list[str] = []
    current: str | None = None
    for line in raw_headers.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and current is not None:
            current += " " + line.strip()
            continue
        if current is not None:
            values.append(current)
            current = None
        if line.lower().startswith("authentication-results:"):
            current = line.split(":", 1)[1].strip()
    if current is not None:
        values.append(current)
    return "\n".join(values) if values else None
