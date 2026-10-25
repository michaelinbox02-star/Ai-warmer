"""SSRF protection for operator-supplied SMTP destinations (spec section 10)."""

from __future__ import annotations

import pytest

from app.core.errors import PolicyViolation
from app.services import netguard


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.5",
        "172.16.4.4",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "not-an-ip",
    ],
)
def test_blocked_destinations(address):
    assert netguard.is_blocked_ip(address) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "203.0.114.10", "2606:4700:4700::1111"])
def test_public_destinations_are_allowed(address):
    assert netguard.is_blocked_ip(address) is False


@pytest.mark.parametrize(
    "host",
    [
        "smtp.example.com",
        "mail.example.co.uk",
        "smtp-relay.example.com",
        "8.8.8.8",
        "SMTP.EXAMPLE.COM.",
    ],
)
def test_valid_hostnames(host):
    assert netguard.validate_hostname_syntax(host)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "metadata.google.internal",
        "smtp.internal",
        "server.local",
        "mail.home.arpa",
        "intranet",
        "-bad.example.com",
        "bad-.example.com",
        "exa mple.com",
        "[::1]",
        "*.example.com",
        "",
    ],
)
def test_invalid_or_blocked_hostnames(host):
    with pytest.raises(PolicyViolation):
        netguard.validate_hostname_syntax(host)


def test_smtp_ports_are_restricted():
    for port in (25, 465, 587, 2525):
        assert netguard.validate_port(port) == port
    for port in (22, 80, 443, 1025, 0, 65536):
        with pytest.raises(PolicyViolation):
            netguard.validate_port(port)


@pytest.mark.asyncio
async def test_resolution_rejects_any_private_address(monkeypatch):
    monkeypatch.setattr(
        netguard,
        "_resolve",
        lambda host, port: ["93.184.216.34", "10.0.0.9"],
    )
    with pytest.raises(PolicyViolation) as excinfo:
        await netguard.resolve_and_validate("smtp.example.com", 587)
    assert excinfo.value.code == "blocked_smtp_destination"
    assert excinfo.value.details["blocked_addresses"] == ["10.0.0.9"]


@pytest.mark.asyncio
async def test_resolution_accepts_all_public_addresses(monkeypatch):
    monkeypatch.setattr(
        netguard,
        "_resolve",
        lambda host, port: ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    )
    destination = await netguard.resolve_and_validate("smtp.example.com", 465)
    assert destination.host == "smtp.example.com"
    assert destination.port == 465
    assert destination.primary_ip == "93.184.216.34"
    assert len(destination.addresses) == 2


@pytest.mark.asyncio
async def test_resolution_errors_are_policy_violations(monkeypatch):
    import socket

    def fail(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(netguard, "_resolve", fail)
    with pytest.raises(PolicyViolation) as excinfo:
        await netguard.resolve_and_validate("smtp.example.com", 587)
    assert excinfo.value.code == "smtp_host_unresolvable"


@pytest.mark.asyncio
async def test_empty_resolution_is_rejected(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: [])
    with pytest.raises(PolicyViolation) as excinfo:
        await netguard.resolve_and_validate("smtp.example.com", 587)
    assert excinfo.value.code == "smtp_host_unresolvable"
