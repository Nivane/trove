"""图驱动的最小 runner —— 从 ``scripts/eval_bird.py`` 抽出的公共形态。

一个 evalset 题**必须走真实管线**才有判定意义;这里只搬 eval_bird 的
最小集(``GraphServices`` + ``build_graphs`` + ``WorkflowState`` +
``_run_with_steps`` 的 tracer 形态),不 import ``scripts/`` —— 脚本是入口,
不是库。P0 刻意不改 ``eval_bird.py``:它现在是这份逻辑的第二个副本,
后续可反过来复用它。

与 eval_bird 的一处刻意差异:``_run_with_steps`` **不打印** step 行。
这是一个库函数,调用方(CLI / 管理端任务)自己决定要不要叙事输出;
trace 事件照常写入。
"""

from __future__ import annotations

import time
from typing import Any

from trove.agent.session import SessionManager
from trove.llm.gateway import LLMGateway
from trove.services.authz.policy import Policy, principal_to_wire
from trove.services.datasource.catalog import CatalogService
from trove.workflow.graphs import GraphServices, build_graphs
from trove.workflow.state import WorkflowState

__all__ = ["build_eval_graph", "run_question"]


def build_eval_graph(
    *, config: Any, kb: Any, registry: Any, semantic_layer: Any = None,
) -> Any:
    """构建与交互相同的 reflection 图(不接 checkpointer,单题一次性运行)。

    服务装配与 ``scripts/eval_bird.py`` 的 setup 段逐项对应:
    ``LLMGateway(providers=config.providers)`` → ``GraphServices`` →
    ``build_graphs(services)["reflection"]``。
    """
    services = GraphServices(
        llm=LLMGateway(providers=config.providers),
        catalog=CatalogService(registry),
        connectors=registry,
        config=config,
        kb=kb,
        semantic_layer=semantic_layer,
    )
    return build_graphs(services)["reflection"]


async def _run_with_steps(
    graph: Any, state: WorkflowState, tracer: Any = None,
) -> WorkflowState:
    """逐节点执行并写 trace 事件;最终状态由各节点 update 合并得到。

    与 ``SessionManager.ask_stream`` / ``scripts/eval_bird.py::_run_with_steps``
    同一事件形态:``run`` 开始 → 每步 ``step`` → ``finish``,因此 /trace
    回放与 REPL 的链路一致。``tracer`` 仅在调用方显式传入时挂 callback
    (本模块不创建 tracer —— 单题 trace 的身份由调用方决定)。
    """
    lang = state.lang
    SessionManager._trace_run_start(state)
    merged: dict[str, Any] = state.model_dump()
    seq = 0
    last_ts = time.monotonic()
    config: dict[str, Any] = {}
    if tracer is not None:
        config["callbacks"] = [tracer.callback()]
    async for update in graph.astream(
        state, config=config or None, stream_mode="updates",
    ):
        for node_name, delta in update.items():
            if not delta:
                continue
            now = time.monotonic()
            elapsed_ms = int((now - last_ts) * 1000)
            last_ts = now
            seq += 1
            # 修正上下文:本次节点执行前挂起的反馈与轮次(与 eval_bird 一致)
            reason = merged.get("error_feedback", "")
            retry = merged.get("retry_count", 0)
            merged.update(delta)
            step = SessionManager._step_event(
                seq, node_name, delta, elapsed_ms, reason, retry, lang,
            )
            SessionManager._trace_step(state, step)
    final = WorkflowState.model_validate(merged)
    SessionManager._trace_run_finish(state.run_id, final)
    return final


async def run_question(
    graph: Any,
    question: str,
    *,
    run_id: str,
    index: int = 0,
    lang: str = "zh",
    datasource: str = "",
    principal: Any = None,
    tracer: Any = None,
) -> WorkflowState:
    """跑一道题,返回最终 ``WorkflowState``。

    主体:``principal`` 缺省时用本机可信主体(``Policy.local_admin()``)——
    与 ``scripts/eval_bird.py`` 的 headless 跑题同一口径。authz 的判定是
    **缺主体即拒绝**(I7),所以这里绝不把 ``None`` 直接塞进 state。

    ``index`` 由调用方给,用来保证同一 run 内的 ``session_id`` 不重复
    (``evalset-<run_id>-<index>``);``run_id`` 本身是本次 evalset 运行的
    身份,跨题共享。
    """
    if principal is None:
        wire: dict[str, Any] | None = principal_to_wire(Policy.local_admin())
    elif isinstance(principal, dict):
        wire = principal
    else:
        wire = principal_to_wire(principal)
    state = WorkflowState(
        session_id=f"evalset-{run_id}-{index}",
        question=question,
        run_id=run_id,
        lang=lang,
        datasource=datasource,
        principal=wire,
    )
    return await _run_with_steps(graph, state, tracer)
