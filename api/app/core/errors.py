"""Typed application errors with a stable JSON shape."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    code = "internal_error"
    status_code = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}


class ValidationError(AppError):
    code = "validation_error"
    status_code = 422


class AuthenticationError(AppError):
    code = "authentication_required"
    status_code = 401


class PermissionDenied(AppError):
    code = "permission_denied"
    status_code = 403


class NotFound(AppError):
    code = "not_found"
    status_code = 404


class Conflict(AppError):
    code = "conflict"
    status_code = 409


class PolicyViolation(AppError):
    code = "policy_violation"
    status_code = 409


class RateLimited(AppError):
    code = "rate_limited"
    status_code = 429


class ProviderError(AppError):
    code = "provider_error"
    status_code = 502


class CapabilityNotSupported(AppError):
    code = "capability_not_supported"
    status_code = 501
