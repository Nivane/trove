"""泄漏检查 —— 冷启动生成的问题不得与 KB few-shot 示例重合。

三把判据、一把尺子:

- **尺子** = ``trove.services.kb.ledger.normalize_question``(``\\W`` 保留
  ``_``,只折真实标点)。刻意**不用** ``trove/eval/text.py::norm_question``:
  那是评测对账键,连 ``_`` 也折成空格 —— 用它判泄漏,「问列名」与「问自然
  语言」会被判成同一句。
- **判据优先级** = ``normalized``(归一后完全相同)> ``bigram``(问题含 CJK
  时的近重复尺子)> ``word``(否则用词重叠)。同一判据内按示例文件顺序取
  第一条命中。
- **只标记,不拒绝**:命中只写进条目 ``leakage``/``leakage_hit``,条目仍是
  ``pending`` —— 泄漏是给人看的信息,不是自动丢弃的依据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from trove.services.kb.ledger import normalize_question

#: 中文近重复阈值:字符 bigram 的 Jaccard 下界。
BIGRAM_NEAR_DUPLICATE = 0.80

#: 英文近重复阈值:与 ``graphs.KB_EXACT_OVERLAP`` 同一个词重叠尺子。
WORD_NEAR_DUPLICATE = 0.95

#: 问题含 CJK(CJK 统一表意文字)即走 bigram 判据。
_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")


@dataclass(frozen=True)
class LeakageHit:
    """一条泄漏命中:``question`` 是命中的**示例**问句(不是被测问句)。"""

    question: str
    kind: str
    score: float

    def note(self) -> str:
        """人读的命中说明;``normalized`` 是确定命中,不带分数。"""
        text = f"{self.kind}: {self.question}"
        if self.kind in ("word", "bigram"):
            text += f" ({self.score:.2f})"
        return text


def word_overlap(a: str, b: str) -> float:
    """两个问题的词重叠率(min 侧归一)—— 逐字复制 ``graphs._word_overlap``。

    跨层不 import ``trove.workflow.graphs``(图模块会拖进整条工作流依赖);
    副本与原文的一致性由 ``test_word_overlap_matches_graphs_ruler`` 钉住。
    """
    w1 = set(re.findall(r"[a-z0-9_]+", (a or "").lower()))
    w2 = set(re.findall(r"[a-z0-9_]+", (b or "").lower()))
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / min(len(w1), len(w2))


def bigram_jaccard(a: str, b: str) -> float:
    """字符 bigram 集合的 Jaccard;任一字符串(去空白后)< 2 字符 → 0.0。

    **空白在成 bigram 之前剥掉**。调用方传 ledger 归一后的串,而归一化只在
    标点处留空格 —— 若空格也进 bigram,「按地区统计 贷款总额」与「按地区
    统计贷款总额」会因一个空白多出不同的 bigram,只有标点/空白不同的问句
    就检测不成近重复(R8:这类差异必须算近重复)。
    """
    def grams(text: str) -> set[str]:
        compact = re.sub(r"\s+", "", text or "")
        return {compact[i:i + 2] for i in range(len(compact) - 1)}

    g1, g2 = grams(a), grams(b)
    if not g1 or not g2:
        return 0.0
    return len(g1 & g2) / len(g1 | g2)


def load_example_questions(path: Path) -> list[str]:
    """读取 ``examples.yml`` 里的问句;**确认与待确认都算**。

    待确认示例一旦被管理员确认就会进入 few-shot 检索,冷启动生成必须在
    确认前就避开它,否则「待确认」只是一段延迟生效的泄漏。文件缺失/读不通/
    根不是映射一律返回 ``[]``(没有示例 ≠ 读失败要中断生成,生成侧按零示例
    继续即可)。
    """
    try:
        if not path.is_file():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("examples") or []
    if not isinstance(entries, list):
        return []
    questions: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("question") or "").strip()
        if question:
            questions.append(question)
    return questions


def check_leakage(question: str, examples: list[str]) -> LeakageHit | None:
    """三趟按优先级扫描示例,返回第一处命中;无命中 → ``None``。

    两边都先过 ledger 归一化,三把判据吃的是同一份串:``normalized`` 直接比
    相等;问题含 CJK 时用字符 bigram Jaccard(中文没有词边界,词重叠尺子
    在中文上恒为 0);否则用英文词重叠。空问题/空示例不产生命中。
    """
    if not isinstance(question, str) or not question.strip():
        return None
    target = normalize_question(question)
    if not target:
        return None
    candidates = [
        (example, normalize_question(example))
        for example in (examples or [])
        if isinstance(example, str) and example.strip()
    ]
    for example, normalized in candidates:
        if normalized == target:
            return LeakageHit(example, "normalized", 1.0)
    if _CJK_RE.search(target):
        for example, normalized in candidates:
            score = bigram_jaccard(target, normalized)
            if score >= BIGRAM_NEAR_DUPLICATE:
                return LeakageHit(example, "bigram", score)
    else:
        for example, normalized in candidates:
            score = word_overlap(target, normalized)
            if score >= WORD_NEAR_DUPLICATE:
                return LeakageHit(example, "word", score)
    return None


__all__ = [
    "BIGRAM_NEAR_DUPLICATE",
    "WORD_NEAR_DUPLICATE",
    "LeakageHit",
    "bigram_jaccard",
    "check_leakage",
    "load_example_questions",
    "word_overlap",
]
