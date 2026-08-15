"""Run-trace commands: /trace (replay latest run) | /trace list.

Replays the full local trajectory of a run: pipeline steps with
timings, every LLM call with input/output, and the final summary —
the CoT-style detail trail, zero external services.
"""

from __future__ import annotations

from trove.cli.slash_registry import SlashRegistry, SlashCommand


def _render_llm(event: dict) -> list[str]:
    lines = [
        f"  [llm] {event.get('node', '')} · {event.get('model', '')} · "
        f"{event.get('elapsed_ms', 0)}ms",
    ]
    for msg in event.get("messages", []):
        content = str(msg.get("content", ""))[:300]
        lines.append(f"    in [{msg.get('role', '')}]: {content}")
    lines.append(f"    out: {str(event.get('output', ''))[:300]}")
    return lines


def _render_run(run: dict) -> str:
    lines = [f"Trace {run.get('run_id', '')} · 问题：{run.get('question', '')}"]
    for event in run.get("events", []):
        kind = event.get("kind")
        if kind == "step":
            detail = event.get("detail", {})
            line = f"[{event.get('seq', '?')}] {event.get('node', '')} · {event.get('elapsed_ms', 0)}ms"
            if detail.get("retry"):
                line += f" · 重试#{detail['retry']}"
            if detail.get("reason"):
                line += f" · {str(detail['reason'])[:120]}"
            lines.append(line)
            if event.get("node") == "gen_sql" and detail.get("sql"):
                lines.append(f"    sql: {detail['sql'][:200]}")
        elif kind == "llm":
            lines.extend(_render_llm(event))
        elif kind == "finish":
            summary = event.get("summary", {})
            lines.append(
                f"→ 最终：verdict={summary.get('verdict', '')} "
                f"retries={summary.get('retry_count', 0)} "
                f"rows={summary.get('row_count', -1)} "
                f"error={str(summary.get('error', ''))[:80]}"
            )
    return "\n".join(lines)


def register_trace_commands(registry: SlashRegistry, context: dict) -> None:
    async def cmd_trace(args: str) -> str:
        from trove.tracing.local import get_run, list_recent_runs

        if args.strip() == "list":
            runs = list_recent_runs(limit=10)
            if not runs:
                return "暂无运行轨迹。"
            lines = [f"最近 {len(runs)} 次运行："]
            for r in runs:
                verdict = (r.get("summary") or {}).get("verdict", "?")
                lines.append(f"  · {r.get('question', '')[:40]} — verdict {verdict}")
            return "\n".join(lines)

        runs = list_recent_runs(limit=1)
        if not runs:
            return "暂无运行轨迹。提问后再试 /trace。"
        return _render_run(get_run(runs[-1]["run_id"]))

    registry.register(SlashCommand(
        name="trace",
        description="Replay the full trajectory of the latest run: /trace | /trace list",
        group="metadata",
        handler=cmd_trace,
    ))
