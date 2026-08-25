"""Pure-stdlib hashed n-gram embeddings + cosine similarity (零依赖、零网络).

中英混合文本 → 特征集合(英文词 + CJK 字符 1/2/3-gram)→ hashing-trick
稀疏向量(blake2b 稳定哈希 + 符号折叠)→ L2 归一化 → 余弦相似度。

用途:与确定性词法锚定**混合**(hybrid)的检索重排与近义去重——
确定性分数是硬门(表锚定、词重叠,决定"是否返回"),embedding 只在
门内对候选做排序提升/近义判定,不改变"零确定性命中 = 不返回"的语义
(回归约束:无关问题仍返回空)。blake2b 保证跨进程/跨调用特征哈希稳定。
"""

from __future__ import annotations

import hashlib
import math
import re

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
_EN_RE = re.compile(r"[a-z0-9_]+")
_STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "with", "to", "and", "or",
    "per", "by", "is", "are", "was", "were", "what", "how", "many", "much",
    "does", "do", "did", "from", "where", "which", "that", "this", "has",
    "have", "over", "under", "into", "each", "all", "its", "it", "be",
}

_EMBED_DIM = 256
_RERANK_WEIGHT = 3.0
_DUP_THRESHOLD = 0.8


def text_features(text: str) -> set[str]:
    """特征集:英文词(≥2 字符、去停用词) + CJK 字符 1/2/3-gram。

    中文单字/双字/三字 gram 同时入特征,兼顾"贷款""贷款金额""平均贷款
    金额"三种粒度;英文按词,与既有词法检索同一套朴素预处理。
    """
    low = (text or "").lower()
    feats = {
        w for w in _EN_RE.findall(low)
        if len(w) > 1 and w not in _STOPWORDS
    }
    chars = list(_CJK_RE.findall(low))
    for i in range(len(chars)):
        feats.add(chars[i])
        if i + 1 < len(chars):
            feats.add(chars[i] + chars[i + 1])
        if i + 2 < len(chars):
            feats.add(chars[i] + chars[i + 1] + chars[i + 2])
    return feats


def _hash(feature: str) -> tuple[int, int]:
    """特征 → (桶索引, 符号):blake2b 稳定,跨进程一致(不依赖 hash() 盐)。"""
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    idx = int.from_bytes(digest[:4], "little") % _EMBED_DIM
    sign = 1 if (int.from_bytes(digest[4:], "little") & 1) else -1
    return idx, sign


def embed(text: str) -> list[float]:
    """文本 → L2 归一化的稀疏哈希向量(维度固定 _EMBED_DIM)。"""
    vec = [0.0] * _EMBED_DIM
    for feat in text_features(text):
        idx, sign = _hash(feat)
        vec[idx] += sign
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """两归一化向量的点积 = 余弦相似度(空向量 → 0)。"""
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def similarity(text_a: str, text_b: str) -> float:
    return cosine(embed(text_a), embed(text_b))


def coverage_score(query: str, candidate: str) -> float:
    """检索用相似度:候选对问题特征的覆盖率(0~1)。

    以问题特征数为分母——候选只需"覆盖问题提到的内容"即可,不受候选
    自身长度影响(L2 余弦会惩罚长候选,已实测失真)。与 context_score
    的 relevance_score 同哲学,但特征集更细(CJK 1/2/3-gram + 英文词)。
    """
    q = text_features(query)
    if not q:
        return 0.0
    c = text_features(candidate)
    if not c:
        return 0.0
    return len(q & c) / len(q)


def rerank_score(det_score: float, sim: float,
                 weight: float = _RERANK_WEIGHT) -> float:
    """混合分 = 确定性分 + weight × embedding 相似度(门内排序提升)。

    weight 与确定性分量同量级:约等于一条 term 命中,不会压过表锚定/词重叠
    这些高区分度信号,只让近义但非逐词相同的候选排得更前。
    """
    return det_score + weight * sim


def near_duplicate(text_a: str, text_b: str,
                   threshold: float = _DUP_THRESHOLD) -> bool:
    """近义判定(教训去重用):相似度 ≥ 阈值即视为重复。"""
    return similarity(text_a, text_b) >= threshold
