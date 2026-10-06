"""Assembly dump — 每次 run 收尾落一份结构化清单 ``<run_id>.assembly.json``。

回答「这一问用了什么」——dsh ``--dump-config`` 的 run 级对应物:

  - ``blocks``:进了 gen prompt 的上下文块逐块逐项的成本与截断
    (few_shots / rules / … / plan / skill_injections);
  - ``tools``:gen 阶段 ToolRegistry 的每个工具(name/level/roles/lazy/
    activated/calls);
  - ``verdicts``:确定性判定(validator_hits / guard_hits / rule_hits /
    fast_path_hit)。guard_hits 是执行前 SQL 域断言(org guard 档)的判定,
    与 validator_hits(结果域)分开列:两者域不同,读者要一眼看出"这条 SQL
    在被执行前被谁拦过"。advisory 与 blocking 命中都在这里(后者还会走
    error_feedback 打回生成)。

数据全部是**既有产物**,不做二次计算:blocks 来自 ``assemble_context`` 的
detail 报告(装配时已算过的同一份 token 计数)+ skill 注入文本的 token 数;
tools 来自 gen 阶段构建的 ``ToolRegistry``;verdicts 来自 ``WorkflowState``
的既有字段。落盘点是 ``RunTracer.finish`` 这一个 run 收尾写点(不中途多次
写),与 ``.log`` 同款语义:同一 run_id 重跑覆盖、随日志一起按
``MAX_RUN_LOGS`` 裁剪(见 runlog._trim_run_logs)。

字段名一次定死(``_normalize_*`` 收口):缺失/异常一律降级为空值,装配 dump
是观测物,绝不允许因为自身形状问题打断或影响一次查询。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.tracing import local as store

logger = get_logger(__name__)

#: 清单文件名后缀(与 .log 成对的 run 级产物,见 runlog._trim_run_logs)。
ASSEMBLY_SUFFIX = ".assembly.json"

#: blocks 节允许的块名闭集(gen 链实际会装配的块;新增块要同步这里)。
BLOCK_NAMES = (
    "few_shots", "rules", "term_notes", "metrics", "entities", "lessons",
    "episodes", "history", "user_facts", "profile", "plan", "skill_injections",
)


def lazy_tool_names(registry: Any) -> set[str]:
    """快照注册表的懒工具名(agent loop 跑之前的调用点)。

    懒工具被 ``activate_lazy`` 解锁后会移出 ``_lazy_specs`` ——那份"注册为
    懒 / 是否被激活"的对照只有**跑之前**的快照 + 跑之后的 ``_lazy_specs``
    才认得出来(两份都在最终注册表里,事后无法区分)。
    """
    lazy = getattr(registry, "_lazy_specs", None) or {}
    return {str(getattr(spec, "name", "")) for spec in lazy.values()}


def tools_from_registry(
    registry: Any,
    *,
    lazy_names: set[str] | tuple[str, ...] = (),
    tool_history: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """ToolRegistry → tools 节的逐工具记录(注册顺序,即 prompt 里的顺序)。

    level/roles 直接读 ToolSpec(治理档位与角色白名单);``roles=None`` =
    不限角色,原样透出为 ``null``(与空列表"仅内部注册"不是一件事)。
    ``calls`` 取自 agent loop 的 ``tool_history``(每次真实执行的工具调用),
    未跑过 loop(异常/降级前)为 0。

    注册表内部字段(``_specs``/``_lazy_specs``)按只读方式访问:这是观测层
    读生成层的既有状态,失败(结构变了/属性不在)降级为空列表,绝不抛出。
    """
    try:
        specs = list((getattr(registry, "_specs", None) or {}).values())
        pending = list((getattr(registry, "_lazy_specs", None) or {}).values())
    except Exception:  # pragma: no cover - 防御:观测不得打断查询
        return []
    lazy_set = {str(n) for n in lazy_names}
    still_lazy = {
        str(getattr(spec, "name", "")) for spec in pending
    }
    counts = Counter(
        str(entry.get("name") or "")
        for entry in (tool_history or [])
        if isinstance(entry, dict)
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in [*specs, *pending]:
        name = str(getattr(spec, "name", ""))
        if not name or name in seen:
            continue
        seen.add(name)
        roles = getattr(spec, "roles", None)
        is_lazy = name in lazy_set
        out.append({
            "name": name,
            "level": str(getattr(spec, "level", "") or ""),
            "roles": list(roles) if roles is not None else None,
            # 非懒工具注册即对模型可见;懒工具以是否仍在 _lazy_specs 判定激活
            "lazy": is_lazy,
            "activated": (not is_lazy) or name not in still_lazy,
            "calls": int(counts.get(name, 0)),
        })
    return [_normalize_tool(t) for t in out]


def block_entry(
    name: str,
    *,
    tokens: int,
    truncated: bool = False,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """单块记录(blocks 节的唯一构造入口,形状收口在 _normalize_block)。"""
    return _normalize_block({
        "name": name, "tokens": tokens,
        "truncated": truncated, "items": items or [],
    })


def build_report(state: Any) -> dict[str, Any]:
    """run 收尾的 state → 装配清单(duck-typed:任何缺字段按空值降级)。

    不导入 workflow.state —— tracing 层不反向依赖工作流层,收尾调用点
    (``SessionManager._trace_run_finish``)把最终 state 传进来即可。
    """
    return {
        "run_id": str(getattr(state, "run_id", "") or ""),
        "datasource": str(getattr(state, "datasource", "") or ""),
        "complexity": str(getattr(state, "complexity", "") or ""),
        "blocks": _normalize_blocks(getattr(state, "assembly_blocks", None)),
        "tools": _normalize_tools(getattr(state, "assembly_tools", None)),
        "verdicts": {
            "validator_hits": _as_list(getattr(state, "validator_hits", None)),
            # org guard 的执行前判定(与 validator_hits 同形状、不同域):
            # 命中与"判不了"(triggered=None)都收,判定物原样透出不改写。
            "guard_hits": _as_list(getattr(state, "guard_hits", None)),
            "rule_hits": _as_list(getattr(state, "validation_hits", None)),
            "fast_path_hit": bool(getattr(state, "fast_path", False)),
        },
    }


def write_report(run_id: str, report: dict[str, Any]) -> Path | None:
    """落盘 ``{home}/runs/<run_id>.assembly.json``("w" 覆盖同 run 旧清单)。

    store 未配置 / 写失败 = 静默跳过(与 .log 同款:观测不得打断主流程)。
    ``default=str`` 兜底:清单里混进不可序列化对象时降级成字符串而不是炸掉。
    """
    home = store.store_dir()
    if home is None:
        return None
    try:
        runs_dir = home / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{run_id}{ASSEMBLY_SUFFIX}"
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path
    except OSError as e:
        logger.debug("Assembly dump write failed: %s", e)
        return None


# ── 形状收口(字段名一次定死)──────────────────────────────


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_int(value: Any) -> int:
    """防御式取 int:None/错型/不可解析字符串 → 0(观测物绝不抛出)。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_block(entry: Any) -> dict[str, Any]:
    e = entry if isinstance(entry, dict) else {}
    items = [
        {
            "ref": str(it.get("ref", "") or ""),
            "tokens": _as_int(it.get("tokens")),
            "truncated": bool(it.get("truncated", False)),
        }
        for it in _as_list(e.get("items"))
        if isinstance(it, dict)
    ]
    return {
        "name": str(e.get("name", "") or ""),
        "tokens": _as_int(e.get("tokens")),
        "truncated": bool(e.get("truncated", False)),
        "items": items,
    }


def _normalize_blocks(value: Any) -> list[dict[str, Any]]:
    return [_normalize_block(e) for e in _as_list(value)]


def _normalize_tool(entry: Any) -> dict[str, Any]:
    e = entry if isinstance(entry, dict) else {}
    roles = e.get("roles")
    return {
        "name": str(e.get("name", "") or ""),
        "level": str(e.get("level", "") or ""),
        "roles": [str(r) for r in roles] if isinstance(roles, (list, tuple)) else None,
        "lazy": bool(e.get("lazy", False)),
        "activated": bool(e.get("activated", False)),
        "calls": _as_int(e.get("calls")),
    }


def _normalize_tools(value: Any) -> list[dict[str, Any]]:
    return [_normalize_tool(e) for e in _as_list(value)]
