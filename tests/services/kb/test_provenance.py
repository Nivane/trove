"""KB 资产 provenance 与格式版本门(Phase C1)。

改前必红的性质(见 design §5 第 1/6/10 条):
- 资产能说出自己是哪一版生成的(digest/format/generator);
- 比自己新的格式**拒绝**读,不被按旧解释静默解释;
- 无 `_meta` 的存量文件**不拒绝**(降级为 format 0),否则升级即全部停摆;
- `_meta` 不进正文摘要、不改变解析结果。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import yaml

from trove.services.kb.provenance import (
    FORMAT_LEGACY,
    FORMAT_VERSION,
    META_KEY,
    body_digest,
    body_edited,
    check_format,
    dump_asset,
    needs_migration,
    read_meta,
    stamp,
)

_DOC = {
    "tables": [
        {
            "name": "loan",
            "description": "贷款记录",
            "columns": [
                {"name": "amount", "description": "金额", "enums": []},
            ],
            "metrics": [{"name": "total", "definition": "SUM(amount)"}],
        },
    ],
}


class TestBodyDigest:
    def test_digest_ignores_key_order(self):
        """同一份内容换个键序 → 同一摘要(不然每次 dump 都判成"人改过")。"""
        a = {"tables": [{"name": "loan", "description": "x"}]}
        b = {"tables": [{"description": "x", "name": "loan"}]}
        assert body_digest(a) == body_digest(b)

    def test_digest_changes_with_content(self):
        a = {"tables": [{"name": "loan", "description": "x"}]}
        b = {"tables": [{"name": "loan", "description": "y"}]}
        assert body_digest(a) != body_digest(b)

    def test_meta_is_not_part_of_the_body(self):
        """`_meta` 是**关于**正文的,不是正文 —— 改它不能改正文摘要。

        这是"文件是否被人改过"能成立的前提:每次 Trove 自己写 `_meta`
        (时间戳每次都新)都不该把自己判成"被人改过"。
        """
        doc = dict(_DOC)
        stamped = stamp(doc, "kb_init")
        assert body_digest(stamped) == body_digest(doc)

        other = dict(stamped)
        other[META_KEY] = dict(stamped[META_KEY], generated_at="2020-01-01T00:00:00+00:00")
        assert body_digest(other) == body_digest(doc)

    def test_yaml_types_round_trip(self):
        """裸日期/时间字面量经 YAML 往返后摘要不变(safe_dump → safe_load)。

        这是一个**真实**的陷阱:safe_load 会把裸 `2026-09-03` 还原成
        ``date`` 对象,而写入时拿到的可能是字符串。摘要若按 Python 类型
        算,人没改过的文件也会判成"改过",合并就会把生成方的更新全判成
        冲突。统一 `default=str` 归一。
        """
        doc = {
            "lessons": [
                {"pattern": "p", "created_at": datetime(2026, 9, 3, 8, 39, 38, tzinfo=timezone.utc)},
                {"pattern": "q", "day": date(2026, 9, 3)},
            ],
        }
        loaded = yaml.safe_load(dump_asset(doc, "kb_learn"))
        assert body_edited(loaded) is False


class TestStampAndRead:
    def test_stamp_adds_a_meta_block(self):
        stamped = stamp(_DOC, "kb_init")
        meta = stamped[META_KEY]
        assert meta["generator"] == "kb_init"
        assert meta["format"] == FORMAT_VERSION
        assert meta["trove"]
        assert meta["generated_at"]
        assert meta["digest"]

    def test_stamp_does_not_mutate_the_input(self):
        doc = dict(_DOC)
        stamp(doc, "kb_init")
        assert META_KEY not in doc

    def test_round_trip_is_not_edited(self):
        """Trove 写完立刻读回 → 没被人改过(合并的快路径)。"""
        loaded = yaml.safe_load(dump_asset(_DOC, "kb_init"))
        assert body_edited(loaded) is False

    def test_body_edit_is_detected(self):
        loaded = yaml.safe_load(dump_asset(_DOC, "kb_init"))
        loaded["tables"][0]["description"] = "人手改过的说明"
        assert body_edited(loaded) is True

    def test_absent_meta_reads_as_legacy(self):
        """存量库(今天所有 KB)都没有 `_meta` —— 一律可读,标 format 0。"""
        meta = read_meta(_DOC)
        assert meta.format == FORMAT_LEGACY
        assert meta.digest == ""
        assert body_edited(_DOC) is None  # 无从判断,不是"没改过"

    def test_malformed_meta_reads_as_legacy(self):
        """`_meta` 损坏 → 按最保守的一侧读(不拒绝,也不假装知道版本)。"""
        for bad in ("kb_init", 42, [], {"format": "next"}, {"format": None}):
            meta = read_meta({"tables": [], META_KEY: bad})
            assert meta.format == FORMAT_LEGACY, bad

    def test_numeric_string_format_is_read(self):
        """`format: "2"` 是**可解析**的版本号,不能当损坏处理 —— 那会把
        "更新的格式"误判成"老文件",正是这道门要拦的情况。"""
        assert read_meta({META_KEY: {"format": "2"}}).format == 2


class TestFormatGate:
    def test_newer_format_is_refused(self):
        msg = check_format(read_meta({META_KEY: {"format": FORMAT_VERSION + 1}}))
        assert msg is not None
        assert str(FORMAT_VERSION) in msg
        assert str(FORMAT_VERSION + 1) in msg

    def test_current_format_is_accepted(self):
        assert check_format(read_meta({META_KEY: {"format": FORMAT_VERSION}})) is None

    def test_legacy_and_older_formats_are_accepted(self):
        """老文件照常读(不拒绝):能读的继续读,迁移是人显式触发的事。"""
        assert check_format(read_meta(_DOC)) is None
        assert check_format(read_meta({META_KEY: {"format": FORMAT_LEGACY}})) is None

    def test_migration_is_reported_but_not_refused(self):
        assert needs_migration(read_meta(_DOC)) is True
        assert needs_migration(read_meta({META_KEY: {"format": FORMAT_VERSION}})) is False
        # 更新格式不叫"需要迁移"(那是升级 Trove 的事,check_format 会拒绝)
        assert needs_migration(read_meta({META_KEY: {"format": FORMAT_VERSION + 1}})) is False

    def test_meta_does_not_change_parsing(self):
        """带 `_meta` 与不带 `_meta` 的产物,解析出的内容逐字相同。

        这条防的是"来源块污染检索":解析端按 section 取键,多一个顶层
        键必须是 no-op。
        """
        import tempfile
        from pathlib import Path

        from trove.services.kb.service import _parse_file

        # 解析器按**文件名**分派,所以两份都得叫 schema_notes.yml
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.mkdir()
            (plain / "schema_notes.yml").write_text(
                yaml.safe_dump(_DOC, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            stamped = Path(tmp) / "stamped"
            stamped.mkdir()
            (stamped / "schema_notes.yml").write_text(
                dump_asset(_DOC, "kb_init"), encoding="utf-8",
            )
            assert (
                _parse_file(stamped / "schema_notes.yml")
                == _parse_file(plain / "schema_notes.yml")
            )
