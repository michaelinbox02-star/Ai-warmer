"""Outbound destination validation (SSRF protection).

Custom SMTP hosts are operator supplied. We refuse loopback, link-local,
private, CGNAT, metadata, and otherwise non-routable destinations, and we
re-resolve immediately before connecting to shrink the DNS-rebinding window.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass

from app.core.errors import PolicyViolation

ALLOWED_SMTP_PORTS = frozenset({25, 465, 587, 2525})

_BLOCKED_V4 = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),  # documentation
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # documentation
    ipaddress.ip_network("203.0.113.0/24"),  # documentation
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
)

_BLOCKED_V6 = (
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("2001:db8::/32"),  # documentation
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("ff00::/8"),  # multicast
)

_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "kubernetes.default",
    }
)

_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")


@dataclass(frozen=True)
class ResolvedDestination:
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def primary_ip(self) -> str:
        return self.addresses[0]


def is_blocked_ip(candidate: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    try:
        ip = ipaddress.ip_address(candidate) if isinstance(candidate, str) else candidate
    except ValueError:
        return True

    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _ipv4_blocked(ip.ipv4_mapped)
        return any(ip in network for network in _BLOCKED_V6)
    return _ipv4_blocked(ip)


def _ipv4_blocked(ip: ipaddress.IPv4Address) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return True
    if ip.is_reserved or ip.is_unspecified:
        return True
    return any(ip in network for network in _BLOCKED_V4)


def validate_hostname_syntax(host: str) -> str:
    value = host.strip().rstrip(".").lower()
    if not value or len(value) > 253:
        raise PolicyViolation("SMTP host is invalid", code="invalid_smtp_host")
    if value in _BLOCKED_HOSTNAMES or value.endswith(_BLOCKED_SUFFIXES):
        raise PolicyViolation(
            "SMTP host must be a public, routable destination",
            code="blocked_smtp_destination",
        )
    if value.startswith("[") or value.endswith("]"):
        raise PolicyViolation("SMTP host must be a hostname or bare IP", code="invalid_smtp_host")

    # Reject anything that is not a plausible hostname or a bare IP.
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass

    labels = value.split(".")
    if len(labels) < 2:
        raise PolicyViolation(
            "SMTP host must be a fully qualified public hostname",
            code="invalid_smtp_host",
        )
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
    for label in labels:
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            raise PolicyViolation("SMTP host is invalid", code="invalid_smtp_host")
        if not set(label) <= allowed:
            raise PolicyViolation("SMTP host is invalid", code="invalid_smtp_host")
    return value


def validate_port(port: int) -> int:
    if port not in ALLOWED_SMTP_PORTS:
        raise PolicyViolation(
            f"SMTP port must be one of {sorted(ALLOWED_SMTP_PORTS)}",
            code="invalid_smtp_port",
        )
    return port


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        address = sockaddr[0]
        if address not in addresses:
            addresses.append(address)
    return addresses


async def resolve_and_validate(host: str, port: int) -> ResolvedDestination:
    """Resolve a destination and reject it unless every address is public."""

    normalized = validate_hostname_syntax(host)
    validate_port(port)

    loop = asyncio.get_running_loop()
    try:
        addresses = await loop.run_in_executor(None, _resolve, normalized, port)
    except socket.gaierror as exc:
        raise PolicyViolation(
            "SMTP host could not be resolved", code="smtp_host_unresolvable"
        ) from exc

    if not addresses:
        raise PolicyViolation("SMTP host did not resolve to any address", code="smtp_host_unresolvable")

    blocked = [addr for addr in addresses if is_blocked_ip(addr)]
    if blocked:
        raise PolicyViolation(
            "SMTP host resolves to a non-public address. Private, loopback, link-local, "
            "and metadata destinations are not permitted.",
            code="blocked_smtp_destination",
            details={"blocked_addresses": blocked},
        )

    return ResolvedDestination(host=normalized, port=port, addresses=tuple(addresses))
