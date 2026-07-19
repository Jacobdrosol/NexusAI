"""Secret handling for externally configured connections.

The dashboard and control-plane both persist connection metadata in the shared
runtime database.  Keep encryption, display redaction, and redacted-import
handling in one dependency-light module so a secret cannot be exposed by a
different API surface than the one that created it.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from cryptography.fernet import Fernet


REDACTED_VALUE = "[REDACTED]"
AUTH_SECRET_KEYS = frozenset({"api_key", "bearer_token", "password"})
CONFIG_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "bearer_token",
        "client_secret",
        "connection_string",
        "dsn",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
        "webhook_secret",
    }
)


def _fernet() -> Fernet:
    seed = (os.environ.get("NEXUSAI_SECRET_KEY") or "dev-secret-change-in-production").encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def _is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("enc:")


def encrypt_secret(value: Any) -> str:
    """Encrypt a non-empty connection secret, preserving existing ciphertext."""
    raw = str(value or "")
    if not raw:
        return ""
    if _is_encrypted(raw):
        return raw
    return "enc:" + _fernet().encrypt(raw.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: Any) -> str:
    """Decrypt a stored secret while tolerating legacy plaintext values."""
    raw = str(value or "")
    if not raw:
        return ""
    if not _is_encrypted(raw):
        return raw
    try:
        return _fernet().decrypt(raw[4:].encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def mask_secret(value: Any) -> str:
    return REDACTED_VALUE if str(value or "").strip() else ""


def _stored_secret(incoming: Any, existing: Any = None) -> tuple[bool, str]:
    """Return whether to store a value and the safe stored value.

    Blank and redacted values mean "keep the existing secret".  This lets an
    operator re-import a portable export without replacing a working local
    credential with the literal redaction marker.
    """
    raw = str(incoming or "")
    if not raw.strip() or raw == REDACTED_VALUE:
        if existing is None:
            return False, ""
        return True, str(existing)
    return True, encrypt_secret(raw)


def normalize_auth_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge auth fields and encrypt secret values before persistence."""
    base = dict(existing or {})
    for key, value in dict(payload or {}).items():
        if key in AUTH_SECRET_KEYS:
            should_store, stored = _stored_secret(value, base.get(key))
            if should_store:
                base[key] = stored
            continue
        base[key] = value
    return base


def mask_auth_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return auth payload with every credential value redacted."""
    out = dict(payload or {})
    for key in AUTH_SECRET_KEYS:
        if key in out:
            out[key] = mask_secret(out[key])
    return out


def resolve_auth_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Decrypt auth payload only for an internal connection execution path."""
    out = dict(payload or {})
    for key in AUTH_SECRET_KEYS:
        if key in out:
            out[key] = decrypt_secret(out[key])
    return out


def _normalize_config_value(value: Any, existing: Any = None, *, secret: bool = False) -> Any:
    if secret:
        should_store, stored = _stored_secret(value, existing)
        return stored if should_store else None
    if isinstance(value, dict):
        existing_dict = existing if isinstance(existing, dict) else {}
        result: dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            child_secret = normalized_key in CONFIG_SECRET_KEYS
            # HTTP headers commonly carry authorization values. Treat every
            # configured header as sensitive rather than guessing by name.
            if normalized_key == "headers" and isinstance(child, dict):
                header_existing = existing_dict.get(key) if isinstance(existing_dict.get(key), dict) else {}
                headers: dict[str, Any] = {}
                for header_name, header_value in child.items():
                    stored_header = _normalize_config_value(
                        header_value,
                        header_existing.get(header_name),
                        secret=True,
                    )
                    if stored_header is not None:
                        headers[str(header_name)] = stored_header
                result[key] = headers
                continue
            normalized = _normalize_config_value(child, existing_dict.get(key), secret=child_secret)
            if normalized is not None:
                result[key] = normalized
        return result
    if isinstance(value, list):
        return [_normalize_config_value(item) for item in value]
    return value


def normalize_connection_config(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Encrypt configured connection secrets while preserving redacted fields."""
    normalized = _normalize_config_value(dict(payload or {}), dict(existing or {}))
    return normalized if isinstance(normalized, dict) else {}


def _resolve_config_value(value: Any, *, secret: bool = False) -> Any:
    if secret:
        return decrypt_secret(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key == "headers" and isinstance(child, dict):
                result[key] = {str(name): decrypt_secret(item) for name, item in child.items()}
            else:
                result[key] = _resolve_config_value(child, secret=normalized_key in CONFIG_SECRET_KEYS)
        return result
    if isinstance(value, list):
        return [_resolve_config_value(item) for item in value]
    return value


def resolve_connection_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Decrypt stored connection config for an internal execution path only."""
    resolved = _resolve_config_value(dict(payload or {}))
    return resolved if isinstance(resolved, dict) else {}


def _mask_config_value(value: Any, *, secret: bool = False) -> Any:
    if secret:
        return mask_secret(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key == "headers" and isinstance(child, dict):
                result[key] = {str(name): mask_secret(item) for name, item in child.items()}
            else:
                result[key] = _mask_config_value(child, secret=normalized_key in CONFIG_SECRET_KEYS)
        return result
    if isinstance(value, list):
        return [_mask_config_value(item) for item in value]
    return value


def mask_connection_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Return connection config suitable for UI, API, exports, or task context."""
    masked = _mask_config_value(dict(payload or {}))
    return masked if isinstance(masked, dict) else {}
