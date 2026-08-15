"""Local LLM call log — zero-config prompt/response trace.

Every real (non-mock) gateway completion appends one JSONL line to
{home}/llm_calls.jsonl: timestamp, node/session metadata, model,
full input messages, output, and elapsed time. The REPL /llm command
reads the most recent entries — no external service required.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

LOG_FILE_NAME = "llm_calls.jsonl"
MAX_LINES = 500  # keep the file bounded (drop oldest)

# set via configure_call_log(home) at app startup; the gateway records
# only when this is configured (tests and lib users stay silent)
_configured_home: str | None = None


def configure_call_log(home_dir: str | Path) -> None:
    """Enable local call logging into the given trove home directory."""
    global _configured_home
    _configured_home = str(home_dir)


def _log_path(home_dir: str | Path) -> Path:
    return Path(home_dir) / LOG_FILE_NAME


def record_call(
    home_dir: str | Path,
    metadata: dict[str, Any],
    model: str,
    messages: list[dict[str, str]],
    output: str,
    elapsed_ms: int = 0,
) -> None:
    """Append one call entry; failures never propagate."""
    try:
        path = _log_path(home_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "metadata": metadata or {},
            "model": model,
            "messages": messages,
            "output": output,
            "elapsed_ms": elapsed_ms,
        }
        lines = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").strip().splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        if len(lines) > MAX_LINES:
            lines = lines[-MAX_LINES:]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.debug("LLM call log write failed: %s", e)


def read_recent(home_dir: str | Path, limit: int = 10) -> list[dict[str, Any]]:
    """Read the most recent call entries (newest last)."""
    path = _log_path(home_dir)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        return entries[-limit:]
    except Exception as e:
        logger.debug("LLM call log read failed: %s", e)
        return []
