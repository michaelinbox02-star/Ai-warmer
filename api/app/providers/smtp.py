"""Authenticated SMTP adapter with mandatory TLS and SSRF validation."""

from __future__ import annotations

import logging
import re
import ssl
from datetime import datetime, timezone
from typing import Any

from aiosmtplib import SMTP
from aiosmtplib.errors import (
    SMTPAuthenticationError,
    SMTPConnectError,
    SMTPConnectTimeoutError,
    SMTPDataError,
    SMTPException,
    SMTPNotSupported,
    SMTPRecipientRefused,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPReadTimeoutError,
    SMTPSenderRefused,
    SMTPServerDisconnected,
    SMTPTimeoutError,
)

from app.config import settings
from app.core.errors import PolicyViolation
from app.core.mime import build_email_message
from app.core.redaction import sanitize_text
from app.core.policy_rules import Retryability
from app.providers.base import (
    ConnectionTestResult,
    CredentialRefreshResult,
    EmailProvider,
    OutboundMessage,
    ProviderCapabilities,
    ProviderContext,
    ProviderSubmissionResult,
)
from app.services.netguard import resolve_and_validate

logger = logging.getLogger(__name__)

_ENHANCED_RE = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")
_QUOTA_HINTS = ("quota", "rate limit", "too many", "throttl", "exceeded", "abuse", "limit exceeded")


def build_tls_context() -> ssl.SSLContext:
    """Certificate verification is mandatory; there is no opt-out."""

    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class SMTPProvider(EmailProvider):
    name = "smtp"
    capabilities = ProviderCapabilities(
        send=True,
        delivery_events=False,
        inbox_read=False,
        oauth=False,
        notes=(
            "Delivery events require a separate relay (DSN/bounce mailbox); provider acceptance "
            "is reported as 'submitted', never 'delivered'.",
        ),
    )

    # -- helpers ----------------------------------------------------------
    def _host(self, ctx: ProviderContext) -> str:
        host = str(ctx.metadata.get("smtp_host") or "").strip()
        if not host:
            raise PolicyViolation("SMTP host is not configured", code="smtp_not_configured")
        return host

    def _port(self, ctx: ProviderContext) -> int:
        raw = ctx.metadata.get("smtp_port", 587)
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise PolicyViolation("SMTP port is invalid", code="invalid_smtp_port") from exc

    def _security(self, ctx: ProviderContext) -> str:
        mode = str(ctx.metadata.get("smtp_security") or "starttls").lower()
        if mode not in {"starttls", "tls"}:
            raise PolicyViolation(
                "SMTP security must be 'starttls' or 'tls'", code="invalid_smtp_security"
            )
        return mode

    def _credentials(self, ctx: ProviderContext) -> tuple[str | None, str | None]:
        username = ctx.metadata.get("smtp_username") or ctx.mailbox_email
        password = ctx.secret_value("password")
        if password is None:
            raise PolicyViolation("SMTP password is missing from the secret store", code="missing_secret")
        return (str(username) if username else None), password

    async def _client(self, ctx: ProviderContext) -> tuple[SMTP, Any]:
        host = self._host(ctx)
        port = self._port(ctx)
        security = self._security(ctx)
        await resolve_and_validate(host, port)

        client = SMTP(
            hostname=host,
            port=port,
            use_tls=security == "tls",
            start_tls=security == "starttls",
            timeout=settings.worker_send_timeout_seconds,
            validate_certs=True,
            tls_context=build_tls_context(),
        )
        await client.connect()
        return client, security

    @staticmethod
    def _tls_details(client: SMTP) -> dict[str, Any]:
        details: dict[str, Any] = {}
        try:
            ssl_object = client.get_transport_info("ssl_object")
            if ssl_object is not None:
                details["tls_version"] = ssl_object.version()
                cipher = ssl_object.cipher()
                if cipher:
                    details["cipher"] = cipher[0]
                details["certificate_verified"] = True
                peer = ssl_object.getpeercert() or {}
                details["certificate_not_after"] = peer.get("notAfter")
                details["certificate_subject"] = [
                    value for rdn in peer.get("subject", ()) for key, value in rdn if key == "commonName"
                ]
        except Exception:  # noqa: BLE001 - diagnostics must never break sending
            logger.debug("Could not read TLS transport details", exc_info=True)
        return details

    # -- provider interface ----------------------------------------------
    async def validate_connection(self, ctx: ProviderContext) -> ConnectionTestResult:
        client: SMTP | None = None
        try:
            client, security = await self._client(ctx)
            details = self._tls_details(client)
            details["security"] = security
            username, password = self._credentials(ctx)
            if username and password:
                try:
                    await client.login(username, password)
                except SMTPAuthenticationError as exc:
                    return ConnectionTestResult(
                        ok=False,
                        stage="auth",
                        code="smtp_auth_failed",
                        summary="SMTP authentication was rejected.",
                        capabilities=self.capabilities,
                        details={**details, "smtp_code": exc.code},
                    )
            await client.noop()
            return ConnectionTestResult(
                ok=True,
                stage="complete",
                code="ok",
                summary="TLS handshake, certificate validation, and authentication succeeded.",
                capabilities=self.capabilities,
                details=details,
            )
        except PolicyViolation as exc:
            return ConnectionTestResult(
                ok=False,
                stage="destination",
                code=exc.code,
                summary=exc.message,
                capabilities=self.capabilities,
            )
        except ssl.SSLCertVerificationError as exc:
            return ConnectionTestResult(
                ok=False,
                stage="tls",
                code="tls_certificate_invalid",
                summary=f"TLS certificate validation failed: {sanitize_text(exc.verify_message, limit=200)}",
                capabilities=self.capabilities,
            )
        except ssl.SSLError as exc:
            return ConnectionTestResult(
                ok=False,
                stage="tls",
                code="tls_handshake_failed",
                summary="TLS handshake failed; the endpoint may not support a secure connection.",
                capabilities=self.capabilities,
                details={"error": sanitize_text(str(exc), limit=200)},
            )
        except SMTPNotSupported as exc:
            return ConnectionTestResult(
                ok=False,
                stage="tls",
                code="starttls_not_supported",
                summary="The server does not offer STARTTLS. Sending is blocked (no plaintext fallback).",
                capabilities=self.capabilities,
                details={"error": sanitize_text(str(exc), limit=200)},
            )
        except (SMTPConnectError, SMTPConnectTimeoutError, SMTPTimeoutError, OSError) as exc:
            return ConnectionTestResult(
                ok=False,
                stage="connect",
                code="smtp_connect_failed",
                summary="Could not connect to the SMTP endpoint.",
                capabilities=self.capabilities,
                details={"error": sanitize_text(str(exc), limit=200)},
            )
        except SMTPException as exc:
            return ConnectionTestResult(
                ok=False,
                stage="smtp",
                code="smtp_error",
                summary="The SMTP server returned an error during validation.",
                capabilities=self.capabilities,
                details={"error": sanitize_text(str(exc), limit=200)},
            )
        finally:
            if client is not None:
                try:
                    await client.quit()
                except Exception:  # noqa: BLE001
                    client.close()

    async def refresh_credentials(self, ctx: ProviderContext) -> CredentialRefreshResult:
        return CredentialRefreshResult(
            ok=True,
            code="not_applicable",
            summary="SMTP password credentials do not expire automatically; rotate them manually.",
        )

    async def send(self, ctx: ProviderContext, message: OutboundMessage) -> ProviderSubmissionResult:
        submitted_at = datetime.now(timezone.utc)
        stage = "prepare"
        mime = build_email_message(
            message,
            extra_headers=(
                [("X-Deliverability-Test", "controlled")] if message.kind == "diagnostic_test" else []
            )
            + [("X-Deliverability-Message-ID", str(message.message_id))],
        )
        message_id_header = mime.get("Message-ID", "")

        client: SMTP | None = None
        try:
            stage = "connect"
            client, _ = await self._client(ctx)
            stage = "auth"
            username, password = self._credentials(ctx)
            if username and password:
                await client.login(username, password)

            stage = "data"
            refused, response_text = await client.send_message(
                mime, sender=message.from_email, recipients=[message.to_email]
            )

            if refused:
                first: SMTPRecipientRefused = next(iter(refused.values()))
                retryability = self._classify_code(first.code, str(first.message))
                return ProviderSubmissionResult(
                    accepted=False,
                    retryability=retryability,
                    submitted_at=submitted_at,
                    smtp_code=first.code,
                    enhanced_smtp_code=self._enhanced(first.message),
                    diagnostic_code=f"smtp_{first.code}",
                    detail=sanitize_text(first.message),
                    metadata={"stage": "rcpt", "message_id_header": message_id_header},
                    circuit_trigger=self._circuit_for(retryability, str(first.message)),
                )

            return ProviderSubmissionResult(
                accepted=True,
                retryability="transient",  # unused when accepted
                submitted_at=submitted_at,
                provider_message_id=message_id_header or None,
                diagnostic_code="accepted",
                detail=sanitize_text(response_text, limit=200),
                metadata={
                    "stage": "accepted",
                    "message_id_header": message_id_header,
                    "provider_message_id_source": "message-id-header",
                },
            )
        except PolicyViolation as exc:
            return self._failure(
                submitted_at, "permanent", exc.code, exc.message, stage, message_id_header
            )
        except SMTPAuthenticationError as exc:
            return self._failure(
                submitted_at,
                "auth",
                "smtp_auth_failed",
                exc.message,
                "auth",
                message_id_header,
                smtp_code=exc.code,
                circuit_trigger="auth_failure",
            )
        except SMTPNotSupported as exc:
            return self._failure(
                submitted_at,
                "permanent",
                "starttls_not_supported",
                str(exc),
                "tls",
                message_id_header,
                circuit_trigger="tls_failure",
            )
        except ssl.SSLCertVerificationError as exc:
            return self._failure(
                submitted_at,
                "permanent",
                "tls_certificate_invalid",
                exc.verify_message or "certificate verification failed",
                "tls",
                message_id_header,
                circuit_trigger="tls_failure",
            )
        except ssl.SSLError as exc:
            return self._failure(
                submitted_at,
                "permanent",
                "tls_handshake_failed",
                str(exc),
                "tls",
                message_id_header,
                circuit_trigger="tls_failure",
            )
        except SMTPRecipientsRefused as exc:
            first = exc.recipients[0] if exc.recipients else None
            code = first.code if first else None
            text = first.message if first else str(exc)
            retryability = self._classify_code(code, text)
            return ProviderSubmissionResult(
                accepted=False,
                retryability=retryability,
                submitted_at=submitted_at,
                smtp_code=code,
                enhanced_smtp_code=self._enhanced(text),
                diagnostic_code=f"smtp_{code}" if code else "smtp_recipients_refused",
                detail=sanitize_text(text),
                metadata={"stage": "rcpt", "message_id_header": message_id_header},
                circuit_trigger=self._circuit_for(retryability, text),
            )
        except SMTPSenderRefused as exc:
            retryability: Retryability = "permanent" if 500 <= exc.code < 600 else "transient"
            return self._failure(
                submitted_at,
                retryability,
                f"smtp_sender_refused_{exc.code}",
                exc.message,
                "envelope",
                message_id_header,
                smtp_code=exc.code,
                enhanced_smtp_code=self._enhanced(exc.message),
            )
        except SMTPDataError as exc:
            retryability = "permanent" if exc.code >= 500 else "transient"
            return self._failure(
                submitted_at,
                retryability,
                f"smtp_data_{exc.code}",
                exc.message,
                "data",
                message_id_header,
                smtp_code=exc.code,
                enhanced_smtp_code=self._enhanced(exc.message),
                circuit_trigger=self._circuit_for(retryability, exc.message),
            )
        except (SMTPServerDisconnected, SMTPTimeoutError, SMTPReadTimeoutError) as exc:
            # The server may have accepted the DATA payload before the
            # connection failed. Never auto-resubmit an ambiguous outcome.
            ambiguous = stage == "data"
            return ProviderSubmissionResult(
                accepted=False,
                retryability="unknown" if ambiguous else "transient",
                submitted_at=submitted_at,
                diagnostic_code="ambiguous_submission" if ambiguous else "smtp_connection_lost",
                detail=sanitize_text(str(exc), limit=300),
                metadata={
                    "stage": stage,
                    "message_id_header": message_id_header,
                    "ambiguous": ambiguous,
                },
            )
        except (SMTPConnectError, ConnectionError, OSError) as exc:
            return self._failure(
                submitted_at,
                "transient",
                "smtp_connect_failed",
                str(exc),
                "connect",
                message_id_header,
            )
        except SMTPResponseException as exc:
            retryability = self._classify_code(exc.code, exc.message)
            return self._failure(
                submitted_at,
                retryability,
                f"smtp_{exc.code}",
                exc.message,
                stage,
                message_id_header,
                smtp_code=exc.code,
                enhanced_smtp_code=self._enhanced(exc.message),
                circuit_trigger=self._circuit_for(retryability, exc.message),
            )
        except SMTPException as exc:
            return self._failure(
                submitted_at,
                "transient",
                "smtp_error",
                str(exc),
                stage,
                message_id_header,
            )
        finally:
            if client is not None:
                try:
                    if client.is_connected:
                        await client.quit()
                except Exception:  # noqa: BLE001
                    client.close()

    # -- classification ----------------------------------------------------
    @staticmethod
    def _enhanced(message: str | bytes | None) -> str | None:
        if message is None:
            return None
        text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message)
        match = _ENHANCED_RE.search(text)
        return match.group(1) if match else None

    @staticmethod
    def _classify_code(code: int | None, text: str) -> Retryability:
        if code is None:
            return "transient"
        if code in (530, 534, 535, 538):
            return "auth"
        if 400 <= code < 500:
            return "transient"
        if 500 <= code < 600:
            return "permanent"
        return "transient"

    def _circuit_for(self, retryability: Retryability, text: str) -> str | None:
        if retryability == "auth":
            return "auth_failure"
        lowered = text.lower()
        if retryability in ("transient", "permanent") and any(hint in lowered for hint in _QUOTA_HINTS):
            return "quota_restriction"
        return None

    def _failure(
        self,
        submitted_at: datetime,
        retryability: Retryability,
        code: str,
        detail: str | None,
        stage: str,
        message_id_header: str | None,
        *,
        smtp_code: int | None = None,
        enhanced_smtp_code: str | None = None,
        circuit_trigger: str | None = None,
    ) -> ProviderSubmissionResult:
        return ProviderSubmissionResult(
            accepted=False,
            retryability=retryability,
            submitted_at=submitted_at,
            smtp_code=smtp_code,
            enhanced_smtp_code=enhanced_smtp_code,
            diagnostic_code=code,
            detail=sanitize_text(detail, limit=300),
            metadata={"stage": stage, "message_id_header": message_id_header},
            circuit_trigger=circuit_trigger,
        )


async def probe_endpoint_tls(
    host: str, port: int, security: str, *, timeout: float | None = None
) -> dict[str, Any]:
    """Handshake (including STARTTLS) and validate the certificate.

    No authentication or mail is performed. Raises PolicyViolation, ssl.SSLError,
    or SMTPException on failure; callers map those into diagnostics.
    """

    await resolve_and_validate(host, port)
    client = SMTP(
        hostname=host,
        port=port,
        use_tls=security == "tls",
        start_tls=security == "starttls",
        timeout=timeout or settings.worker_send_timeout_seconds,
        validate_certs=True,
        tls_context=build_tls_context(),
    )
    await client.connect()
    try:
        ssl_object = client.get_transport_info("ssl_object")
        if ssl_object is None:
            raise ssl.SSLError("No TLS session was established")
        peer = ssl_object.getpeercert() or {}
        cipher = ssl_object.cipher()
        return {
            "tls_version": ssl_object.version(),
            "cipher": cipher[0] if cipher else None,
            "certificate_verified": True,
            "certificate_not_after": peer.get("notAfter"),
            "certificate_subject": [
                value for rdn in peer.get("subject", ()) for key, value in rdn if key == "commonName"
            ],
            "certificate_issuer": [
                value for rdn in peer.get("issuer", ()) for key, value in rdn if key == "organizationName"
            ],
            "security": security,
        }
    finally:
        try:
            if client.is_connected:
                await client.quit()
        except Exception:  # noqa: BLE001
            client.close()
