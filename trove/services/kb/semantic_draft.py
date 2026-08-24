"""P4 semantic-layer drafting: LLM adds synonyms/descriptions, whitelist-validated.

Deterministic structural layer (datasets/fields/relationships/metrics) comes
from ``semantic_gen``. This module lets the LLM add **business language only**:
field synonyms and one-line descriptions (including opaque-column mapping, e.g.
A3 → district name). The consumer enforces a whitelist: any added column, table,
expression, key or relationship is stripped — the LLM owns wording, never
structure.

Reuses the init_pipeline conventions: chunked drafting, markdown-fence recovery,
graceful skip on parse failure (enrichment is optional, never blocks init).
"""
from __future__ import annotations

import logging
import re
from typing import Any

import yaml

from trove.prompts import render

logger = logging.getLogger(__name__)

DRAFT_CHUNK_TABLES = 5
DRAFT_MAX_TOKENS = 8192


def _strip_fences(text: str) -> str:
    """Strip markdown code fences (```yaml ... ```) if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]  # drop the opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return stripped


def _fenced_blocks(text: str) -> list[str]:
    """Fenced code blocks, last first(推理文本越靠后越接近最终草稿)。"""
    return list(reversed(re.findall(r"```[a-zA-Z]*\s*\n(.*?)```", text, re.DOTALL)))


def _parse_draft(response: str) -> list[dict]:
    """LLM 草稿 → annotations 列表;不可解析抛 ValueError(含 YAML 错误)。"""
    try:
        data = yaml.safe_load(_strip_fences(response)) or {}
    except Exception as e:
        raise ValueError(f"draft not parseable: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("draft must be a mapping")
    annotations = data.get("annotations")
    if not isinstance(annotations, list):
        raise ValueError("draft missing 'annotations' list")
    return annotations


def _recover_draft(response: str) -> list[dict] | None:
    """从 prose/围栏回收草稿;失败返回 None。"""
    for candidate in _fenced_blocks(response):
        try:
            return _parse_draft(candidate)
        except Exception:
            continue
    return None


def apply_annotations(model: dict, annotations: list[dict]) -> tuple[int, int]:
    """白名单合并:只认已声明 table.field,synonyms/description 落位。

    Returns:
        (applied, dropped)——dropped = 未知表/字段 + 无内容条目。
    """
    datasets = {d.get("name"): d for d in model.get("datasets", []) if isinstance(d, dict)}
    applied = 0
    dropped = 0
    for ann in annotations:
        if not isinstance(ann, dict):
            dropped += 1
            continue
        table = str(ann.get("table", "")).strip()
        dataset = datasets.get(table)
        if dataset is None:
            logger.warning("Semantic draft: unknown table %r dropped", table)
            dropped += 1
            continue
        fields = {f["name"]: f for f in dataset.get("fields", []) if isinstance(f, dict)}
        for note in ann.get("field_notes") or []:
            if not isinstance(note, dict):
                dropped += 1
                continue
            name = str(note.get("name", "")).strip()
            field = fields.get(name)
            if field is None:
                logger.warning("Semantic draft: unknown field %s.%s dropped", table, name)
                dropped += 1
                continue
            synonyms = [
                s for s in (note.get("synonyms") or [])
                if isinstance(s, str) and s.strip()
            ]
            description = note.get("description")
            if not synonyms and not (isinstance(description, str) and description.strip()):
                dropped += 1
                continue
            if synonyms:
                field.setdefault("ai_context", {})["synonyms"] = synonyms
            if isinstance(description, str) and description.strip():
                field["description"] = description.strip()
            applied += 1
    return applied, dropped


def _chunk_text(chunk: list[dict]) -> str:
    """数据集块 → 提示词文本(表名 + 列名与类型)。"""
    lines: list[str] = []
    for d in chunk:
        cols = ", ".join(
            f["name"] + (f" ({f['datatype']})" if f.get("datatype") else "")
            for f in d.get("fields", [])
        )
        lines.append(f"- {d['name']}: columns: {cols}")
    return "\n".join(lines)


async def draft_semantic_annotations(
    llm, model: str, doc: dict, *, lang: str = "en",
    chunk_size: int = DRAFT_CHUNK_TABLES,
) -> dict:
    """结构层 doc → LLM 补 synonyms/description(白名单),返回增强 doc。

    分块起草;每块解析失败/回收失败 → 保持原结构(增强是锦上添花,
    绝不阻断 init)。返回原 doc(原地合并)。
    """
    model_entry = doc["semantic_model"][0]
    datasets = model_entry.setdefault("datasets", [])
    for chunk in (datasets[i:i + chunk_size] for i in range(0, len(datasets), chunk_size)):
        messages = [
            {"role": "system", "content": render(
                "kb/semantic_draft_system", lang=lang)},
            {"role": "user", "content": render(
                "kb/semantic_draft_user", lang=lang, chunk=_chunk_text(chunk))},
        ]
        try:
            raw = await llm.chat(model=model, messages=messages,
                                 max_tokens=DRAFT_MAX_TOKENS)
        except Exception as e:
            logger.warning("Semantic draft call failed: %s", e)
            continue
        try:
            annotations = _parse_draft(raw)
        except ValueError:
            annotations = _recover_draft(raw)
        if annotations is None:
            logger.warning("Semantic draft chunk unparseable; skipped")
            continue
        applied, dropped = apply_annotations(model_entry, annotations)
        logger.debug("Semantic draft chunk: %d applied, %d dropped", applied, dropped)
    return doc


# ── refuse 起草(Phase A 语义优先:编译 MISS 的模型扩展草稿) ───────


def _vocabulary_text(model) -> str:
    """SemanticModel → 已声明词表文本(供 refuse 起草时不重复定义)。"""
    if model is None:
        return "(no model declared)"
    lines: list[str] = []
    for d in model.datasets:
        fields = ", ".join(f.name for f in d.fields) or "(no fields)"
        lines.append(f"- dataset: {d.name} (source: {d.source}) fields: {fields}")
    for m in model.metrics:
        lines.append(f"- metric: {m.name} = {m.expression}")
    return "\n".join(lines) or "(no datasets or metrics declared)"


def _parse_refusal_draft(response: str) -> dict | None:
    """refuse 草稿 YAML → draft dict;不可解析/缺 kind → None。"""
    try:
        data = yaml.safe_load(_strip_fences(response or ""))
    except Exception:
        data = None
    if not isinstance(data, dict):
        return None
    draft = data.get("draft")
    if not isinstance(draft, dict) or not isinstance(draft.get("kind"), str):
        return None
    return draft


def _recover_refusal_draft(response: str) -> dict | None:
    """从 prose/围栏回收 refuse 草稿;失败返回 None。"""
    for candidate in _fenced_blocks(response or ""):
        parsed = _parse_refusal_draft(candidate)
        if parsed is not None:
            return parsed
    return None


async def draft_refusal_extension(
    llm, model: str, question: str, plan: dict | None,
    existing_model, *, lang: str = "en",
) -> dict | None:
    """编译 MISS 的模型扩展草稿(metric/field 二选一,白名单到已声明数据集)。

    语义优先(Phase A)的 refuse 通道:LLM 只被允许在已声明数据集范围内
    草拟缺失的 metric(聚合表达式)或 field(标量维度),且绝不重复定义。
    返回 draft dict(kind/name/expression/synonyms/definition/...);
    起草失败/回收失败/kind=none → None(不写库,只产出"缺少声明"文案)。
    """
    plan_text = ""
    if plan:
        agg = plan.get("aggregation")
        cols = plan.get("answer_columns")
        conds = plan.get("conditions")
        if agg:
            plan_text += f"aggregation: {agg}\n"
        if cols:
            plan_text += f"answer_columns: {', '.join(map(str, cols))}\n"
        if conds:
            plan_text += f"conditions: {conds}\n"
    if not plan_text.strip():
        plan_text = "(none)"

    messages = [
        {"role": "system", "content": "You are drafting a minimal extension to a semantic model so a previously unanswerable question becomes answerable."},
        {"role": "user", "content": render(
            "refuse/draft", lang=lang,
            question=question,
            plan=plan_text.strip(),
            vocabulary=_vocabulary_text(existing_model),
        )},
    ]
    try:
        raw = await llm.chat(model=model, messages=messages, max_tokens=DRAFT_MAX_TOKENS)
    except Exception as e:
        logger.warning("Refusal draft call failed: %s", e)
        return None
    draft = _parse_refusal_draft(raw)
    if draft is None:
        draft = _recover_refusal_draft(raw)
    if draft is None or draft.get("kind") == "none":
        return None
    return draft