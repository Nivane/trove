"""Synthetic few-shot generation — AskData 式 SQL-to-Text(纯合成,零金标)。

让 LLM 基于 schema + 统计 + 描述生成"自然语言问题 → SQL"对(每表 2-3 条,
覆盖 GROUP BY/SUM/AVG/JOIN/日期过滤/HAVING 等模式),经双重护栏校验后存
入 examples.yml(template: true),作为 gen_sql 的 few-shot:
  1. SQLGlot 语法解析失败 → 丢弃
  2. 试执行 `SELECT * FROM (<sql>) t LIMIT 1`(只取 1 行)——表/列名错误、
     方言不兼容、超时 → 丢弃
LLM 输出不可用(JSON 解析失败)整批静默跳过——合成 few-shot 是锦上添花,
确定性模板(COUNT/GROUP BY)仍是兜底。完全不使用 benchmark 的 gold SQL,
不违反禁背题约束。

schema_text/stats_suffix 同时供 /kb init 起草提示词使用(AskData 式
"profiling 结果作为写描述的硬证据"),故放在本模块,避免 CLI→services
反向依赖。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from trove.core.logging import get_logger
from trove.prompts import render

logger = get_logger(__name__)

SYNTH_PER_TABLE = 3        # 每表目标条数(提示词约束;护栏会再筛掉坏的)
SYNTH_MAX_TOKENS = 8192    # 与 init 起草一致
TRIAL_TIMEOUT_S = 10       # 单条试执行超时(慢查询静默丢弃)


def stats_suffix(stats: dict | None) -> str:
    """列统计 → 紧凑提示词后缀(统计是 LLM 写描述/生成 SQL 的硬证据)。

    形如 "[92% NULL, 3 distinct, json]" / "[75..99]" / "[14-17 chars]";
    无统计或纯平凡值(text 形状、0% NULL)不渲染。
    """
    if not stats:
        return ""
    bits = []
    nr = stats.get("null_ratio")
    if nr is not None and nr > 0:
        bits.append(f"{round(nr * 100)}% NULL")
    d = stats.get("distinct")
    if d is not None:
        bits.append(f"{d} distinct")
    shape = stats.get("shape")
    if shape and shape != "text":
        bits.append(shape)
    if stats.get("min") is not None and stats.get("max") is not None:
        bits.append(f"{stats['min']}..{stats['max']}")
    mn, mx = stats.get("min_len"), stats.get("max_len")
    if mn is not None and mx is not None:
        bits.append(f"{mn}-{mx} chars")
    return " [" + ", ".join(bits) + "]" if bits else ""


def schema_text(tables, samples: dict | None = None, stats: dict | None = None) -> str:
    """Compact schema listing for LLM prompts(init 起草 + 合成 few-shot)。

    每表一行 "name (N rows): col type — 描述 [样例; 样例] [92% NULL, json]":
    描述保留上下文,样例值帮 LLM 猜枚举含义,统计(探测自真实库)是证据——
    AskData 式"基于 profiling 总结元数据"。无描述的不透明列没有素材,
    LLM 无从瞎猜。
    """
    samples = samples or {}
    stats = stats or {}
    lines = []
    for table in tables:
        cols = []
        for c in table["columns"]:
            line = f"{c['name']} {c['type']}"
            desc = str(c.get("description", "") or "").strip()
            if desc:
                line += f" — {desc}"
            values = samples.get(table["name"], {}).get(c["name"], "")
            if values:
                shown = "; ".join(values.split("; ")[:3])
                line += f" [{shown}]"
            # probe_stats 返回 {table: {row_count, columns: {col: stats}}}
            col_stats = stats.get(table["name"], {}).get("columns", {}).get(c["name"])
            line += stats_suffix(col_stats)
            cols.append(line)
        rows = stats.get(table["name"], {}).get("row_count")
        header = f"{table['name']}"
        if rows is not None:
            header += f" ({rows} rows)"
        lines.append(f"{header}: {', '.join(cols)}")
    return "\n".join(lines)


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]  # drop the opening fence (``` or ```json)
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return stripped


def _parse_synthetic(response: str) -> list[dict]:
    """Parse the LLM's JSON response into example dicts (empty list on failure).

    Tolerant by design:合成 few-shot 是锦上添花,任何解析瑕疵都整批跳过。
    """
    try:
        data = json.loads(_strip_json_fences(response))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    examples = data.get("examples")
    if not isinstance(examples, list):
        return []
    cleaned: list[dict] = []
    for ex in examples:
        if not isinstance(ex, dict):
            continue
        question = str(ex.get("question", "")).strip()
        sql = str(ex.get("sql", "")).strip()
        if not question or not sql:
            continue
        cleaned.append({
            "question": question,
            "sql": sql,
            "tags": [str(t) for t in (ex.get("tags") or [])][:6],
        })
    return cleaned


def _sqlglot_ok(sql: str) -> bool:
    """SQLGlot 语法校验:解析失败(缺括号/错 token)直接丢弃。"""
    try:
        import sqlglot
        sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
        return True
    except Exception:
        return False


async def _trial_ok(registry: Any, datasource: str | None, sql: str) -> bool:
    """试执行护栏:`SELECT * FROM (<sql>) t LIMIT 1` 只取 1 行。

    表名/列名错误、方言不兼容、超时都会让这条被丢弃——只有真能在
    目标库跑起来的 SQL 才进 few-shot。
    """
    wrapped = f"SELECT * FROM ({sql}) t LIMIT 1"
    try:
        result = await asyncio.wait_for(
            registry.execute(wrapped, datasource), timeout=TRIAL_TIMEOUT_S,
        )
        return True
    except Exception:
        return False


async def generate_synthetic_examples(
    llm,
    model: str,
    tables: list[dict],
    *,
    samples: dict | None = None,
    stats: dict | None = None,
    lang: str = "en",
) -> list[dict]:
    """LLM 生成一组合成 few-shot(SQL-to-Text),尚未做执行护栏。

    Returns:
        [{question, sql, tags}] — 提示词/解析层面的合法条目;
        LLM 响应不可用(JSON 解析失败)时为空列表(不抛异常)。
    """
    if not tables:
        return []
    messages = [
        {"role": "system", "content": render("kb/synthetic_system", lang=lang)},
        {"role": "user", "content": render(
            "kb/synthetic_user", schema_text=schema_text(tables, samples, stats))},
    ]
    response = await llm.chat(
        model=model, messages=messages, max_tokens=SYNTH_MAX_TOKENS,
    )
    examples = _parse_synthetic(response)
    if not examples:
        logger.warning(
            "Synthetic few-shot: LLM response unusable for %d table(s); skipping",
            len(tables),
        )
    return examples


async def validate_examples(
    examples: list[dict],
    registry,
    datasource: str | None,
) -> list[dict]:
    """双重护栏(SQLGlot 语法 + 试执行 LIMIT 1),返回可入库的条目。

    每条都打上 template: True(与确定性模板同库,检索时都算 few-shot)。
    """
    kept: list[dict] = []
    for ex in examples:
        sql = str(ex.get("sql", ""))
        if not _sqlglot_ok(sql):
            logger.debug("Synthetic few-shot: sqlglot rejected %r", sql[:80])
            continue
        if registry is not None and not await _trial_ok(registry, datasource, sql):
            logger.debug("Synthetic few-shot: trial execution rejected %r", sql[:80])
            continue
        kept.append({**ex, "template": True})
    return kept
