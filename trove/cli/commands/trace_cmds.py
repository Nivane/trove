"""Run-trace commands: /trace (replay latest run) | /trace list.

Replays the full local trajectory of a run: pipeline steps with
timings, every LLM call with input/output, and the final summary —
the CoT-style detail trail, zero external services.
"""

from __future__ import annotations

from trove.cli.slash_registry import SlashRegistry, SlashCommand


def _render_llm(event: dict, depth: int = 0) -> list[str]:
    prefix = "│ " * depth
    lines = [
        f"{prefix}├─ [llm] {event.get('node', '')} · {event.get('model', '')} · "
        f"{event.get('elapsed_ms', 0)}ms",
    ]
    for msg in event.get("messages", []):
        content = str(msg.get("content", ""))[:300]
        lines.append(f"{prefix}│   in [{msg.get('role', '')}]: {content}")
    lines.append(f"{prefix}│   out: {str(event.get('output', ''))[:300]}")
    return lines


def _render_tool(event: dict, depth: int = 0) -> list[str]:
    prefix = "│ " * depth
    return [
        f"{prefix}├─ [tool] {event.get('name', '')} {event.get('arguments', {})}",
        f"{prefix}│   → {str(event.get('observation', ''))[:200]}",
    ]


def _build_span_tree(events: list[dict]) -> dict:
    """span_start/span_end → 节点树;llm/tool 按 parent_id 挂到节点下。

    无 parent 的孤儿事件(旧扁平事件/崩溃残留)单列,渲染时平铺。"""
    nodes: dict[str, dict] = {}
    roots: list[dict] = []
    orphan_llms: list[dict] = []
    orphan_tools: list[dict] = []
    for e in events:
        kind = e.get("kind")
        if kind == "span_start":
            node = {
                "name": e.get("name", ""), "seq": e.get("seq", 0),
                "input": e.get("input", {}), "end": None,
                "children": [], "llms": [], "tools": [],
            }
            nodes[e["span_id"]] = node
            parent_id = e.get("parent_id")
            if parent_id and parent_id in nodes:
                nodes[parent_id]["children"].append(node)
            else:
                roots.append(node)
        elif kind == "span_end":
            node = nodes.get(e.get("span_id"))
            if node is not None:
                node["end"] = e
        elif kind == "llm":
            parent_id = e.get("parent_id")
            if parent_id and parent_id in nodes:
                nodes[parent_id]["llms"].append(e)
            else:
                orphan_llms.append(e)
        elif kind == "tool":
            parent_id = e.get("parent_id")
            if parent_id and parent_id in nodes:
                nodes[parent_id]["tools"].append(e)
            else:
                orphan_tools.append(e)
    return {"nodes": nodes, "roots": roots,
            "orphan_llms": orphan_llms, "orphan_tools": orphan_tools}


def _render_span(node: dict, depth: int) -> list[str]:
    """递归渲染一个节点 span:OTel 树风格,├─ 头、子事件/子 span 缩进。"""
    prefix = "│ " * depth
    head = f"{prefix}├─ [{node['seq']}] {node['name']}"
    if node["end"] is not None:
        head += f" · {node['end'].get('elapsed_ms', 0)}ms"
    lines = [head]
    for llm in node["llms"]:
        lines.extend(_render_llm(llm, depth + 1))
    for tool in node["tools"]:
        lines.extend(_render_tool(tool, depth + 1))
    if node["name"] == "gen_sql" and node["end"] is not None:
        sql = (node["end"].get("output") or {}).get("sql", "")
        if sql:
            lines.append(f"{prefix}│   sql: {sql[:200]}")
    for child in node["children"]:
        lines.extend(_render_span(child, depth + 1))
    return lines


def _render_run(run: dict) -> str:
    events = run.get("events", [])
    lines = [f"Trace {run.get('run_id', '')} · 问题：{run.get('question', '')}"]
    tree = _build_span_tree(events)
    if tree["nodes"]:
        # 树形渲染:节点 span + 挂载其下的 llm/tool(OTel trace 风格)
        for root in tree["roots"]:
            lines.extend(_render_span(root, depth=0))
        for llm in tree["orphan_llms"]:
            lines.extend(_render_llm(llm))
        for tool in tree["orphan_tools"]:
            lines.extend(_render_tool(tool))
    else:
        # 旧扁平渲染(无 span 事件的 run 保持兼容)
        for event in events:
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
            elif kind == "tool":
                lines.extend(_render_tool(event))
    for event in events:
        if event.get("kind") == "finish":
            summary = event.get("summary", {})
            stats = _format_stats(summary)
            lines.append(
                f"└─ 最终：verdict={summary.get('verdict', '')} "
                f"retries={summary.get('retry_count', 0)} "
                f"rows={summary.get('row_count', -1)} "
                f"error={str(summary.get('error', ''))[:80]}"
                f"{stats}"
            )
    return "\n".join(lines)


def _format_stats(summary: dict) -> str:
    """Run-level cost stats (time + LLM token usage), '' when absent."""
    parts = []
    elapsed = summary.get("total_elapsed_ms")
    if elapsed:
        parts.append(f"耗时 {elapsed}ms")
    usage = summary.get("token_usage") or {}
    if usage:
        parts.append(
            f"tokens {usage.get('prompt', 0)}+{usage.get('completion', 0)}="
            f"{usage.get('total', 0)}"
        )
    return (" · " + " · ".join(parts)) if parts else ""


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
