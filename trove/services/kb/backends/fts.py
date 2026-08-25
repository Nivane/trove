"""FTS5 文本工具 — 纯函数、叶子模块(零 kb 依赖,避免循环导入)。

预分词哲学与 ``kb.embeddings.text_features`` 一致:英文按词、中文按
单字符。SQLite FTS5 的 unicode61 tokenizer 把连续 CJK 视为一个 token
(实测 "贷款金额是多少" 匹配不到 "贷款"),这里在索引与查询两侧统一把
CJK 拆成单字符 + 空格拼接,使每字成为独立 token,查询侧同一套预处理
保证一致命中。英文保持词级,BM25 的 IDF 在词上生效。

MATCH 查询用 OR 召回(倒排命中任一 token),排序交给 bm25 —— 召回噪声
由调用方的确定性门(表锚/词重叠)过滤,不影响最终结果。
"""

from __future__ import annotations

import re
from typing import Any

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_WORD_RE = re.compile(r"[a-z0-9_]+")

# 问句框架词(去停用词):domain 词(loan/amount/region)保留 IDF 价值。
_STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "with", "to", "and", "or",
    "per", "by", "is", "are", "was", "were", "what", "how", "many", "much",
    "does", "do", "did", "from", "where", "which", "that", "this", "has",
    "have", "over", "under", "into", "each", "all", "its", "it", "be",
    "who", "when", "why", "will", "would", "should", "could", "can", "may",
    "might", "there", "their", "than", "then", "both", "but", "not", "vs",
}

# 单次 MATCH 的最大 token 数(长问句截断,防巨型 OR 查询)。
_MATCH_TOKEN_CAP = 24


def fts_tokens(text: str) -> list[str]:
    """预分词:英文词(≥2 字符、去停用词) + CJK 单字符,保持原序。"""
    low = (text or "").lower()
    toks = [
        w for w in _WORD_RE.findall(low)
        if len(w) > 1 and w not in _STOPWORDS
    ]
    toks.extend(_CJK_RE.findall(low))
    return toks


def fts_index_text(*parts: Any) -> str:
    """kb_fts.text 的索引文本:统一预分词后空格拼接。"""
    return " ".join(fts_tokens(" ".join(str(p) for p in parts if p)))


def fts_query(question: str, cap: int = _MATCH_TOKEN_CAP) -> str:
    """FTS5 MATCH 查询串:OR 召回 + token 截断(空 → 空串,调用方跳过)。"""
    toks = fts_tokens(question)[:cap]
    if not toks:
        return ""
    return " OR ".join(f'"{t}"' for t in toks)


def fts_item_text(kind: str, payload: dict) -> str:
    """按 kind 构造可检索文本(仅 search 会用的 kind 入索引)。

    example/template → question + tags + sql;lesson → pattern + note +
    sql_snippet;term/table/rule 不索引(term 检索是子串语义,rule 全量
    注入,schema_notes 走 table_notes 精确读取)。
    """
    if kind in ("example", "template"):
        return fts_index_text(
            payload.get("question"),
            *[str(t) for t in payload.get("tags", [])],
            payload.get("sql"),
        )
    if kind == "lesson":
        return fts_index_text(
            payload.get("pattern"), payload.get("note"), payload.get("sql_snippet"),
        )
    return ""


def normalize_bm(scores: dict[int, float]) -> dict[int, float]:
    """bm25 原始分(负数,越小越相关)→ 0..1(1 = 召回集内最相关)。

    归一化在召回集内做(相对排序信号),确定性、零外部依赖。
    """
    if not scores:
        return {}
    worst = max(scores.values())   # 最不相关(least negative)
    best = min(scores.values())    # 最相关(most negative)
    span = worst - best
    if span <= 1e-9:
        return {k: 1.0 for k in scores}
    return {k: (worst - v) / span for k, v in scores.items()}
