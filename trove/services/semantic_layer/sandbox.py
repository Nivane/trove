"""合入前验证 —— 在**合入后的模型**上编译回放已声明的语义查询产物。

零 LLM、零查询、纯编译（设计 §5.4）。回放对象 = KB 里一切以语义查询形式
声明的东西：``decisions.yml`` 每条规则的 subject（正是 lint 的硬门测试，
这里双份跑）与语义主题的 ``resolve_topic``。产出三态裁决
（``improves`` / ``neutral`` / ``regresses``）+ 诚实的 ``not_applicable``
（没有可回放产物时不冒充 neutral）+ 异常时的 ``unknown``。

运行期任何异常 → ``verdict="unknown"`` 且**不阻断**：验证是增强,不该阻断
评审（§10）。「把原问题再问一遍」需要 LLM/planner,不在本边界内（N11）。
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

from trove.services.semantic_layer.ossie import parse_ossie
from trove.services.semantic_layer.query import (
    SemanticQuery,
    SemanticQueryError,
    build_and_compile,
)
from trove.services.semantic_layer.topics import resolve_topic

logger = logging.getLogger(__name__)

_EMPTY = {"replayed": [], "still_compiles": [], "now_broken": [],
          "was_broken_now_compiles": [], "not_applicable_reason": None,
          "reason": ""}


def _parse(text: str | None, dialect: str):
    """按**调用方的方言**挑表达式块（与 ``manage.py`` 的 ``dialect or "sqlite"`` 同款回退）。"""
    if not text or not text.strip():
        return None
    return parse_ossie(text, preferred_dialect=dialect or "sqlite")


def _rule_compiles(model, rule, dialect: str) -> tuple[bool, str]:
    q = SemanticQuery(
        metrics=list(rule.subject.metrics),
        dimensions=list(rule.subject.dimensions),
        time_grain=rule.subject.time_grain,
        filters=[dict(f) for f in rule.subject.filters],
        limit=rule.subject.limit,
    )
    try:
        build_and_compile(model, q, dialect)
        return True, ""
    except SemanticQueryError as e:
        return False, str(e)


def run_replay(
    kb_dir: Path, datasource: str, *, dialect: str,
    base_text: str | None, after_text: str | None,
) -> dict[str, Any]:
    # 深拷贝:``_EMPTY`` 的列表是模块级常量,浅拷贝会让 append 写穿到常量上,
    # 下一次调用就带着上一次的键（跨调用串味）。
    out = copy.deepcopy(_EMPTY)
    out["verdict"] = "unknown"
    try:
        base_model = _parse(base_text, dialect)
        after_model = _parse(after_text, dialect)
    except Exception as e:
        out["reason"] = f"model_parse_failed: {e}"
        return out
    if after_model is None:
        out["reason"] = "after_model_unavailable"
        return out

    # 产物收集：规则（decisions.yml）+ 主题（after 模型的 topics 声明）
    rules = []
    decisions_path = Path(kb_dir) / datasource / "decisions.yml"
    if decisions_path.exists():
        try:
            from trove.services.decision.rules import parse_document

            doc = parse_document(yaml.safe_load(decisions_path.read_text(encoding="utf-8")))
            rules = list(doc.rules)
        except Exception as e:
            out["reason"] = f"decisions_unreadable: {e}"
            return out
    topic_names = [t.name for t in (getattr(after_model, "topics", None) or [])]

    if not rules and not topic_names:
        out["verdict"] = "not_applicable"
        out["not_applicable_reason"] = "no_replayable_artifacts"
        return out

    try:
        for rule in rules:
            key = f"rule:{rule.id}"
            out["replayed"].append(key)
            after_ok, _ = _rule_compiles(after_model, rule, dialect)
            before_ok: bool | None = None
            if base_model is not None:
                before_ok, _ = _rule_compiles(base_model, rule, dialect)
            if after_ok:
                out["still_compiles"].append(key)
                if before_ok is False:
                    out["was_broken_now_compiles"].append(key)
            else:
                out["now_broken"].append(key)
        for name in topic_names:
            key = f"topic:{name}"
            out["replayed"].append(key)
            after_status = resolve_topic(after_model, name).status
            before_status = resolve_topic(base_model, name).status if base_model is not None else None
            if after_status == "ok":
                out["still_compiles"].append(key)
                # 「曾经编不出」= base 里**声明过**却解析不出作用域（域过期）;
                # base 里根本没这个主题是**新增**,不是修复 —— 与规则侧的
                # ``before_ok is False``（规则存在于 KB、base 模型编不出）同义。
                if before_status == "empty_scope":
                    out["was_broken_now_compiles"].append(key)
            else:
                out["now_broken"].append(key)
    except Exception as e:
        # 契约「运行期任何异常 → unknown 且不阻断」：回放中途出事时给一份干净的
        # unknown（不留半截清单,理由如实写进 reason）,评审照常继续。
        logger.warning("compile replay failed: %s", e)
        out = copy.deepcopy(_EMPTY)
        out["verdict"] = "unknown"
        out["reason"] = f"replay_failed: {e}"
        return out

    if out["now_broken"]:
        out["verdict"] = "regresses"
    elif out["was_broken_now_compiles"]:
        out["verdict"] = "improves"
    else:
        out["verdict"] = "neutral"
    return out
