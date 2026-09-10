"""读侧版本门的接线测试(C1)。

`test_provenance.py` 测的是纯函数;这里测**它有没有真的接在路径上** ——
一个没人调用的 gate 与没有 gate 完全等价,而这正是改造前 `format` 字段的
处境(声明了版本,从没人比过)。

改前必红的性质(design §5 第 2/3/4/5 条):
- 比自己新的资产**不进镜像**(旧镜像保留),并出现在 refused 报告里;
- 比自己新的 semantics.yml **不进语义层**(保留 last-known-good);
- 每条 Trove 写路径写完立刻读回,都判定为"没被人改过"。
"""

from __future__ import annotations

import yaml

from trove.services.kb.provenance import (
    FORMAT_VERSION,
    META_KEY,
    body_edited,
    dump_asset,
    stamp,
)
from trove.services.kb.service import KbService

_SCHEMA_NOTES = {
    "tables": [
        {
            "name": "loan",
            "description": "贷款记录",
            "columns": [{"name": "amount", "description": "金额", "enums": []}],
            "metrics": [],
        },
    ],
}


def _write(path, doc, generator="kb_init"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_asset(doc, generator), encoding="utf-8")


def _write_future(path, doc):
    """把正文写成 format 999(比本代码新)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = dict(stamp(doc, "trove-from-the-future")[META_KEY], format=FORMAT_VERSION + 998)
    path.write_text(
        yaml.safe_dump({**doc, META_KEY: meta}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


class TestMirrorGate:
    async def test_newer_asset_is_not_mirrored(self, tmp_path):
        """比自己新的资产:镜像不采纳 —— 且保留旧镜像,不是清空。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        _write(notes, _SCHEMA_NOTES)  # 先有一份能读的
        await kb.force_sync("demo")
        before = await kb.list_items()

        _write_future(notes, {
            "tables": [{**_SCHEMA_NOTES["tables"][0], "description": "来自未来"}],
        })
        kb.refused_assets.clear()
        await kb.force_sync("demo")

        # 镜像仍是上一次读懂的样子(旧的描述),而不是新内容、也不是空
        assert await kb.list_items() == before
        assert await kb.list_items() == {"demo": {"table": 1}}, (
            "旧镜像必须还在:文件是新的、代码是旧的,退回上次读懂的样子"
        )

        refused = kb.refused_assets
        assert len(refused) == 1
        (rel_path, reason), = refused.items()
        assert rel_path == "demo/schema_notes.yml"
        assert str(FORMAT_VERSION + 998) in reason

    async def test_legacy_asset_is_mirrored(self, tmp_path):
        """存量库(无 `_meta`)照常入镜像 —— 升级不能把所有人挡在门外。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(
            yaml.safe_dump(_SCHEMA_NOTES, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        await kb.force_sync("demo")
        assert await kb.list_items() == {"demo": {"table": 1}}
        assert kb.refused_assets == {}

    async def test_recovery_clears_the_refusal(self, tmp_path):
        """文件换回可读版本 → refused 报告里必须消失(否则运维永远在灭火)。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        _write_future(notes, _SCHEMA_NOTES)
        await kb.force_sync("demo")
        assert kb.refused_assets

        _write(notes, _SCHEMA_NOTES)
        await kb.force_sync("demo")
        assert kb.refused_assets == {}
        assert await kb.list_items() == {"demo": {"table": 1}}

    async def test_status_reports_assets_and_refusals(self, tmp_path):
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        _write(notes, _SCHEMA_NOTES)
        _write_future(kb.kb_dir / "demo" / "semantics.yml", {"semantic_model": []})
        await kb.force_sync("demo")

        status = await kb.kb_status("demo")
        by_file = {a["file"]: a for a in status["assets"]}
        assert by_file["schema_notes.yml"]["generator"] == "kb_init"
        assert by_file["schema_notes.yml"]["format"] == FORMAT_VERSION
        assert by_file["schema_notes.yml"]["edited"] is False
        assert by_file["semantics.yml"]["refused"]
        assert "demo/semantics.yml" in status["refused_assets"]


class TestSemanticGate:
    def _provider(self, tmp_path, kb_yml):
        from trove.services.semantic_layer.provider import SemanticLayerProvider

        return SemanticLayerProvider(
            directory=tmp_path / "empty-semantic",
            datasource="demo",
            dialect="sqlite",
            kb_semantics_path=kb_yml,
        )

    async def test_newer_semantics_keeps_last_known_good(self, tmp_path):
        """语义资产的拒绝 = 整份不采纳(不是跳过看一眼)。"""
        kb_yml = tmp_path / "semantics.yml"
        doc = {
            "semantic_model": [{
                "name": "demo",
                "datasets": [{"name": "loan"}],
                "metrics": [{
                    "name": "平均贷款金额",
                    "expression": {"dialects": [
                        {"dialect": "ANSI_SQL", "expression": "AVG(loan.amount)"},
                    ]},
                }],
            }],
        }
        _write(kb_yml, doc)
        provider = self._provider(tmp_path, kb_yml)
        assert [m.name for m in provider.model().metrics] == ["平均贷款金额"]

        # 换成本代码读不懂的新格式:内容也换了,但绝不能生效
        future = dict(doc)
        future["semantic_model"] = [{
            "name": "demo",
            "datasets": [{"name": "loan"}],
            "metrics": [{
                "name": "未来指标",
                "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "SUM(loan.amount)"},
                ]},
            }],
        }]
        _write_future(kb_yml, future)
        provider._reload()
        assert [m.name for m in provider.model().metrics] == ["平均贷款金额"]

    async def test_current_format_is_adopted(self, tmp_path):
        """当前格式照常生效(门不能把正常路径也挡住)。"""
        kb_yml = tmp_path / "semantics.yml"
        doc = {
            "semantic_model": [{
                "name": "demo",
                "datasets": [{"name": "loan"}],
                "metrics": [{
                    "name": "第一版",
                    "expression": {"dialects": [
                        {"dialect": "ANSI_SQL", "expression": "AVG(loan.amount)"},
                    ]},
                }],
            }],
        }
        _write(kb_yml, doc)
        provider = self._provider(tmp_path, kb_yml)
        assert [m.name for m in provider.model().metrics] == ["第一版"]

        doc["semantic_model"][0]["metrics"][0]["name"] = "第二版"
        _write(kb_yml, doc)
        provider._reload()
        assert [m.name for m in provider.model().metrics] == ["第二版"]


class TestWritersRestamp:
    """每条 Trove 写路径写完读回,都必须判为"没被人改过"。

    一个漏打 `_meta` 的写入口 = 整批机器写入在 C2 的合并里伪装成人的编辑。
    """

    async def _kb(self, tmp_path, ds="demo"):
        kb = KbService(tmp_path / "proj")
        _write(kb.kb_dir / ds / "schema_notes.yml", _SCHEMA_NOTES)
        return kb

    @staticmethod
    def _edited(path) -> bool | None:
        return body_edited(yaml.safe_load(path.read_text(encoding="utf-8")))

    async def test_append_lesson(self, tmp_path):
        kb = await self._kb(tmp_path)
        await kb.append_lesson(
            {"pattern": "p", "note": "n", "confirmed": False}, "demo",
        )
        assert self._edited(kb.kb_dir / "demo" / "lessons.yml") is False

    async def test_rate_lesson(self, tmp_path):
        kb = await self._kb(tmp_path)
        await kb.rate_lesson(
            {"vote": 1, "question": "q", "note": "n"}, "demo",
        )
        assert self._edited(kb.kb_dir / "demo" / "lessons.yml") is False

    async def test_confirm_and_reject_lesson(self, tmp_path):
        kb = await self._kb(tmp_path)
        await kb.append_lesson({"pattern": "p", "confirmed": False}, "demo")
        assert await kb.confirm_lesson("demo", "p") is True
        assert self._edited(kb.kb_dir / "demo" / "lessons.yml") is False
        assert await kb.reject_lesson("demo", "p") is True
        assert self._edited(kb.kb_dir / "demo" / "lessons.yml") is False

    async def test_confirm_pending_lessons(self, tmp_path):
        kb = await self._kb(tmp_path)
        await kb.append_lesson({"pattern": "p", "confirmed": False}, "demo")
        assert await kb.confirm_pending_lessons("demo") == 1
        assert self._edited(kb.kb_dir / "demo" / "lessons.yml") is False

    async def test_lesson_promotion(self, tmp_path):
        kb = await self._kb(tmp_path)
        await kb.append_lesson(
            {"pattern": "p", "confirmed": False, "confidence": 0.4}, "demo",
        )
        res = await kb.update_lesson_confidence(
            "demo", "p", evidence_kind="repeated_correction", count=5,
            threshold=0.9,
        )
        assert res.get("updated") is True
        assert self._edited(kb.kb_dir / "demo" / "lessons.yml") is False

    async def test_draft_example(self, tmp_path):
        kb = await self._kb(tmp_path)
        res = await kb.draft_example("q", "SELECT 1", "demo")
        assert res["status"] == "drafted"
        assert self._edited(kb.kb_dir / "demo" / "examples.yml") is False

    async def test_confirm_and_reject_examples(self, tmp_path):
        kb = await self._kb(tmp_path)
        await kb.draft_example("q", "SELECT 1", "demo")
        assert await kb.confirm_pending_examples("demo") == 1
        assert self._edited(kb.kb_dir / "demo" / "examples.yml") is False
        await kb.draft_example("q2", "SELECT 2", "demo")
        assert await kb.reject_pending_examples("demo") == 1
        assert self._edited(kb.kb_dir / "demo" / "examples.yml") is False

    async def test_append_example_and_term(self, tmp_path):
        kb = await self._kb(tmp_path)
        await kb.append_example({"question": "q", "sql": "SELECT 1"}, "demo")
        assert self._edited(kb.kb_dir / "demo" / "examples.yml") is False
        await kb.append_term(
            {"term": "贷款金额", "mapping": "loan.amount", "tables": ["loan"]},
            "demo",
        )
        assert self._edited(kb.kb_dir / "demo" / "semantics.yml") is False

    async def test_init_files_are_stamped(self, tmp_path):
        kb = KbService(tmp_path / "proj")
        assert kb.init_notes(_SCHEMA_NOTES["tables"], "demo", overwrite=True)
        assert self._edited(kb.kb_dir / "demo" / "schema_notes.yml") is False

    async def test_a_human_edit_is_still_detected(self, tmp_path):
        """写路径全打 `_meta`,但人的编辑仍必须**被判出来** —— 否则 C2 的合并
        会把人的修改当成机器产物直接丢掉。"""
        kb = await self._kb(tmp_path)
        path = kb.kb_dir / "demo" / "examples.yml"
        await kb.draft_example("q", "SELECT 1", "demo")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["examples"][0]["sql"] = "SELECT 999  -- 人改的"
        path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8",
        )
        assert self._edited(path) is True
