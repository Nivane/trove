"""投票证据接线 —— 用户点赞产出的教案要能被晋升、被检索。

病根:``rate_lesson`` 建的教案按 ``question`` 存(无 ``pattern``),而
``update_lesson_confidence`` 按 ``pattern`` 匹配、``_lesson_text`` 也不含
``question`` —— 于是 ``upvote`` 增量无处可落,``maybe_promote`` 里「净好评
达到 3」的规则对**唯一带票数的条目**永远不可达,票数加权
(``score = sim × (1 + 0.25 × votes)``)也乘在一个恒为 0 的 ``sim`` 上。
"""

from __future__ import annotations

import pytest

from trove.services.kb.service import KbService

DS = "demo"


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


@pytest.fixture
async def seeded(kb):
    (kb.kb_dir / DS).mkdir(parents=True)
    await kb.ensure_synced(DS)
    return kb


async def test_update_confidence_matches_question_keyed_lesson(seeded):
    """按 question 存下的教案,也要能被置信度更新找到。"""
    await seeded.rate_lesson(
        {"question": "每年发放的贷款笔数", "vote": 1,
         "sql_snippet": "SELECT COUNT(*) FROM loan"},
        DS,
    )

    res = await seeded.update_lesson_confidence(
        DS, "每年发放的贷款笔数", evidence_kind="upvote", threshold=0.99,
    )

    assert res["updated"] is True
    assert res["confidence"] == 0.4


async def test_net_upvotes_at_threshold_promotes(seeded):
    """净好评达到 3 → 自动确认(文档承诺的规则,此前对带票条目不可达)。"""
    for _ in range(3):
        await seeded.rate_lesson({"question": "重复问题", "vote": 1}, DS)

    res = await seeded.update_lesson_confidence(
        DS, "重复问题", evidence_kind="upvote", threshold=0.99,
    )

    assert res["promoted"] is True
    assert res["confirmed"] is True


async def test_net_upvotes_below_threshold_does_not_promote(seeded):
    """净好评不足时不确认 —— 规则的边界要站得住。"""
    for _ in range(2):
        await seeded.rate_lesson({"question": "两票问题", "vote": 1}, DS)

    res = await seeded.update_lesson_confidence(
        DS, "两票问题", evidence_kind="upvote", threshold=0.99,
    )

    assert res["updated"] is True
    assert res["promoted"] is False
    assert res["confirmed"] is False


async def test_confirmed_vote_lesson_is_retrievable_by_its_question(seeded):
    """确认后的投票教案要能被它自己的问题检索到(此前 question 不参与打分)。"""
    await seeded.rate_lesson(
        {"question": "每年发放的贷款笔数", "vote": 1}, DS,
    )
    await seeded.confirm_pending_lessons(DS)

    hits = await seeded.search_lessons("每年发放的贷款笔数", DS, limit=5)

    assert any(
        "每年发放的贷款笔数" in (h.get("question") or "") for h in hits
    ), hits


async def test_pattern_keyed_lessons_unaffected(seeded):
    """回归:pattern 教案既没有 question 字段,行为一个字节都不该变。"""
    await seeded.append_lesson(
        {"pattern": "日期列误当文本比较", "note": "先确认列类型",
         "confirmed": True},
        DS,
    )

    hits = await seeded.search_lessons("日期列误当文本比较", DS, limit=5)

    assert any(
        h.get("pattern") == "日期列误当文本比较" for h in hits
    ), hits
    res = await seeded.update_lesson_confidence(
        DS, "日期列误当文本比较", evidence_kind="upvote", threshold=0.99,
    )
    assert res["updated"] is True
