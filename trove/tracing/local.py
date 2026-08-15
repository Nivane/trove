"""Local run-trace store — zero-config full trajectory recording.

One JSONL file ({home}/traces.jsonl) holds events grouped by run_id:
  - run:      run start (session_id, question, ts)
  - step:     one pipeline node execution (seq, node, elapsed_ms, detail)
  - llm:      one LLM call (node, model, full messages, output, elapsed_ms)
  - finish:   run summary (verdict, retry_count, error, consensus, timings)

Bounded file (oldest runs dropped). The REPL /trace command replays
recent runs — no external service required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

TRACE_FILE_NAME = "traces.jsonl"
MAX_LINES = 2000  # bounded: drop oldest events

_configured_home: str | None = None


def configure_trace_store(home_dir: str | Path) -> None:
    """Enable the local trace store into the given trove home directory."""
    global _configured_home
    _configured_home = str(home_dir)


def _path() -> Path | None:
    if not _configured_home:
        return None
    return Path(_configured_home) / TRACE_FILE_NAME


def add_event(run_id: str, event: dict[str, Any]) -> None:
    """Append one trace event for a run; failures never propagate."""
    path = _path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"run_id": run_id, **event}
        lines = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").strip().splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        if len(lines) > MAX_LINES:
            lines = lines[-MAX_LINES:]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.debug("Trace write failed: %s", e)


def get_run(run_id: str) -> dict[str, Any]:
    """All events of one run, in order."""
    path = _path()
    if path is None or not path.exists():
        return {"events": []}
    try:
        events = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        run_events = [e for e in events if e.get("run_id") == run_id]
        run_info = next((e for e in run_events if e.get("kind") == "run"), {})
        return {"events": run_events, **{k: v for k, v in run_info.items() if k != "kind"}}
    except Exception as e:
        logger.debug("Trace read failed: %s", e)
        return {"events": []}


def list_recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Most recent runs (newest last), each with question + finish summary."""
    path = _path()
    if path is None or not path.exists():
        return []
    try:
        events = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        runs: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for e in events:
            rid = e.get("run_id", "")
            if rid not in runs:
                runs[rid] = {"run_id": rid, "events": 0}
                order.append(rid)
            runs[rid]["events"] += 1
            if e.get("kind") == "run":
                runs[rid]["question"] = e.get("question", "")
                runs[rid]["session_id"] = e.get("session_id", "")
            if e.get("kind") == "finish":
                runs[rid]["summary"] = e.get("summary", {})
        return [runs[rid] for rid in order[-limit:]]
    except Exception as e:
        logger.debug("Trace listing failed: %s", e)
        return []
