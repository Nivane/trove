"""三方合并 ``merge3`` 的逐条规则(C2,design §3.3 的表)。

纯函数测试:不读盘、不写盘。每一条都对应设计表里的一行 —— 本设计的核心
目的是"**人的编辑在重新生成后仍然在**",所以这一组用例是它的全部理由。
"""

from __future__ import annotations

from trove.services.kb.merge import (
    ADDED_VS_ADDED,
    BOTH_MODIFIED,
    DELETED_VS_MODIFIED,
    merge3,
)


def _notes(*tables: dict) -> dict:
    return {"tables": list(tables)}


def _table(name: str, description: str, **extra) -> dict:
    return {"name": name, "description": description, **extra}


class TestEntryRules:
    """设计表逐行验证(``ours`` = 盘上的文件,``theirs`` = 这次生成的)。"""

    def test_generator_update_lands_when_human_did_not_touch_it(self):
        """base = ours, theirs ≠ base → theirs(人没动这条)。"""
        base = _notes(_table("loan", "旧说明"))
        r = merge3(base, base, _notes(_table("loan", "生成方的新说明")), "schema_notes.yml")
        assert r.doc["tables"][0]["description"] == "生成方的新说明"
        assert r.clean and r.updated == 1

    def test_human_edit_survives(self):
        """base = theirs, ours ≠ base → ours。**本设计的核心目的。**"""
        base = _notes(_table("loan", "生成方写的"), _table("client", "客户"))
        ours = _notes(_table("loan", "人手写的准确说明"), _table("client", "客户"))
        r = merge3(base, ours, base, "schema_notes.yml")
        assert r.doc["tables"][0]["description"] == "人手写的准确说明"
        assert r.clean, "生成方没动这条 → 不是冲突"
        assert r.kept == 1

    def test_both_modified_keeps_ours_and_reports(self):
        """双改 → 保留 ours + 进报告(不猜)。"""
        r = merge3(
            _notes(_table("loan", "原有")),
            _notes(_table("loan", "人改的")),
            _notes(_table("loan", "生成方改的")),
            "schema_notes.yml",
        )
        assert r.doc["tables"][0]["description"] == "人改的"
        assert not r.clean
        (c,) = r.conflicts
        assert c.kind == BOTH_MODIFIED
        assert c.path == "tables[loan].description"
        assert c.ours == "人改的" and c.theirs == "生成方改的"

    def test_both_modified_to_the_same_value_is_not_a_conflict(self):
        """双方改成了同一个值 → 收敛,不算冲突。"""
        r = merge3(
            _notes(_table("loan", "原有")),
            _notes(_table("loan", "一致的新值")),
            _notes(_table("loan", "一致的新值")),
            "schema_notes.yml",
        )
        assert r.clean
        assert r.doc["tables"][0]["description"] == "一致的新值"

    def test_human_deletion_is_respected(self):
        """有 / 删 / = base → 删(尊重人的删除,生成方不复活它)。"""
        base = _notes(_table("loan", "贷款"), _table("client", "客户"))
        r = merge3(base, _notes(_table("client", "客户")), base, "schema_notes.yml")
        assert [t["name"] for t in r.doc["tables"]] == ["client"]
        assert r.clean and r.removed == 1

    def test_generator_deletion_is_respected(self):
        """有 / 在 / 生成方删 → 删(如指标被从模型里移除)。"""
        base = _notes(_table("loan", "贷款"), _table("client", "客户"))
        r = merge3(
            base, base, _notes(_table("client", "客户")), "schema_notes.yml",
        )
        assert [t["name"] for t in r.doc["tables"]] == ["client"]

    def test_delete_vs_modify_respects_the_deletion_and_reports(self):
        """人删了但生成方也改了 → 说不清:按"冲突保留 ours"的同一把尺子,
        ours 在这里就是"没有这条" → **保持删除**,并把生成方的复议放进报告。"""
        base = _notes(_table("loan", "原有"), _table("client", "客户"))
        r = merge3(
            base,
            _notes(_table("client", "客户")),
            _notes(_table("loan", "生成方改过的"), _table("client", "客户")),
            "schema_notes.yml",
        )
        assert [t["name"] for t in r.doc["tables"]] == ["client"], (
            "人删掉的条目不该被生成方复活"
        )
        assert not r.clean
        (c,) = r.conflicts
        assert c.kind == DELETED_VS_MODIFIED
        assert c.ours is None, "报告里缺失显式写成 None"
        assert c.theirs["name"] == "loan", "报告要说清生成方想加回来的是什么"

    def test_human_modification_vs_generator_deletion_keeps_ours(self):
        """人改过 + 生成方删了 → 保留人的那份 + 报告。"""
        base = _notes(_table("loan", "原有"), _table("client", "客户"))
        r = merge3(
            base,
            _notes(_table("loan", "人改的"), _table("client", "客户")),
            _notes(_table("client", "客户")),
            "schema_notes.yml",
        )
        assert [t["name"] for t in r.doc["tables"]] == ["loan", "client"]
        assert r.doc["tables"][0]["description"] == "人改的"
        assert r.conflicts[0].kind == DELETED_VS_MODIFIED

    def test_human_added_entry_survives(self):
        """无 / 有 / 无 → 保留(人手工加的条目)。"""
        base = _notes(_table("loan", "贷款"))
        ours = _notes(_table("loan", "贷款"), _table("手加的表", "人写的"))
        r = merge3(base, ours, base, "schema_notes.yml")
        assert [t["name"] for t in r.doc["tables"]] == ["loan", "手加的表"]
        assert r.clean and r.kept == 1

    def test_generator_added_entry_is_appended(self):
        """无 / 无 / 有 → 新增 theirs。"""
        base = _notes(_table("loan", "贷款"))
        r = merge3(
            base, base, _notes(_table("loan", "贷款"), _table("新表", "生成方加的")),
            "schema_notes.yml",
        )
        assert [t["name"] for t in r.doc["tables"]] == ["loan", "新表"]
        assert r.clean and r.added == 1

    def test_both_added_different_entries_is_reported(self):
        """双方各加了同名的条目且内容不同 → 祖先不明,不猜。"""
        base = _notes(_table("loan", "贷款"))
        r = merge3(
            base,
            _notes(_table("loan", "贷款"), _table("新表", "人加的")),
            _notes(_table("loan", "贷款"), _table("新表", "生成方加的")),
            "schema_notes.yml",
        )
        assert r.doc["tables"][1]["description"] == "人加的"
        assert r.conflicts[0].kind == ADDED_VS_ADDED


class TestIdentityAndStructure:
    def test_order_follows_ours(self):
        """人重排过列表 → 不该被生成方改回去。"""
        r = merge3(
            _notes(_table("a", "1"), _table("b", "2")),
            _notes(_table("b", "2"), _table("a", "1")),
            _notes(_table("a", "1"), _table("b", "2")),
            "schema_notes.yml",
        )
        assert [t["name"] for t in r.doc["tables"]] == ["b", "a"]

    def test_meta_block_never_participates(self):
        """`_meta` 每次都新(时间戳),留着它会让每份文件都先炸一条冲突。"""
        base = {"tables": [_table("loan", "x")], "_meta": {"digest": "sha256:aaa"}}
        theirs = {"tables": [_table("loan", "x")], "_meta": {"digest": "sha256:bbb"}}
        r = merge3(base, base, theirs, "schema_notes.yml")
        assert r.clean, f"_meta 不该产生冲突: {r.conflicts}"
        assert "_meta" not in r.doc, "合并结果由调用方重新盖章"

    def test_unknown_file_falls_back_to_whole_document(self):
        """不在白名单里的文件:整份按通用规则,不猜结构。"""
        base = {"foo": [1, 2, 3]}
        assert merge3(base, base, {"foo": [1, 2]}, "custom.yml").doc == {"foo": [1, 2]}
        assert merge3(base, {"foo": [9]}, {"foo": [1, 2]}, "custom.yml").doc == {"foo": [9]}

    def test_rules_file_is_never_regenerated(self):
        """rules.yml 是纯人工文件(没有任何写入器)→ 原样保留。"""
        ours = {"rules": [{"rule": "no_select_star"}]}
        r = merge3({}, ours, {"rules": []}, "rules.yml")
        assert r.doc == ours
        assert r.clean

    def test_unkeyable_list_is_replaced_whole(self):
        """`expression.dialects` 之类没有身份键的列表 → 整块替换,不硬凑条目。"""
        base = {"metrics": [{"name": "m", "expression": {
            "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(a)"}]}}]}
        theirs = {"metrics": [{"name": "m", "expression": {
            "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(b)"}]}}]}
        r = merge3(base, base, theirs, "semantics.yml")
        assert r.doc["metrics"][0]["expression"]["dialects"][0]["expression"] == "SUM(b)"
        assert r.clean

    def test_nested_entries_merge_at_their_own_granularity(self):
        """`datasets[].fields[]` 有自己的身份键 → 在字段粒度上合并。"""
        base = {"semantic_model": [{"name": "demo", "datasets": [{
            "name": "loan", "fields": [
                {"name": "amount", "description": "生成方的"},
                {"name": "status", "description": "状态"},
            ]}]}]}
        ours = {"semantic_model": [{"name": "demo", "datasets": [{
            "name": "loan", "fields": [
                {"name": "amount", "description": "人写准的"},
                {"name": "status", "description": "状态"},
            ]}]}]}
        theirs = {"semantic_model": [{"name": "demo", "datasets": [{
            "name": "loan", "fields": [
                {"name": "amount", "description": "生成方新写的"},
                {"name": "status", "description": "状态"},
                {"name": "date", "description": "日期"},
            ]}]}]}
        r = merge3(base, ours, theirs, "semantics.yml")
        fields = r.doc["semantic_model"][0]["datasets"][0]["fields"]
        assert [f["name"] for f in fields] == ["amount", "status", "date"]
        assert fields[0]["description"] == "人写准的", "字段粒度:人的编辑存活"
        assert fields[1]["description"] == "状态", "双方都没动的字段原样留着"
        # amount 双方都改过 → 在**字段**粒度上报冲突,不是整个 dataset 一条
        (c,) = r.conflicts
        assert c.path.endswith("datasets[loan].fields[amount].description")

    def test_top_level_scalars_merge_by_the_same_rule(self):
        """OSSIE ``version`` 这类顶层标量走同一条规则。"""
        r = merge3(
            {"version": "0.1.2", "semantic_model": []},
            {"version": "0.1.2", "semantic_model": []},
            {"version": "0.1.3", "semantic_model": []},
            "semantics.yml",
        )
        assert r.doc["version"] == "0.1.3"

    def test_duplicate_identity_keys_fall_back_to_whole_block(self):
        """身份键重复 → 定不了序,整块处理比"猜哪条是哪条"安全。"""
        base = {"tables": [{"name": "loan", "description": "a"},
                           {"name": "loan", "description": "b"}]}
        r = merge3(base, base, {"tables": []}, "schema_notes.yml")
        assert r.doc["tables"] == base["tables"]


class TestRealisticShapes:
    def test_examples_identified_by_question(self):
        base = {"examples": [
            {"question": "有多少贷款?", "sql": "SELECT count(*) FROM loan"},
        ]}
        ours = {"examples": [
            {"question": "有多少贷款?", "sql": "SELECT count(*) FROM loan WHERE status=1"},
        ]}
        r = merge3(base, ours, base, "examples.yml")
        assert r.doc["examples"][0]["sql"].endswith("status=1")
        assert r.clean

    def test_lessons_identified_by_pattern(self):
        base = {"lessons": [{"pattern": "地区平均", "note": "旧"}]}
        r = merge3(
            base, base, {"lessons": [{"pattern": "地区平均", "note": "生成方新写的"}]},
            "lessons.yml",
        )
        assert r.doc["lessons"][0]["note"] == "生成方新写的"

    def test_merging_with_empty_base_keeps_everything_ours(self):
        """空 base(无基线)会把人的东西全留下、并报一堆冲突 —— 这**不是**
        可用的合并策略,只是"不猜"的必然结果。调用方必须拦住无基线的情况。"""
        ours = _notes(_table("loan", "人写的"))
        theirs = _notes(_table("loan", "生成方写的"))
        r = merge3({}, ours, theirs, "schema_notes.yml")
        assert r.doc["tables"][0]["description"] == "人写的"
        assert len(r.conflicts) == 1
