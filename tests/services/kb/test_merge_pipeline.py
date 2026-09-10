"""重新生成**不吞人的编辑** —— C2 的端到端验收(design §5 第 2–5 条)。

这一组测的是写路径:`kb init --overwrite` 从"整份覆盖"变成"按条目合并"之后,
人的编辑、人的删除、人的新增在重新生成后还在不在。
"""

from __future__ import annotations

import yaml

from trove.services.kb.provenance import dump_asset, read_meta
from trove.services.kb.service import KbService


def _tables(*pairs: tuple[str, str]) -> list[dict]:
    return [{"name": n, "description": d, "columns": [], "metrics": []} for n, d in pairs]


def _read(path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _descriptions(path) -> dict[str, str]:
    return {t["name"]: t.get("description", "") for t in _read(path)["tables"]}


def _names(path) -> list[str]:
    return [t["name"] for t in _read(path)["tables"]]


class TestRegenerationMerges:
    def _init(self, kb, tables, **kw):
        assert kb.init_notes(tables, "demo", **kw), "写盘应当发生"

    def test_human_edit_survives_regeneration(self, tmp_path):
        """验收 #2:人改过的条目在 --overwrite 后仍在,且是人的那个值。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        self._init(kb, _tables(("loan", "gen v1"), ("client", "客户")))

        doc = _read(notes)
        doc["tables"][0]["description"] = "人手写准的说明"
        notes.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")

        self._init(kb, _tables(("loan", "gen v2"), ("client", "客户")),
                   overwrite=True)

        assert _descriptions(notes)["loan"] == "人手写准的说明", (
            "改前必红:整份覆盖会把它变回 gen v2"
        )
        assert _descriptions(notes)["client"] == "客户"

    def test_editor_reformatting_is_not_an_edit(self, tmp_path):
        """人只是把文件存了一遍(重排缩进/键序)→ 生成方的更新照常落地。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        self._init(kb, _tables(("loan", "gen v1")))
        _read_and_rewrite_unordered = dump_asset(
            {"tables": _tables(("loan", "gen v1"))}, "kb_init")
        notes.write_text(
            yaml.safe_dump(yaml.safe_load(_read_and_rewrite_unordered),
                           allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )
        self._init(kb, _tables(("loan", "gen v2")), overwrite=True)
        assert _descriptions(notes)["loan"] == "gen v2"

    def test_human_deletion_is_not_resurrected(self, tmp_path):
        """验收 #3:人删过的条目不被生成方复活。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        self._init(kb, _tables(("loan", "贷款"), ("client", "客户")))

        doc = _read(notes)
        doc["tables"] = [t for t in doc["tables"] if t["name"] != "loan"]
        notes.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")

        self._init(kb, _tables(("loan", "贷款"), ("client", "客户")),
                   overwrite=True)
        assert _names(notes) == ["client"], "改前必红:整份覆盖会把它加回来"

    def test_human_added_entry_survives(self, tmp_path):
        """验收 #5:人手工新增的条目在合并后仍在。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        self._init(kb, _tables(("loan", "贷款")))

        doc = _read(notes)
        doc["tables"].append({"name": "手工加的表", "description": "人写的",
                              "columns": [], "metrics": []})
        notes.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")

        self._init(kb, _tables(("loan", "贷款")), overwrite=True)
        assert "手工加的表" in _names(notes)

    def test_conflict_keeps_ours_and_lands_in_the_report(self, tmp_path):
        """验收 #4:双改 → 保留 ours + 出现在报告里。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        self._init(kb, _tables(("loan", "gen v1")))

        doc = _read(notes)
        doc["tables"][0]["description"] = "人改的"
        notes.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")

        self._init(kb, _tables(("loan", "gen v2")), overwrite=True)

        assert _descriptions(notes)["loan"] == "人改的"
        (report,) = kb.take_merge_reports()
        assert report["file"] == "schema_notes.yml"
        (conflict,) = report["conflicts"]
        assert conflict["path"] == "tables[loan].description"
        assert conflict["ours"] == "人改的" and conflict["theirs"] == "gen v2"

    def test_report_is_consumed_once(self, tmp_path):
        """报告取走即清空,不会把上一轮的结果重复报给下一次调用方。"""
        kb = KbService(tmp_path / "proj")
        self._init(kb, _tables(("loan", "gen v1")))
        self._init(kb, _tables(("loan", "gen v2")), overwrite=True)
        assert kb.take_merge_reports()
        assert kb.take_merge_reports() == []

    def test_third_regeneration_still_keeps_the_edit(self, tmp_path):
        """**基线的语义**:它必须是"生成方的输出",不是"合并后的文件"。

        如果基线写成合并结果,第二轮之后 base 里就带着人的编辑 —— 第三轮
        "人没动过这条"成立,人的编辑会被名正言顺地覆盖掉。所以真正检验这条
        不变量的用例必须跑到第三轮。
        """
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        self._init(kb, _tables(("loan", "gen v1")))

        doc = _read(notes)
        doc["tables"][0]["description"] = "人手写的"
        notes.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")

        self._init(kb, _tables(("loan", "gen v2")), overwrite=True)
        assert _descriptions(notes)["loan"] == "人手写的"

        baseline = yaml.safe_load(
            (kb.kb_dir / "demo" / ".generated" / "schema_notes.yml").read_text("utf-8"))
        assert baseline["tables"][0]["description"] == "gen v2", (
            "基线必须是生成方的输出(gen v2),不能是合并结果(人手写的)"
        )

        self._init(kb, _tables(("loan", "gen v3")), overwrite=True)
        assert _descriptions(notes)["loan"] == "人手写的", (
            "改前必红:基线错写成合并结果时,这一轮开始吞人的编辑"
        )

    def test_merge_keeps_a_backup_of_the_replaced_file(self, tmp_path):
        """覆盖前留一份原文件:合并按构造不丢人的编辑,备份防的是合并自己写错。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        self._init(kb, _tables(("loan", "gen v1")))
        doc = _read(notes)
        doc["tables"][0]["description"] = "人手写的"
        notes.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")

        self._init(kb, _tables(("loan", "gen v2")), overwrite=True)

        generations = sorted((kb.kb_dir / "demo" / ".generated" / "backup").iterdir())
        assert len(generations) == 1, "覆盖写之前要留一份"
        restored = yaml.safe_load(
            (generations[-1] / "schema_notes.yml").read_text(encoding="utf-8"))
        assert restored["tables"][0]["description"] == "人手写的", (
            "备份是**覆盖前**的样子(带人的编辑),不是合并结果"
        )

    def test_backups_are_pruned(self, tmp_path):
        """备份按代数封顶:每次重新生成都留三份,不设限会无限长下去。"""
        from trove.services.kb.baseline import KEEP_BACKUPS, backup_asset

        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        notes.parent.mkdir(parents=True)
        notes.write_text("tables: []\n", encoding="utf-8")
        for i in range(KEEP_BACKUPS + 3):
            backup_asset(notes, stamp=f"2026010{i}T000000000000")
        kept = sorted(p.name for p in
                      (kb.kb_dir / "demo" / ".generated" / "backup").iterdir())
        assert len(kept) == KEEP_BACKUPS
        assert kept == sorted(kept)[-KEEP_BACKUPS:], "留最近的几代"


class TestNoBaselineIsNeverClobbered:
    """写路径的兜底:合并不了就**不写**,绝不退化成覆盖。

    `init_kb` 的预检已经在花钱之前拦过一次,但服务可能被别的调用方直接使用
    (无 LLM 骨架路径、测试、管理端脚本)—— 这一层保证的是"任何调用方都不
    可能靠 `overwrite=True` 覆盖一份没有基线的文件"。
    """

    def test_overwrite_without_baseline_is_a_no_op(self, tmp_path):
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        notes.parent.mkdir(parents=True)
        handwritten = "tables:\n- name: loan\n  description: 人手写的\n"
        notes.write_text(handwritten, encoding="utf-8")

        wrote = kb.init_notes(_tables(("loan", "gen 生成的新说明")), "demo",
                              overwrite=True)

        assert wrote is False
        assert notes.read_text(encoding="utf-8") == handwritten, (
            "改前必红:没有基线时 overwrite 会直接覆盖"
        )

    def test_force_is_the_only_way_through(self, tmp_path):
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        notes.parent.mkdir(parents=True)
        notes.write_text("tables: []\n", encoding="utf-8")

        assert kb.init_notes(_tables(("loan", "gen")), "demo",
                             overwrite=True, force=True) is True
        assert _descriptions(notes) == {"loan": "gen"}

    def test_a_newer_format_asset_is_never_overwritten(self, tmp_path):
        """磁盘上是更新格式的资产 → 覆盖它等于用旧解释改写新资产,拒绝。"""
        from trove.services.kb.provenance import stamp

        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        notes.parent.mkdir(parents=True)
        future = stamp({"tables": _tables(("loan", "来自未来的版本"))}, "kb_init")
        future["_meta"]["format"] = 999
        notes.write_text(yaml.safe_dump(future, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")

        assert kb.init_notes(_tables(("loan", "gen")), "demo",
                             overwrite=True, force=True) is False
        assert _descriptions(notes) == {"loan": "来自未来的版本"}
        assert kb.refused_assets


class TestBaselineIsNotAnAsset:
    """`.generated/` 是点目录,读路径按 `*.yml` 取文件 —— 基线不能被当成资产。"""

    async def test_baseline_files_are_not_mirrored_as_items(self, tmp_path):
        kb = KbService(tmp_path / "proj")
        assert kb.init_notes(_tables(("loan", "gen v1")), "demo")
        assert (kb.kb_dir / "demo" / ".generated" / "schema_notes.yml").exists()

        await kb.force_sync("demo")
        rows = await kb._rows(
            "SELECT source_file FROM kb_items WHERE datasource = 'demo'", ())
        assert rows, "资产本身要进镜像"
        assert all(r["source_file"] == "schema_notes.yml" for r in rows), (
            f"基线被当成资产读进来了: {rows}"
        )

    def test_baseline_has_no_meta_block(self, tmp_path):
        """基线不盖章:来源块描述的是盘上那份文件,不是它的祖先副本。"""
        kb = KbService(tmp_path / "proj")
        kb.init_notes(_tables(("loan", "gen v1")), "demo")
        baseline = _read(kb.kb_dir / "demo" / ".generated" / "schema_notes.yml")
        assert "_meta" not in baseline
        assert read_meta(baseline).format == 0

    def test_semantic_provider_does_not_read_the_baseline(self, tmp_path):
        """`SemanticLayerProvider._reload` 用 `glob("*.yml")` —— 显式核对。

        基线若是被读成"补充源",一份空模型就可能把真源整个换掉。
        """
        from trove.services.semantic_layer.provider import SemanticLayerProvider

        ds_dir = tmp_path / "semantic" / "demo"
        ds_dir.mkdir(parents=True)
        (ds_dir / "semantics.yml").write_text(
            yaml.safe_dump(
                {"semantic_model": [{"name": "demo", "datasets": [], "metrics": []}]},
                allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        (ds_dir / ".generated").mkdir()
        (ds_dir / ".generated" / "semantics.yml").write_text(
            yaml.safe_dump({"semantic_model": [{"name": "BASELINE", "datasets": [],
                                                "metrics": []}]},
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8")

        model = SemanticLayerProvider(ds_dir, "demo").model()
        assert model is not None and model.name == "demo", "基线不该被读进来"
