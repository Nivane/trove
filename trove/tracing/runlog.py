"""Per-run rich recorder — span tree + human-readable run log + verbose echo.

自研三合一观测层(无外部服务),与 Langfuse/LangSmith 平级补充:
  1. span 树:节点 span(span_start/span_end)、llm、tool 事件写入
     traces.jsonl;llm/tool 通过 parent_id 挂到所属节点 span 下,
     节点输入/输出完整进入 span(超长字段截断预览)。
  2. 详尽 run 日志:runs/{run_id}.log 人类可读叙事——每次 LLM 调用的
     完整 messages(不截断)、完整输出与 reasoning,每个工具调用的
     参数与观测;事件按时间顺序追加,llm/tool 自然落在所属节点段内。
  3. verbose:同一叙事实时回显到控制台(默认关闭)。

trace store 未配置时全部静默 no-op(测试/CI 无副作用)。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from trove.tracing import local as store

MAX_RUN_LOGS = 50          # runs/ 目录保留的最新 run 日志数
_LONG_VALUE = 600          # 单字段字符串截断长度(run 日志)
_LIST_PREVIEW = 10         # 列表/rows 预览条数(run 日志)
_SPAN_LIST_PREVIEW = 50    # span JSONL 事件的列表预览条数(稍宽松)

_LLM_FULL = 20000  # run 日志里 LLM 消息/输出的截断上限(实际等于不截断)

_registry: dict[str, "RunTracer"] = {}


def create_tracer(
    run_id: str, verbose: bool = False, stream: TextIO | None = None,
) -> "RunTracer":
    """Create and register the per-run tracer (one per run)."""
    tracer = RunTracer(run_id, verbose=verbose, stream=stream)
    _registry[run_id] = tracer
    return tracer


def get_tracer(run_id: str) -> "RunTracer | None":
    """Active tracer for a run (None after finish / when never created)."""
    return _registry.get(run_id)


def _unregister(run_id: str) -> None:
    _registry.pop(run_id, None)


def _to_dict(value: Any) -> dict[str, Any]:
    """LangGraph 回调拿到的输入输出可能是 Pydantic 状态模型,归一化为 dict。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):  # WorkflowState / GenSQLState
        return value.model_dump()
    return {"value": value}


def _truncate(value: Any, max_len: int = _LONG_VALUE,
              list_preview: int = _SPAN_LIST_PREVIEW) -> Any:
    """Deep truncation for span 事件:超长字符串/大列表只留预览。"""
    if isinstance(value, str):
        return value if len(value) <= max_len else value[:max_len] + f"…({len(value)} chars)"
    if isinstance(value, (list, tuple)):
        return [_truncate(v, max_len, list_preview) for v in list(value)[:list_preview]]
    if isinstance(value, dict):
        return {k: _truncate(v, max_len, list_preview) for k, v in value.items()}
    return value


def _fmt(value: Any, max_len: int = _LONG_VALUE) -> str:
    """run 日志里的单值渲染:字符串直出,容器走 JSON(带预览截断)。"""
    if isinstance(value, str):
        if len(value) > max_len:
            return value[:max_len] + f"…({len(value)} chars)"
        return value
    if isinstance(value, (list, tuple)):
        shown = list(value[:_LIST_PREVIEW])
        try:
            text = json.dumps(shown, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(shown)
        return text + (f" …({len(value)} items)" if len(value) > _LIST_PREVIEW else "")
    if isinstance(value, dict):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
        return text[:max_len]
    return str(value)[:max_len]


def _fmt_state(state: dict[str, Any] | None, indent: str) -> list[str]:
    """key = value 逐行渲染节点输入/输出。"""
    if not state:
        return [f"{indent}(empty)"]
    return [f"{indent}{k} = {_fmt(v)}" for k, v in state.items()]


class RunTracer:
    """Per-run recorder: JSONL span tree + run log file + optional echo."""

    def __init__(
        self, run_id: str, verbose: bool = False, stream: TextIO | None = None,
    ):
        self.run_id = run_id
        self.verbose = verbose
        self._stream = stream or sys.stdout
        self._stack: list[str] = []          # 打开的节点 span(栈顶=当前节点)
        self._spans: dict[str, float] = {}   # span_id -> monotonic 开始时间
        self._span_names: dict[str, str] = {}  # span_id -> 节点名
        self._node_seq = 0
        self._log_fh: TextIO | None = None
        self._log_path: Path | None = None
        self._started = False
        self._finished = False

    # ── 输出通道 ──────────────────────────────────────────

    def _write_event(self, event: dict[str, Any]) -> None:
        if not store.is_configured():
            return
        store.add_event(self.run_id, event)

    def _emit(self, depth: int, lines: list[str]) -> None:
        """树形输出(run 日志 + verbose 共用):`│ ` 连接祖先层级。

        depth = 当前打开的祖先 span 数;每行统一加前缀,节点段内用
        ├─ 开块、└─ 收块(OTel trace 风格的可读树)。
        未配置 store 时仅 verbose 回显。"""
        prefix = "│ " * depth
        text = "\n".join(prefix + ln for ln in lines) + "\n"
        if self._ensure_log():
            self._log_fh.write(text)
            self._log_fh.flush()
        if self.verbose:
            try:
                print(text, end="", file=self._stream, flush=True)
            except (BrokenPipeError, OSError):
                # 管道被消费方关闭(head/grep -q 等):停止回显,绝不打断主流程
                self.verbose = False

    def _ensure_log(self) -> bool:
        if self._log_fh is not None:
            return True
        home = store.store_dir()
        if home is None:
            return False
        try:
            runs_dir = home / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            self._log_path = runs_dir / f"{self.run_id}.log"
            # "w":同一 run_id 重跑(eval 反复评估同一题)时覆盖旧日志
            self._log_fh = open(self._log_path, "w", encoding="utf-8")
            return True
        except OSError:
            return False

    def _trim_run_logs(self) -> None:
        """只保留最近 MAX_RUN_LOGS 个 run 日志(按 mtime,同名平局取字典序)。"""
        home = store.store_dir()
        if home is None:
            return
        runs_dir = home / "runs"
        if not runs_dir.exists():
            return
        logs = sorted(
            runs_dir.glob("*.log"),
            key=lambda p: (p.stat().st_mtime, p.name), reverse=True,
        )
        for stale in logs[MAX_RUN_LOGS:]:
            try:
                stale.unlink()
            except OSError:
                pass

    # ── 生命周期 ──────────────────────────────────────────

    def start_run(self, meta: dict[str, Any]) -> None:
        """run 开始:kind=run 事件 + 日志头(question/model/lang/gold_sql/evidence)。

        幂等:多次调用(如 eval 与 SessionManager 各自触发)只写一次。"""
        if self._started:
            return
        self._started = True
        self._write_event({
            "kind": "run",
            "session_id": meta.get("session_id", ""),
            "question": meta.get("question", ""),
            "ts": time.time(),
            **{k: v for k, v in meta.items() if k not in ("session_id", "question", "ts")},
        })
        head = [f"════ Run {self.run_id} · {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        for key in ("question", "evidence", "gold_sql", "model", "lang"):
            if meta.get(key):
                head.append(f"{key}: {meta[key]}")
        self._emit(0, head)

    def finish(self, summary: dict[str, Any]) -> None:
        """run 结束:kind=finish 事件 + 日志页脚;注销并清理旧日志。

        幂等:崩溃路径可能重复调用(如正常 finish 后再 CRASH finish),
        只记录第一次。"""
        if self._finished:
            return
        self._finished = True
        self._write_event({"kind": "finish", "summary": summary})
        verdict = summary.get("verdict", "")
        retries = summary.get("retry_count", 0)
        rows = summary.get("row_count", -1)
        error = str(summary.get("error", ""))[:120]
        self._emit(0, [f"└─ finish: verdict={verdict} · retries={retries} · rows={rows} · error={error}"])
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None
        self._trim_run_logs()
        _unregister(self.run_id)

    # ── 节点 span ─────────────────────────────────────────

    def node_start(self, name: str, input: Any = None) -> str:
        """节点开始:开 span 并写日志段头(输入)。返回 span_id。

        input 可为 dict 或 Pydantic 状态模型(LangGraph 回调原样传入)。"""
        input = _to_dict(input)
        depth = len(self._stack)
        self._node_seq += 1
        span_id = f"{self.run_id}:{self._node_seq}"
        self._stack.append(span_id)
        self._spans[span_id] = time.monotonic()
        self._span_names[span_id] = name
        self._write_event({
            "kind": "span_start",
            "span_id": span_id,
            "parent_id": self._stack[-2] if len(self._stack) > 1 else None,
            "name": name,
            "seq": self._node_seq,
            "input": _truncate(input),
            "ts": time.time(),
        })
        lines = [f"├─ [{self._node_seq}] {name}", "├─ in:"]
        lines.extend(_fmt_state(input, "│   "))
        self._emit(depth, lines)
        return span_id

    def node_end(self, span_id: str, output: Any = None) -> None:
        """节点结束:关 span,写日志段尾(输出 + 耗时)。"""
        output = _to_dict(output)
        depth = len(self._stack)
        start = self._spans.pop(span_id, None)
        self._span_names.pop(span_id, None)
        if span_id in self._stack:
            self._stack.remove(span_id)
        elapsed_ms = int((time.monotonic() - start) * 1000) if start is not None else 0
        self._write_event({
            "kind": "span_end",
            "span_id": span_id,
            "output": _truncate(output),
            "elapsed_ms": elapsed_ms,
        })
        lines = [f"└─ out ({elapsed_ms}ms):"]
        lines.extend(_fmt_state(output, "│   "))
        self._emit(depth, lines)

    # ── 子事件(LLM 调用 / 工具调用)────────────────────────

    def llm(
        self, node: str, model: str, messages: list[dict[str, Any]], output: str,
        elapsed_ms: int, temperature: float = 0.0, reasoning: str = "",
    ) -> None:
        """一次 LLM 调用:完整 messages/output/reasoning 进日志与 span 树。"""
        parent_id = self._stack[-1] if self._stack else None
        depth = len(self._stack)
        self._write_event({
            "kind": "llm",
            "node": node,
            "model": model,
            "messages": messages,
            "output": output,
            "elapsed_ms": elapsed_ms,
            "temperature": temperature,
            "reasoning": reasoning[:1000],
            "parent_id": parent_id,
        })
        lines = [f"├─ · llm {model} · {elapsed_ms}ms · temp {temperature}"]
        for msg in messages:
            lines.append(f"│   [{msg.get('role', '?')}]")
            content = str(msg.get("content", ""))
            lines.append(f"│     {_fmt(content, max_len=_LLM_FULL)}")
        lines.append("│   [output]")
        lines.append(f"│     {_fmt(output, max_len=_LLM_FULL)}")
        if reasoning:
            lines.append("│   [reasoning]")
            lines.append(f"│     {_fmt(reasoning, max_len=_LLM_FULL)}")
        self._emit(depth, lines)

    def tool(self, name: str, arguments: dict[str, Any], observation: str) -> None:
        """一次工具调用:参数 + 观测结果(agent loop 的"思考→观察"痕迹)。"""
        parent_id = self._stack[-1] if self._stack else None
        depth = len(self._stack)
        self._write_event({
            "kind": "tool",
            "name": name,
            "arguments": arguments,
            "observation": observation,
            "parent_id": parent_id,
        })
        try:
            args_text = json.dumps(arguments, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            args_text = str(arguments)
        lines = [f"├─ · tool {name} {args_text[:300]}"]
        lines.append(f"│     → {_fmt(observation)}")
        self._emit(depth, lines)

    def step(self, step_event: dict[str, Any]) -> None:
        """旧式 step 事件照写(向后兼容 /trace 回放;日志内容由 span 覆盖)。"""
        self._write_event({"kind": "step", **step_event})

    # ── LangGraph callback 适配 ───────────────────────────

    def callback(self):
        """LangChain BaseCallbackHandler:节点 span 自动开关(含子图/重试)。

        过滤掉根图与 __start__/__end__,只追踪真实节点;输入输出即
        LangGraph 传给节点的 state 快照与返回 delta。
        """
        from langchain_core.callbacks import BaseCallbackHandler

        tracer = self

        class _SpanHandler(BaseCallbackHandler):
            def __init__(self) -> None:
                self._open: dict[str, str] = {}  # callback run_id -> span_id

            async def on_chain_start(
                self, serialized, inputs, *, run_id, parent_run_id=None,
                tags=None, metadata=None, **kwargs,
            ) -> None:
                node = (metadata or {}).get("langgraph_node", "")
                if not node or node in ("__start__", "__end__"):
                    return
                # LangGraph 对同一节点会重复触发 callback(节点函数链):
                # 栈顶同名节点仍在执行 → 跳过,避免冗余嵌套 span
                if (tracer._stack
                        and tracer._span_names.get(tracer._stack[-1]) == node):
                    return
                self._open[run_id] = tracer.node_start(node, inputs or {})

            async def on_chain_end(self, outputs, *, run_id, **kwargs) -> None:
                span_id = self._open.pop(run_id, None)
                if span_id is not None:
                    tracer.node_end(span_id, outputs or {})

        return _SpanHandler()
