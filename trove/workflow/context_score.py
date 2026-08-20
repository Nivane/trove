"""Context relevance scoring — lexical overlap between the question and
candidate context items (few-shots, terms, lessons, rules, history turns).

KB retrieval already ranks examples; this module supplies the same
relevance signal for items that arrive unordered (rules/terms/lessons)
and for per-turn conversation history, so the item-level budget trim in
assemble_context actually discriminates instead of degenerating to
retrieval order.

Scoring is bilingual: English words + CJK character bigrams are the
shared feature set, so ``question`` and a candidate share a positive
score when either language overlaps.
"""

from __future__ import annotations

import re

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_EN_RE = re.compile(r"[a-z0-9_]+")
_TURN_RE = re.compile(r"^(user|assistant):\s?(.*)$")


def text_ngrams(text: str, n: int = 2) -> set[str]:
    """中英混合 n-gram 特征集:英文按词、中文按字 bigram。

    空文本返回空集(不参与相似度计算,避免与任何特征误撞)。
    """
    low = (text or "").lower()
    feats = set(_EN_RE.findall(low))
    chars = _CJK_RE.findall(low)
    for i in range(len(chars) - n + 1):
        feats.add("".join(chars[i : i + n]))
    return feats


def relevance_score(text: str, question: str) -> float:
    """候选条目与当前问题的相关度:共享特征占问题特征的比例(0~1)。

    以问题特征数为分母的覆盖率(Jaccard 单侧)——候选只需"覆盖问题
    提到的东西"即可,不必要求问题覆盖候选(候选通常更长、含噪音)。
    """
    q = text_ngrams(question)
    if not q:
        return 0.0
    t = text_ngrams(text)
    if not t:
        return 0.0
    return len(q & t) / len(q)


def parse_history_turns(history: str) -> list[dict[str, str]]:
    """把扁平 history 字符串拆成逐轮条目,保序。

    session._conversation_history 的输出格式:每行 ``user: text`` /
    ``assistant: text``,可选 ``[summary] ...`` 摘要头。同一角色连续行
    归并为一轮;无前缀行续到上一轮。

    Returns:
        [{"role": "user"|"assistant"|"summary", "text": ...}]
    """
    turns: list[dict[str, str]] = []
    for line in (history or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[summary]"):
            turns.append({"role": "summary", "text": line[len("[summary]") :].strip()})
            continue
        m = _TURN_RE.match(line)
        if m:
            turns.append({"role": m.group(1), "text": m.group(2)})
        elif turns:
            turns[-1]["text"] = f"{turns[-1]['text']} {line}"
        else:
            turns.append({"role": "user", "text": line})
    return turns


def history_items(
    history: str,
    question: str,
    prefix: str = "turn",
) -> list["ContextItem"]:
    """历史轮 → 预算条目:score = 相关度 + 最近度加权。

    摘要([summary])内容浓缩、相关度普遍偏低,给一个小的最近度加成
    保底(摘要 = 早期轮次的高密度代表,近轮原文被其压缩掉之后仍需
    进入上下文)。
    """
    from trove.workflow.context_budget import ContextItem

    turns = parse_history_turns(history)
    n = len(turns)
    items: list[ContextItem] = []
    for i, turn in enumerate(turns):
        text = turn["text"]
        if not text:
            continue
        recency = (i + 1) / n if n else 0.0
        rel = relevance_score(text, question)
        role = turn["role"]
        head = "[summary] " if role == "summary" else f"{role}: "
        items.append(ContextItem(
            key=f"{prefix}{i}",
            text=f"{head}{text}",
            score=rel + recency * (0.4 if role == "summary" else 0.5),
        ))
    return items
