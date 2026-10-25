"""Secret store adapters.

The database only ever stores ``secret_ref``. Live OAuth tokens, SMTP
passwords, and vault keys never enter PostgreSQL, logs, API responses, or
diagnostic prompts.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from app.config import settings
from app.core.errors import AppError, CapabilityNotSupported

logger = logging.getLogger(__name__)


class SecretStoreError(AppError):
    code = "secret_store_error"
    status_code = 500


class SecretStore(ABC):
    backend: str

    @abstractmethod
    def put(self, payload: Mapping[str, Any]) -> str:
        """Store a secret payload and return its reference."""

    @abstractmethod
    def get(self, secret_ref: str) -> dict[str, Any]:
        """Load a secret payload by reference."""

    @abstractmethod
    def delete(self, secret_ref: str) -> None:
        """Remove a secret. Must be idempotent."""

    def probe(self) -> bool:
        return True


def _validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = {str(k): v for k, v in payload.items() if v is not None}
    if not cleaned:
        raise SecretStoreError("Refusing to store an empty secret payload")
    return cleaned


class EncryptedLocalVault(SecretStore):
    """Fernet-encrypted, one-file-per-secret vault.

    One file per secret avoids read-modify-write races between the API and the
    worker. Files are written atomically and with 0600 permissions where the
    platform supports it.
    """

    backend = "encrypted_local_vault"

    def __init__(self, directory: str, master_key: str | None) -> None:
        from cryptography.fernet import Fernet

        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        key = master_key or os.environ.get("VAULT_MASTER_KEY")
        if not key:
            key = self._local_master_key()
        try:
            self._fernet = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:  # pragma: no cover - startup error
            raise SecretStoreError(
                "VAULT_MASTER_KEY must be a Fernet key (generate with `python -m app.cli gen-key`)"
            ) from exc

    def _local_master_key(self) -> str:
        path = self.root / "master.key"
        if path.exists():
            return path.read_text(encoding="ascii").strip()
        key = Fernet.generate_key().decode("ascii")
        path.write_text(key + "\n", encoding="ascii")
        _restrict(path)
        logger.warning(
            "Generated a local vault master key at %s. Back it up separately from the database.",
            path,
        )
        return key

    def _path(self, secret_ref: str) -> Path:
        if not secret_ref.startswith("vault://"):
            raise SecretStoreError("Invalid vault reference")
        name = secret_ref.removeprefix("vault://")
        if not name or any(ch not in "0123456789abcdef" for ch in name):
            raise SecretStoreError("Invalid vault reference")
        return self.root / f"{name}.token"

    def put(self, payload: Mapping[str, Any]) -> str:
        cleaned = _validate_payload(payload)
        name = secrets.token_hex(16)
        token = self._fernet.encrypt(
            json.dumps(cleaned, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        target = self.root / f"{name}.token"
        with self._lock:
            fd, tmp_name = tempfile.mkstemp(dir=self.root, prefix=".tmp-", suffix=".token")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(token)
                _restrict(Path(tmp_name))
                os.replace(tmp_name, target)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        return f"vault://{name}"

    def get(self, secret_ref: str) -> dict[str, Any]:
        path = self._path(secret_ref)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise SecretStoreError("Secret not found in the vault") from exc
        from cryptography.fernet import InvalidToken

        try:
            decoded = self._fernet.decrypt(raw)
            payload = json.loads(decoded)
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise SecretStoreError(
                "Secret could not be decrypted. Check the vault key and file integrity."
            ) from exc
        if not isinstance(payload, dict):
            raise SecretStoreError("Stored secret is malformed")
        return payload

    def delete(self, secret_ref: str) -> None:
        try:
            self._path(secret_ref).unlink(missing_ok=True)
        except SecretStoreError:
            return


class OSKeyring(SecretStore):
    backend = "os_keyring"
    SERVICE = "deliverability-platform"

    def __init__(self) -> None:
        try:
            import keyring  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise CapabilityNotSupported(
                "OS keyring backend requires the `keyring` package: pip install keyring"
            ) from exc
        self._keyring = keyring

    def put(self, payload: Mapping[str, Any]) -> str:
        cleaned = _validate_payload(payload)
        username = f"secret-{secrets.token_hex(12)}"
        self._keyring.set_password(self.SERVICE, username, json.dumps(cleaned, sort_keys=True))
        return f"keyring://{self.SERVICE}/{username}"

    def get(self, secret_ref: str) -> dict[str, Any]:
        if not secret_ref.startswith("keyring://"):
            raise SecretStoreError("Invalid keyring reference")
        service, _, username = secret_ref.removeprefix("keyring://").partition("/")
        if service != self.SERVICE or not username:
            raise SecretStoreError("Invalid keyring reference")
        raw = self._keyring.get_password(service, username)
        if raw is None:
            raise SecretStoreError("Secret not found in the OS keyring")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretStoreError("Keyring secret is malformed") from exc
        if not isinstance(payload, dict):
            raise SecretStoreError("Keyring secret is malformed")
        return payload

    def delete(self, secret_ref: str) -> None:
        _, _, username = secret_ref.removeprefix("keyring://").partition("/")
        if username:
            try:
                self._keyring.delete_password(self.SERVICE, username)
            except Exception:  # noqa: BLE001 - keyring backends raise varied errors
                pass


class DockerSecrets(SecretStore):
    """Reads files from a Docker secrets mount.

    Bootstrapping a secret still requires the operator to place the file; the
    backend refuses to write when the mount is read-only.
    """

    backend = "docker_secret"

    def __init__(self, directory: str) -> None:
        self.root = Path(directory)

    def put(self, payload: Mapping[str, Any]) -> str:
        name = f"deliverability-{secrets.token_hex(8)}"
        target = self.root / name
        if not self.root.exists() or not os.access(self.root, os.W_OK):
            raise CapabilityNotSupported(
                "Docker secrets mount is not writable. Create the secret out-of-band and store its reference."
            )
        cleaned = _validate_payload(payload)
        target.write_text(json.dumps(cleaned, sort_keys=True), encoding="utf-8")
        _restrict(target)
        return f"dockersecret://{name}"

    def get(self, secret_ref: str) -> dict[str, Any]:
        if not secret_ref.startswith("dockersecret://"):
            raise SecretStoreError("Invalid docker secret reference")
        name = secret_ref.removeprefix("dockersecret://")
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise SecretStoreError("Invalid docker secret reference")
        path = self.root / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SecretStoreError("Docker secret file not found") from exc
        except json.JSONDecodeError as exc:
            raise SecretStoreError("Docker secret is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SecretStoreError("Docker secret is malformed")
        return payload

    def delete(self, secret_ref: str) -> None:
        name = secret_ref.removeprefix("dockersecret://")
        if name and "/" not in name and "\\" not in name:
            (self.root / name).unlink(missing_ok=True)


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs
        pass


@lru_cache
def get_secret_store() -> SecretStore:
    backend = settings.secret_backend
    if backend == "encrypted_local_vault":
        return EncryptedLocalVault(settings.vault_dir, settings.vault_master_key)
    if backend == "os_keyring":
        return OSKeyring()
    if backend == "docker_secret":
        return DockerSecrets(settings.docker_secret_dir)
    raise CapabilityNotSupported(f"Unsupported secret backend: {backend}")


def reset_secret_store_cache() -> None:
    get_secret_store.cache_clear()
