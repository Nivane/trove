"""``evalset_rev`` —— 验收集**评分内容**的修订指纹(与 ``kb_rev`` 无关)。

只哈希 ``status == "confirmed"`` 的条目:生成了一批 pending、拒绝了一条、
退役了一条,rev 都不该动 —— rev 的职责是回答「**被评分的那套题**变了没有」。
条目按 ``qid`` 排序后序列化为规范 JSON(``sort_keys`` + 紧凑分隔符 +
``ensure_ascii=False``),所以同一份内容在任何进程、任何 locale 下都是同一
个 16 位十六进制串。

四个字段就是评分输入的全集:``qid``(判定行按它回写)、``question``(问给
模型的原文)、``reference_sql``(对错尺子)、``match``(比对方式)。tag、
note、时间戳、来源元数据都不参与 —— 改备注不是改题。
"""

from __future__ import annotations

import hashlib
import json

from trove.services.evalset.models import EvalsetDoc


def evalset_rev(doc: EvalsetDoc) -> str:
    """confirmed 条目的规范 JSON 的 sha256 前 16 位。

    没有 confirmed 条目时为 ``sha256("[]")[:16]`` —— 一个稳定的空集指纹,
    不是空串(空串会让「未初始化」与「有资产但尚无确认题」撞成一种状态)。
    """
    confirmed = sorted(
        (it for it in doc.items if it.status == "confirmed"),
        key=lambda it: it.qid,
    )
    canonical = [
        {
            "qid": it.qid,
            "question": it.question,
            "reference_sql": it.reference_sql,
            "match": it.match,
        }
        for it in confirmed
    ]
    blob = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


__all__ = ["evalset_rev"]
