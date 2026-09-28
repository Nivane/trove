"""合流层测试 —— 重点是 I3「空报告 ≠ 无漂移」。

这套测试的**主要目的不是覆盖映射的正确性,而是钉住一条曾经不存在的区分**:
在 2026-09-28 之前,``detect_drift`` 与 ``provider.drift()`` 在「没查成」时
返回的都是全空字典,与「确实无漂移」逐字节相同。任何下游(CI、巡检、API)
只要读那个字典,就一定会把前者读成后者。

因此 ``TestI3SkippedIsNotClean`` 是这组测试的中心,其余是它的支撑。
"""

from __future__ import annotations

import pytest

from trove.services.drift.adapters import (
    collect,
    from_schema_drift,
    from_semantic_drift,
)
from trove.services.drift.models import (
    L1,
    L2,
    RUN_OK,
    RUN_SKIPPED,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    normalize_subject,
)


# ── 两个检测器的真实返回形状(取自 schema_drift.py / provider.py)────────

def _l1(**over) -> dict:
    """``memory.schema_drift.detect_drift`` 的返回形状。"""
    base = {
        "datasource": "demo",
        "new_tables": [],
        "gone_tables": [],
        "column_changes": {},
        "status": "ok",
        "skip_reason": None,
    }
    base.update(over)
    return base


def _l2(**over) -> dict:
    """``SemanticLayerProvider._compute_drift`` 的返回形状。

    注意 ``missing_keys`` 是 **dict**(键为数据集名),不是 list。
    """
    base = {
        "stale": False,
        "gone_tables": [],
        "missing_fields": {},
        "missing_keys": {},
        "relationship_breaks": [],
        "status": "ok",
        "skip_reason": None,
    }
    base.update(over)
    return base


# ══ I3:未检测不得冒充干净 ═══════════════════════════════════════════

class TestI3SkippedIsNotClean:
    """I3:空报告 ≠ 无漂移。"""

    @pytest.mark.parametrize("reason", [
        "catalog_unreachable", "kb_missing", "kb_empty", "kb_unreadable",
    ])
    def test_l1_skipped_reasons_survive_adaptation(self, reason):
        rep = from_schema_drift(
            _l1(status="skipped", skip_reason=reason), "demo")
        assert rep.status == RUN_SKIPPED
        assert rep.skip_reason == reason
        assert rep.items == []
        assert rep.ok is False

    @pytest.mark.parametrize("reason", ["no_semantic_model", "no_catalog"])
    def test_l2_skipped_reasons_survive_adaptation(self, reason):
        rep = from_semantic_drift(
            _l2(status="skipped", skip_reason=reason), "demo")
        assert rep.status == RUN_SKIPPED
        assert rep.skip_reason == reason
        assert rep.ok is False

    def test_empty_and_skipped_are_distinguishable(self):
        """核心断言:两者 items 都为空,但 status 必须不同。"""
        clean = from_schema_drift(_l1(), "demo")
        unchecked = from_schema_drift(
            _l1(status="skipped", skip_reason="catalog_unreachable"), "demo")

        assert clean.items == [] and unchecked.items == []
        assert clean.status != unchecked.status
        assert clean.ok is True and unchecked.ok is False

    def test_collect_degrades_whole_report_when_one_side_skipped(self):
        """一路 skipped → 整体 skipped。

        半真报告(标 ok 但只查了一半)比明确的"未完成"更危险:调用方
        看到 ok 会以为两面都干净。
        """
        merged = collect(
            _l1(new_tables=["brand_new"]),           # L1 查成了,而且有发现
            _l2(status="skipped", skip_reason="no_catalog"),
            "demo",
        )
        assert merged.status == RUN_SKIPPED
        # L1 的发现被**有意丢弃** —— 见 collect() 的 docstring
        assert merged.items == []
        assert "no_catalog" in (merged.skip_reason or "")

    def test_collect_merges_reasons_from_both_sides(self):
        merged = collect(
            _l1(status="skipped", skip_reason="kb_missing"),
            _l2(status="skipped", skip_reason="no_catalog"),
            "demo",
        )
        assert merged.status == RUN_SKIPPED
        assert "kb_missing" in merged.skip_reason
        assert "no_catalog" in merged.skip_reason

    def test_legacy_report_without_status_is_treated_as_checked(self):
        """兼容旧形状:无 status 键 → 按已检测处理,不误报 skipped。"""
        legacy = {"datasource": "demo", "new_tables": ["t"],
                  "gone_tables": [], "column_changes": {}}
        rep = from_schema_drift(legacy, "demo")
        assert rep.status == RUN_OK
        assert len(rep.items) == 1


# ══ L1 映射 ════════════════════════════════════════════════════════

class TestL1Adapter:
    def test_clean_report_yields_no_items(self):
        rep = from_schema_drift(_l1(), "demo")
        assert rep.status == RUN_OK and rep.items == []
        assert rep.datasource == "demo"

    def test_new_table_is_info(self):
        rep = from_schema_drift(_l1(new_tables=["orders_v2"]), "demo")
        (item,) = rep.items
        assert item.level == L1
        assert item.kind == "table_added"
        assert item.subject == "orders_v2"
        assert item.severity == SEVERITY_INFO

    def test_gone_table_is_warning_not_critical(self):
        """L1 违反的是 KB 描述(喂提示词),不是语义契约。

        后果是模型可能写到不存在的表上 → 查询失败,但**不会静默答错**。
        与 L2 的 critical 是客观区分,不是主观排序。
        """
        rep = from_schema_drift(_l1(gone_tables=["legacy"]), "demo")
        (item,) = rep.items
        assert item.kind == "table_removed"
        assert item.severity == SEVERITY_WARNING

    def test_column_changes_subject_is_table_dot_column(self):
        rep = from_schema_drift(_l1(column_changes={
            "orders": {"added": ["coupon_id"], "removed": ["old_col"]},
        }), "demo")
        got = {(i.kind, i.subject, i.severity) for i in rep.items}
        assert got == {
            ("column_added", "orders.coupon_id", SEVERITY_INFO),
            ("column_removed", "orders.old_col", SEVERITY_WARNING),
        }

    def test_datasource_falls_back_to_report_field(self):
        rep = from_schema_drift(_l1(), "")   # 调用方没传
        assert rep.datasource == "demo"

    def test_blank_table_name_is_dropped_not_crashing(self):
        rep = from_schema_drift(_l1(new_tables=["", "  "]), "demo")
        assert rep.items == []


# ══ L2 映射 ════════════════════════════════════════════════════════

class TestL2Adapter:
    def test_gone_dataset_is_critical(self):
        rep = from_semantic_drift(_l2(gone_tables=["sales"]), "demo")
        (item,) = rep.items
        assert item.level == L2
        assert item.kind == "dataset_table_missing"
        assert item.subject == "sales"
        assert item.severity == SEVERITY_CRITICAL

    def test_missing_field_subject_is_dataset_dot_field(self):
        rep = from_semantic_drift(
            _l2(missing_fields={"sales": ["revenue", "cost"]}), "demo")
        got = {(i.kind, i.subject) for i in rep.items}
        assert got == {
            ("field_column_missing", "sales.revenue"),
            ("field_column_missing", "sales.cost"),
        }
        assert all(i.severity == SEVERITY_CRITICAL for i in rep.items)

    def test_missing_keys_dict_is_iterated_not_treated_as_list(self):
        """回归:``missing_keys`` 是 dict。

        若按 list 迭代会产出 0 条 —— 又一次"静默变空",正是本模块要消灭
        的东西。用一个非空 dict 钉住它。
        """
        rep = from_semantic_drift(
            _l2(missing_keys={"orders": ["order_id"]}), "demo")
        (item,) = rep.items
        assert item.kind == "key_column_missing"
        assert item.subject == "orders.order_id"
        # 键列缺失削弱去重/计数口径,但不让编译产物引用不存在的对象
        assert item.severity == SEVERITY_WARNING

    def test_relationship_break_keeps_detector_readable_detail(self):
        rep = from_semantic_drift(_l2(relationship_breaks=[
            {"name": "orders_to_users", "detail": "table users gone; orders.user_id"},
        ]), "demo")
        (item,) = rep.items
        assert item.kind == "relationship_broken"
        assert item.subject == "orders_to_users"
        assert item.severity == SEVERITY_CRITICAL
        # 检测器拼好的可读串原样保留 —— 排障时先看的就是它
        assert item.detail["problems"] == "table users gone; orders.user_id"

    def test_all_four_signals_in_one_report(self):
        rep = from_semantic_drift(_l2(
            gone_tables=["sales"],
            missing_fields={"orders": ["amount"]},
            missing_keys={"orders": ["order_id"]},
            relationship_breaks=[{"name": "r1", "detail": "x"}],
        ), "demo")
        assert {i.kind for i in rep.items} == {
            "dataset_table_missing", "field_column_missing",
            "key_column_missing", "relationship_broken",
        }
        assert len(rep.items) == 4

    def test_empty_containers_do_not_crash(self):
        rep = from_semantic_drift(
            _l2(missing_fields={"sales": []}, missing_keys={},
                relationship_breaks=[]), "demo")
        assert rep.items == []


# ══ collect ════════════════════════════════════════════════════════

class TestCollect:
    def test_both_ok_union_of_items(self):
        merged = collect(
            _l1(gone_tables=["legacy"]),
            _l2(gone_tables=["sales"]),
            "demo",
        )
        assert merged.status == RUN_OK
        assert {(i.level, i.subject) for i in merged.items} == {
            (L1, "legacy"), (L2, "sales"),
        }

    def test_missing_semantic_report_yields_l1_only(self):
        """没配语义层时 semantic_report 传 None —— 不算 skipped,L1 独立成立。"""
        merged = collect(_l1(new_tables=["t"]), None, "demo")
        assert merged.status == RUN_OK
        assert len(merged.items) == 1

    def test_no_detector_ran_is_skipped(self):
        merged = collect(None, None, "demo")
        assert merged.status == RUN_SKIPPED
        assert merged.skip_reason == "no_detector_ran"

    def test_both_clean_is_ok(self):
        merged = collect(_l1(), _l2(), "demo")
        assert merged.status == RUN_OK and merged.items == []

    def test_by_level_and_blocking_helpers(self):
        merged = collect(
            _l1(new_tables=["t"]),          # info —— 不阻断
            _l2(gone_tables=["sales"]),     # critical —— 阻断
            "demo",
        )
        assert len(merged.by_level(L1)) == 1
        assert len(merged.by_level(L2)) == 1
        assert [i.subject for i in merged.blocking()] == ["sales"]


# ══ subject 规范化 ═════════════════════════════════════════════════

class TestNormalizeSubject:
    @pytest.mark.parametrize("raw,expected", [
        ("public.orders.id", "orders.id"),      # 去 schema 前缀
        ("Orders.ID", "orders.id"),             # 小写
        ("`db`.`orders`.`id`", "orders.id"),    # 去反引号
        ('"orders"."id"', "orders.id"),
        ("[orders].[id]", "orders.id"),
        ("  orders .  id  ", "orders.id"),      # 压空白
        ("orders", "orders"),
        ("", ""),
        ("   ", ""),
    ])
    def test_normalization(self, raw, expected):
        assert normalize_subject(raw) == expected

    def test_schema_qualified_and_bare_agree(self):
        """物理 schema 前缀在语义层是可选的 —— 不归一会让同一处漂移在两条
        检测路径下产生两个 subject,门禁 coverage 判定就永远对不上。"""
        assert normalize_subject("public.orders.id") == \
            normalize_subject("orders.id")
