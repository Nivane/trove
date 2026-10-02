"""Read-only run replay — ``GET /v1/runs/{run_id}``.

答案溯源条与「查看依据」抽屉的读数入口:按 run_id 取回**这一次运行**的
只读轨迹(节点时间线 / LLM 调用 / 工具调用 / 终态摘要)。只读、不重跑、
不碰数据源 —— 回放不是第二次执行。

数据源两级(规格 trove-page-chat「需要新增」表):

1. ``traces.jsonl``(``trove/tracing/local.py``)—— 主用;机器可读 JSONL,
   按 run_id 分组。有界(**MAX_LINES=2000**,老事件会被裁剪),所以轨迹
   可能只剩后半段或完全没有 —— 响应以 ``source: "trace"`` 标明。
2. 会话消息 ``metadata.summary`` —— 兜底;持久、完整,但只有终态,没有
   过程。响应以 ``source: "session"`` 标明,前端据此**显著**显示「仅终态
   摘要」(不重建一份看起来像过程的东西)。

``runs/<run_id>.log`` 刻意**不做**数据源:同名覆盖写("w")、只保留最近
50 份,且含完整 prompt/输出原文 —— 那是 admin 侧的排障材料,不是用户侧
的依据。trace 一律只透出**结构化字段**(节点名/耗时/token/模型名/工具
名),prompt 与工具观测不出口。

**归属先于读数**:先按 run_id 反查所属会话(非管理员在会话库查询层就按
user_id 过滤),查不到即 404 —— 任何 trace 内容都不会在没有已归属会话的
前提下交出去;run_id 不能变成一个可枚举的读盘接口(每个 run_id 都是
uuid4,本身不可猜,但接口不靠这一点)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import get_current_user, require_scope

# 与 chat 同档:受限 token 需要 ``query`` scope 才能读回放。
router = APIRouter(dependencies=[Depends(require_scope("query"))])


def _iso(ts: Any) -> str | None:
    """Epoch seconds → ISO-8601 (UTC);拿不到就 None(三态,不编)。"""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _reduce_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Raw trace events → structured-only replay payload.

    span_start/span_end 配对成节点时间线(配不上的 = 运行中断,``status:
    "running"``);llm / tool 事件只留**结构化字段** —— messages / output /
    reasoning / arguments / observation 一律不出现在响应里。"""
    starts: dict[str, dict[str, Any]] = {}
    ends: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    llm_calls: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    for e in events:
        kind = e.get("kind")
        if kind == "span_start":
            span_id = str(e.get("span_id") or "")
            starts[span_id] = e
            order.append(span_id)
        elif kind == "span_end":
            ends[str(e.get("span_id") or "")] = e
        elif kind == "llm":
            llm_calls.append({
                "node": e.get("node") or "",
                "model": e.get("model") or "",
                "elapsed_ms": e.get("elapsed_ms"),
                "tokens": e.get("tokens"),
            })
        elif kind == "tool":
            # 工具事件按时间序挂在已开始的父 span 上(父节点必先于子事件,
            # 因此此刻 starts 里一定有它);取父节点名,不取参数/观测。
            parent = starts.get(str(e.get("parent_id") or ""), {})
            tools.append({"name": e.get("name") or "", "node": parent.get("name") or ""})

    timeline: list[dict[str, Any]] = []
    for span_id in order:
        start = starts[span_id]
        end = ends.get(span_id)
        # 嵌套深度(父 span 先于子 span 开始,链上查得到)
        depth = 0
        parent_id = start.get("parent_id")
        while parent_id and parent_id in starts:
            depth += 1
            parent_id = starts[str(parent_id)].get("parent_id")
        timeline.append({
            "name": start.get("name") or "",
            "seq": start.get("seq"),
            "depth": depth,
            "elapsed_ms": end.get("elapsed_ms") if end else None,
            "tokens": end.get("tokens") if end else None,
            "status": "ok" if end else "running",
        })
    return {"timeline": timeline, "llm_calls": llm_calls, "tools": tools}


def _pick_model(summary: dict[str, Any], llm_calls: list[dict[str, Any]]) -> str:
    """终态摘要的 model 优先(``_state_summary`` 的 gen_sql 模型,含义确定);
    旧 trace 的 summary 没有这个键时,退到 gen 节点的 LLM 调用记录 ——
    仍然是「这条 SQL 是哪个模型写的」这一件事,不是猜。"""
    model = str(summary.get("model") or "")
    if model:
        return model
    for call in llm_calls:
        if call.get("model") and "gen" in str(call.get("node") or ""):
            return str(call["model"])
    return ""


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    """按 run_id 取只读回放:节点时间线 / 模型 / 工具调用 / 终态摘要。"""
    manager = request.app.state.session_manager
    # ① 归属裁决(先于任何盘上数据):管理员全库口径,其余按属主过滤。
    scoped_user = None if user["role"] == "admin" else str(user["id"])
    record = await manager.find_run(run_id, user_id=scoped_user)
    if record is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")

    md = record["metadata"] if isinstance(record["metadata"], dict) else {}
    summary = md.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}

    # ② trace 主用。事件存在且（有 run 事件时）归属会话一致才认 —— 对不上
    # 就不当轨迹用（宁缺毋假），退回终态摘要。
    from trove.tracing.local import get_run as _get_trace_run

    trace = _get_trace_run(run_id)
    events = trace.get("events") or []
    trace_session = str(trace.get("session_id") or "")
    use_trace = bool(events) and (
        not trace_session or trace_session == record["session_id"]
    )

    if use_trace:
        reduced = _reduce_events(events)
        finish = next(
            (e.get("summary") for e in reversed(events) if e.get("kind") == "finish"),
            None,
        )
        if isinstance(finish, dict) and finish:
            # 终态以 finish 事件为准(与消息 metadata.summary 同一份
            # _state_summary,但带 stats 合并,更全)。
            summary = finish
        model = _pick_model(summary, reduced["llm_calls"])
        return {
            "run_id": run_id,
            "source": "trace",
            "session_id": record["session_id"],
            "question": trace.get("question") or summary.get("question") or "",
            "started_at": _iso(trace.get("ts")),
            # complete = 有终态(finish 事件)。崩在中途的 run 是 False。
            "complete": isinstance(finish, dict),
            "model": model or None,
            "summary": summary,
            **reduced,
        }

    # ③ 终态兜底:只有已持久化的部分,没有过程 —— source 说清楚。
    return {
        "run_id": run_id,
        "source": "session",
        "session_id": record["session_id"],
        "question": summary.get("question") or "",
        "started_at": None,
        "complete": True,
        "model": summary.get("model") or None,
        "summary": summary,
        "timeline": [],
        "llm_calls": [],
        "tools": [],
    }
