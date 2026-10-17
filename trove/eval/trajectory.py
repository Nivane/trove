"""生成链归因的适配器面(唯一 IO 处):run 日志 / traces.jsonl / 结构化 records。

解析规则与保真度语义见设计 §1 与 §4;纯核心在 trove/eval/chain.py。
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

from trove.eval.chain import (
    PlanFacts, Trajectory, plan_facts_from_json, plan_facts_text,
)

_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]*) = (.*)$")
_NODE_RE = re.compile(r"^\[(\d+)\] ([a-z0-9_]+)")
_TRUNC_RE = re.compile(r"…\(\d+ (?:chars|items)\)\s*$")
_MARKERS = ("│", "├", "└", "═")
#: 消费的键 → Trajectory 字段名(适配器只认这些;其余原样留在 values 里供诊断)。
#: ``available`` 用**字段**词汇,不是日志键名:Task 4 的 ``loop_state`` 与
#: Task 7 的 ``merge_trajectories`` 都按字段名查它,别名键在这里归并(R19)。
_CONSUMED: tuple[tuple[str, str], ...] = (
    ("matched_tables", "matched_tables"), ("plan", "plan"),
    ("plan_json", "plan"), ("validation_hits", "validation_hits"),
    ("error_analysis", "error_analysis"), ("rollback_target", "rollback_target"),
    ("last_rollback_target", "rollback_target"), ("retry_count", "retry_count"),
)


@dataclass(frozen=True)
class RunLogFacts:
    """一个 run 日志的原始事实:每键最后一次非空取值(多行值已拼回)。"""

    question: str
    values: dict[str, str]
    seen: frozenset[str]
    nodes: tuple[str, ...]


def parse_run_log(text: str) -> RunLogFacts:
    """逐行扫描器:前缀行要么是 key=value(新键),要么终止连行;无前缀行是连行。"""
    question = ""
    m = re.search(r"^question: (.*)$", text, re.M)
    if m:
        question = m.group(1).strip()
    values: dict[str, str] = {}
    seen: set[str] = set()
    nodes: list[str] = []
    last_key: str | None = None
    for raw in text.splitlines():
        if raw[:1] in _MARKERS:
            last_key = None
            body = raw.lstrip("│├└─═ ")
            nm = _NODE_RE.match(body)
            if nm:
                nodes.append(nm.group(2))
            km = _KEY_RE.match(body)
            if km:
                key, val = km.group(1), km.group(2)
                seen.add(key)
                last_key = key
                if val != "" or key not in values:
                    if val != "":
                        values[key] = val
                    else:
                        values.setdefault(key, "")
        elif last_key is not None:
            values[last_key] = values.get(last_key, "") + "\n" + raw
    values = {k: v.rstrip() for k, v in values.items()}
    return RunLogFacts(question=question, values=values,
                       seen=frozenset(seen), nodes=tuple(nodes))


def _is_truncated(value: str) -> bool:
    return bool(_TRUNC_RE.search(value))


def _parse_table_list(raw: str) -> tuple[str, ...] | None:
    """JSON 列表或 Python repr 列表(单引号)→ 小写元组;解析不了 → None。"""
    if raw.strip() in ("", "None"):
        return None
    parsed = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        try:
            parsed = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return None
    if not isinstance(parsed, (list, tuple)):
        return None
    return tuple(str(x).strip().lower() for x in parsed if str(x).strip())


def trajectory_from_log(facts: RunLogFacts, *, source: str = "logs") -> Trajectory:
    mt_state, mt = "absent", ()
    if "matched_tables" in facts.seen:
        parsed = _parse_table_list(facts.values.get("matched_tables", ""))
        if parsed is None:
            mt_state = "absent"
        elif parsed:
            mt_state, mt = "present", parsed
        else:
            mt_state = "empty"

    plan_text = facts.values.get("plan", "")
    plan_state = ("absent" if "plan" not in facts.seen
                  else ("present" if plan_text.strip() else "empty"))

    plan_facts: PlanFacts | None = None
    pj_truncated = False
    raw_pj = facts.values.get("plan_json", "").strip()
    if raw_pj and raw_pj != "None":
        try:
            pj = json.loads(raw_pj)
        except (json.JSONDecodeError, ValueError):
            pj = None
        if isinstance(pj, dict):
            # 合法空字典 {} 是「计划为空」,不是截断;截断只会让 json.loads 失败。
            if pj:
                plan_facts = plan_facts_from_json(pj)
        elif raw_pj.startswith("{"):
            pj_truncated = True  # 600 字符静默截断:前缀不是合法 JSON
    plan_truncated = pj_truncated or _is_truncated(plan_text)
    if plan_facts is None and plan_text.strip():
        plan_facts = plan_facts_text(plan_text, truncated=_is_truncated(plan_text))

    retry: int | None = None
    raw_retry = facts.values.get("retry_count", "").strip()
    if raw_retry:
        try:
            retry = int(raw_retry)
        except ValueError:
            retry = None

    rollback = (facts.values.get("last_rollback_target", "").strip()
                or facts.values.get("rollback_target", "").strip())

    available = frozenset(field for key, field in _CONSUMED if key in facts.seen)
    return Trajectory(
        question=facts.question, source=source,
        matched_tables=mt, matched_tables_state=mt_state,
        plan_facts=plan_facts, plan_text_state=plan_state,
        plan_truncated=plan_truncated,
        validation_hits_raw=facts.values.get("validation_hits", ""),
        error_analysis=facts.values.get("error_analysis", ""),
        rollback_target=rollback, retry_count=retry,
        available=available,
    )


def load_run_logs(runs_dir: str | Path) -> dict[str, Trajectory]:
    """目录级加载:同题多日志按 mtime 后者胜(重跑即替换,与 runlog 同语义)。"""
    out: dict[str, Trajectory] = {}
    paths = sorted(Path(runs_dir).glob("*.log"),
                   key=lambda p: (p.stat().st_mtime, p.name))
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        facts = parse_run_log(text)
        if not facts.question:
            continue
        out[facts.question] = trajectory_from_log(facts)
    return out
