"""Datasource name rules — uniqueness hygiene at the registration surface.

Enterprise registration rules for user-supplied names:

- ``NAME_RULE_RE``: ``^[a-z0-9][a-z0-9-]{2,63}$`` — lowercase slug, no
  ``/``, ``.``, control characters or path separators (a name is joined
  into ``.trove/kb/<name>/`` paths, so path-safety is mandatory, not a
  style preference).
- Reserved words (``demo`` / ``default``) reject collisions with the
  built-in demo datasource and the ``default`` flag semantics.
- ``validate_datasource_name`` is applied ONLY to explicitly typed names
  at the admin API. URL-derived handles (remote DB names are often not
  slug-shaped, e.g. ``mini_dev``) get the weaker ``is_path_safe`` guard
  instead of the strict slug rule.
"""

from __future__ import annotations

import re
import uuid

from trove.core.errors import DatasourceError

# ^[a-z0-9]  [a-z0-9-]{2,63}$  →  total length 3..64, lowercase slug.
NAME_RULE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")

# Names that collide with the built-in demo datasource / the `default`
# flag semantics and would be confusing or shadow system behavior.
RESERVED_NAMES = frozenset({"demo", "default"})

# Deterministic namespace for backfilling ds_id from legacy yml entries.
_TROVE_NS = uuid.uuid5(uuid.NAMESPACE_URL, "trove:datasource")


def new_ds_id() -> str:
    """Fresh immutable datasource identity (32 hex chars, path-safe)."""
    return uuid.uuid4().hex


def backfill_ds_id(type_: str, name: str) -> str:
    """Deterministic ds_id for legacy persisted entries lacking one.

    ``uuid5`` keeps the id stable across reloads for an existing yml, so
    the migration is idempotent. Fresh registrations always use
    ``new_ds_id`` (random), which also breaks identity on rename/recreate
    as the enterprise model wants.
    """
    return str(uuid.uuid5(_TROVE_NS, f"{type_}:{name}"))


def is_path_safe(name: str) -> bool:
    """Weak guard for URL-derived names: must not break ``kb_dir / name``."""
    return bool(name) and name not in (".", "..") and "/" not in name and "\x00" not in name


def validate_datasource_name(name: str) -> None:
    """Enforce the strict registration rule on an explicitly typed name.

    Raises:
        DatasourceError: name violates the slug rule or is reserved.
    """
    if not NAME_RULE_RE.match(name):
        raise DatasourceError(
            message=(
                f"invalid datasource name {name!r}: must match "
                r"^[a-z0-9][a-z0-9-]{2,63}$ (lowercase letters/digits/hyphens)"
            ),
            datasource=name,
        )
    if name in RESERVED_NAMES:
        raise DatasourceError(
            message=f"datasource name {name!r} is reserved",
            datasource=name,
        )