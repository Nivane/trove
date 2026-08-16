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


def configure_trace_store(home_dir: str | Path | None) -> None:
    """Enable the local trace store into the given trove home directory.

    Passing None disables it (tests need a deterministic off switch —
    the store home is process-global)."""
    global _configured_home
    _configured_home = str(home_dir) if home_dir is not None else None


def is_configured() -> bool:
    """Whether a trace store home has been configured."""
    return _configured_home is not None


def store_dir() -> Path | None:
    """Directory holding traces.jsonl (None when unconfigured)."""
    return Path(_configured_home) if _configured_home else None


def _path() -> Path | None:
    if not _configured_home:
        return None
    return Path(_configured_home) / TRACE_FILE_NAME


def _read_lines(path: Path) -> list[str]:
    """Raw lines of the store; lenient decode — a corrupt line must not
    blank the whole replay (it just reads as garbage and is skipped by
    callers that filter non-JSON lines)."""
    return [
        raw.decode("utf-8", errors="replace")
        for raw in path.read_bytes().splitlines()
    ]


def _parse_events(path: Path) -> list[dict[str, Any]]:
    """Parse all valid JSON events; corrupt/unparseable lines are skipped."""
    events: list[dict[str, Any]] = []
    for line in _read_lines(path):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _trim(path: Path) -> None:
    """Keep the file bounded: drop oldest events when over MAX_LINES.

    Reads bytes and decodes leniently, so historical corrupt lines cannot
    block trimming; only rewritten when actually over the limit."""
    data = path.read_bytes()
    if data.count(b"\n") <= MAX_LINES:
        return
    path.write_text("\n".join(_read_lines(path)[-MAX_LINES:]) + "\n", encoding="utf-8")


def add_event(run_id: str, event: dict[str, Any]) -> None:
    """Append one trace event for a run; failures never propagate.

    Append-only: the file is never re-read before a write, so one corrupt
    historical line cannot block new events (and each event no longer
    costs a full read-modify-write of the store)."""
    path = _path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"run_id": run_id, **event}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim(path)
    except Exception as e:
        logger.debug("Trace write failed: %s", e)


def get_run(run_id: str) -> dict[str, Any]:
    """All events of one run, in order."""
    path = _path()
    if path is None or not path.exists():
        return {"events": []}
    try:
        events = _parse_events(path)
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
        events = _parse_events(path)
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
