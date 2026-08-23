"""Runtime settings policy — the admin-managed slice of agent config.

The DB (SettingsStore) holds the *runtime overrides* for a whitelisted slice
of ``AgentConfig``. agent.yml stays the static baseline; at boot and on every
admin update the stored values are applied on top (DB wins). agent.yml is
never written.

This module owns the schema: which keys exist, their types/validation, and
how they map onto the live ``AgentConfig`` object.
"""

from __future__ import annotations

from typing import Any

from trove.core.config import AgentConfig, ProviderConfig

# Sentinel the admin UI echoes back to mean "keep the stored value".
MASK = "__trove_masked_key__"

ALLOWED_LANGUAGES = ("zh", "en")
ALLOWED_REFLECT_SKIP = ("simple", "standard", "all", "off")

# key -> (config path, kind, constraint)
SETTINGS_SCHEMA: dict[str, tuple[str, str, Any]] = {
    # LLM
    "llm.default_model": ("target", "str", None),
    "llm.fast_model": ("model_fast", "str", None),
    "llm.providers": ("providers", "providers", None),
    # General
    "app.language": ("language", "enum", ALLOWED_LANGUAGES),
    "app.semantic_layer_path": ("semantic_layer_path", "path", None),
    "app.date_parser": ("date_parser", "bool", None),
    "app.explain_semantics": ("explain_semantics", "bool", None),
    "app.fast_path": ("fast_path", "bool", None),
    "app.reflect_skip": ("reflect_skip", "enum", ALLOWED_REFLECT_SKIP),
    "app.hitl": ("hitl", "bool", None),
    "app.insights": ("insights", "bool", None),
    "app.conclusion": ("conclusion", "bool", None),
    "app.result_cache": ("result_cache", "bool", None),
    "app.decompose_llm_judge": ("decompose_llm_judge", "bool", None),
    # Retention
    "retention.max_sessions_per_user": ("retention.max_sessions_per_user", "int", 0),
    "retention.active_grace_min": ("retention.active_grace_min", "int", 0),
    "retention.max_checkpoints_per_thread": ("retention.max_checkpoints_per_thread", "int", 0),
    "retention.sweep_interval_hours": ("retention.sweep_interval_hours", "int", 0),
    # Results (范例:答案表格展示行数 / 查询结果行数上限)
    "app.result_display_rows": ("result_display_rows", "range", (1, 500)),
    "app.result_max_rows": ("result_max_rows", "range", (1, 50000)),
}


def coerce_value(key: str, raw: Any, current_providers: list[dict[str, Any]] | None = None) -> tuple[Any, str | None]:
    """Validate + coerce one incoming value. Returns (value, error)."""
    row = SETTINGS_SCHEMA.get(key)
    if row is None:
        return raw, f"unknown setting: {key}"
    _, kind, constraint = row
    try:
        if kind == "str":
            value = str(raw).strip()
            if not value:
                return raw, f"{key} must not be empty"
            return value, None
        if kind == "path":
            # 目录路径;空串 = 关闭该功能(与 str 的区别:允许清空)
            return str(raw).strip(), None
        if kind == "bool":
            if isinstance(raw, bool):
                return raw, None
            if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0"):
                return raw.lower() in ("true", "1"), None
            return raw, f"{key} must be a boolean"
        if kind == "int":
            value = int(raw)
            if constraint is not None and value < constraint:
                return raw, f"{key} must be >= {constraint}"
            return value, None
        if kind == "range":
            lo, hi = constraint
            value = int(raw)
            if not (lo <= value <= hi):
                return raw, f"{key} must be between {lo} and {hi}"
            return value, None
        if kind == "enum":
            value = str(raw)
            if value not in constraint:
                return raw, f"{key} must be one of: {', '.join(constraint)}"
            return value, None
        if kind == "providers":
            return _validate_providers(raw, current_providers), None
    except (TypeError, ValueError):
        return raw, f"{key} has an invalid value"
    return raw, f"{key} has an invalid value"


def validate_values(values: dict[str, Any], current_providers: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Validate every provided key; masked api_keys resolve to the stored value."""
    coerced: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in values.items():
        value, err = coerce_value(key, raw, current_providers)
        if err is not None:
            errors.append(err)
        else:
            coerced[key] = value
    return coerced, errors


def mask_providers(providers: list[Any]) -> list[dict[str, Any]]:
    """Render providers for the API: secrets replaced by a mask marker.

    Accepts ``ProviderConfig`` objects (runtime config) or raw dicts
    (stored rows); both round-trip through the same masked shape.
    """
    out = []
    for p in providers:
        if isinstance(p, ProviderConfig):
            name, params = p.name, p.litellm_params
        else:
            name, params = str(p.get("name", "")), dict(p.get("litellm_params", {}))
        has_key = bool(params.get("api_key"))
        masked_params: dict[str, Any] = {
            "api_base": params.get("api_base", ""),
            "api_key": MASK if has_key else "",
        }
        for k, v in params.items():
            if k not in ("api_key", "api_base"):
                masked_params[k] = v
        out.append({
            "name": name,
            "litellm_params": masked_params,
            "has_api_key": has_key,
        })
    return out


def _validate_providers(raw: Any, current: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalise a providers list and resolve masked api_keys."""
    if not isinstance(raw, list):
        raise TypeError("providers must be a list")
    stored = {
        p.get("name", ""): p.get("litellm_params", {}) for p in (current or [])
    }
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise TypeError("each provider must be an object with a name")
        name = str(item.get("name", "")).strip()
        if not name:
            raise TypeError("each provider must have a name")
        params = dict(item.get("litellm_params", {}))
        if params.get("api_key") == MASK:
            params.pop("api_key", None)
            prev = stored.get(name, {})
            if prev.get("api_key"):
                params["api_key"] = prev["api_key"]
        out.append({
            "name": name,
            "litellm_params": params,
            "has_api_key": bool(params.get("api_key")),
        })
    return out


def apply_overrides(config: AgentConfig, overrides: dict[str, Any]) -> None:
    """Mutate ``config`` in place from stored settings (DB wins over yaml)."""
    if not overrides:
        return
    for key, raw in overrides.items():
        row = SETTINGS_SCHEMA.get(key)
        if row is None:
            continue
        path, kind, _ = row
        if kind == "providers":
            raw = raw.get("value", raw) if isinstance(raw, dict) else raw
            if isinstance(raw, list):
                config.providers = [
                    ProviderConfig(
                        name=p.get("name", ""),
                        litellm_params=p.get("litellm_params", {}),
                    )
                    for p in raw
                    if isinstance(p, dict) and p.get("name")
                ]
            continue
        _set(config, path, raw)


def _set(config: AgentConfig, path: str, value: Any) -> None:
    obj: Any = config
    parts = path.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def effective_values(config: AgentConfig) -> dict[str, Any]:
    """Current runtime values, keyed by our flat settings schema (for GET)."""
    values: dict[str, Any] = {}
    for key, (path, kind, _constraint) in SETTINGS_SCHEMA.items():
        if kind == "providers":
            values[key] = mask_providers(config.providers)
        else:
            obj: Any = config
            cursor = path.split(".")
            for part in cursor:
                obj = getattr(obj, part)
            values[key] = obj
    return values