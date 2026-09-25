"""At-rest encryption for datasource secrets (``datasources.yml``).

Defense-in-depth, not a security boundary: the real boundary stays at the
database (read-only role / column grants / row limits) and at the OS file
permissions. This module keeps datasource passwords and credential-bearing
DSNs from sitting in plaintext on disk so a leaked config file, a backup,
or a careless ``git add -f`` does not hand out database access.

Key resolution (first hit wins):

  1. ``TROVE_SECRET_KEY`` env var — a urlsafe-base64 Fernet key (32 bytes)
     or any passphrase (derived with SHA-256). Production injects this
     from a KMS / secret manager.
  2. A ``secret.key`` file beside ``datasources.yml``, created ``0600`` on
     first use. It is gitignored via ``.trove/*``.

Legacy plaintext values load as-is and are re-encrypted on the next save
(``enc:v1:`` marker distinguishes the two). If ``cryptography`` is not
installed encryption degrades to plaintext with a one-time warning — the
guard must never block startup, consistent with the rest of the read-only
funnel.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from trove.core.errors import DatasourceError

logger = logging.getLogger(__name__)

# 加密值标记:enc:v1:<fernet-token>。无前缀 = 旧版明文(读时透传,写时加密)。
_MARKER = "enc:v1:"

# 敏感键名(与 registry._SENSITIVE_KEY_RE 同款口径):账号口令/令牌/凭据。
_SENSITIVE_KEY_RE = re.compile(
    r"password|passwd|secret|token|credential|api[_-]?key", re.IGNORECASE
)

_FERNET_CACHE: dict[str, Any] = {}


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(key or ""))


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_MARKER)


# ── Key management ───────────────────────────────────────


def _coerce_env_key(raw: str) -> bytes:
    """``TROVE_SECRET_KEY`` → Fernet key bytes.

    Accepts a native Fernet key (urlsafe-base64, decodes to 32 bytes) or
    derives one deterministically from an arbitrary passphrase.
    """
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii"))
        if len(decoded) == 32:
            return raw.encode("ascii")
    except Exception:
        pass
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _write_key_file(path: Path, key: bytes) -> None:
    """Create the key file atomically with 0600; tolerate a concurrent create."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".secret-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except FileExistsError:
        pass
    except OSError as e:
        # 无写权限(只读挂载):降级明文 + 明确告警,不阻断启动。
        raise DatasourceError(
            message=f"cannot persist datasource secret key at {path}: {e}",
            datasource="",
        ) from e


def _load_fernet(key_dir: Path, *, create: bool):
    """Resolve the Fernet instance for a key directory.

    ``create=True`` (encrypt path) mints and persists a key on first use.
    ``create=False`` (decrypt path) fails if no key exists — generating a
    new key there would silently orphan every encrypted value on disk.
    Returns ``None`` only when ``cryptography`` is unavailable.
    """
    cached = _FERNET_CACHE.get(str(key_dir))
    if cached is not None:
        return cached
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning(
            "cryptography not installed — datasource secrets stay plaintext"
        )
        _FERNET_CACHE[str(key_dir)] = None
        return None

    raw = os.environ.get("TROVE_SECRET_KEY", "").strip()
    if raw:
        fernet = Fernet(_coerce_env_key(raw))
    else:
        path = key_dir / "secret.key"
        try:
            existing = path.read_bytes().strip()
        except OSError:
            existing = b""
        if existing:
            try:
                fernet = Fernet(existing)
            except Exception as e:
                raise DatasourceError(
                    message=f"invalid datasource secret key file {path}: {e}",
                    datasource="",
                ) from e
        elif not create:
            raise DatasourceError(
                message=f"datasource secret key not found at {path}; cannot "
                        "decrypt stored credentials (was it deleted or rotated?)",
                datasource="",
            )
        else:
            key = Fernet.generate_key()
            _write_key_file(path, key)
            fernet = Fernet(key)
    _FERNET_CACHE[str(key_dir)] = fernet
    return fernet


def reset_cache() -> None:
    """Drop the cached Fernet instances (tests / key rotation)."""
    _FERNET_CACHE.clear()


# ── Value-level API ──────────────────────────────────────


def encrypt_value(value: Any, key_dir: Path) -> Any:
    """Encrypt a single secret; non-str / already-encrypted / empty pass through."""
    if not isinstance(value, str) or not value:
        return value
    if is_encrypted(value):
        return value
    fernet = _load_fernet(key_dir, create=True)
    if fernet is None:
        return value  # cryptography 缺失 → 明文降级
    token = fernet.encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_MARKER}{token}"


def decrypt_value(value: Any, key_dir: Path) -> Any:
    """Decrypt a stored secret; legacy plaintext passes through unchanged."""
    if not isinstance(value, str) or not value:
        return value
    if not is_encrypted(value):
        return value
    fernet = _load_fernet(key_dir, create=False)
    if fernet is None:
        raise DatasourceError(
            message="datasource secret is encrypted but cryptography is not "
                    "installed; cannot decrypt",
            datasource="",
        )
    token = value[len(_MARKER):]
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as e:
        raise DatasourceError(
            message="cannot decrypt datasource secret (missing or rotated "
                    "secret key?)",
            datasource="",
        ) from e


def encrypt_mapping(mapping: dict[str, Any], key_dir: Path, *,
                    all_values: bool = False) -> dict[str, Any]:
    """Encrypt a dict of secrets.

    ``all_values`` (credentials bag) encrypts every string value; otherwise
    only keys matching the sensitive-key pattern are touched.
    """
    return {
        k: encrypt_value(v, key_dir) if (all_values or is_sensitive_key(k)) else v
        for k, v in mapping.items()
    }


def decrypt_mapping(mapping: dict[str, Any], key_dir: Path) -> dict[str, Any]:
    """Decrypt every marked value; plaintext values are left as-is."""
    return {k: decrypt_value(v, key_dir) for k, v in mapping.items()}
