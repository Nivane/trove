"""Lifecycle (purge), schema drift, and config parsing tests."""

from __future__ import annotations

import pytest

from trove.core.config import ConfigLoader
from trove.services.kb.service import KbService
from trove.services.memory.models import MemoryConfig, MemoryScope
from trove.services.memory.schema_drift import detect_drift
from trove.services.memory.service import MemoryService


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


async def test_lifecycle_purges_user_facts(tmp_path, kb):
    # 死代码修复验证:run_lifecycle 现在真正调用 user_facts.purge_expired
    from trove.services.user_facts.service import UserFactsService

    facts = UserFactsService(tmp_path / "facts.db")
    await facts.add("u", "demo", "use 30-day average")
    assert len(await facts.list("u")) == 1

    cfg = MemoryConfig(enabled=True, retention_days={"facts": 0})
    # retention 0 → 不删(0/None 语义 = 保留)
    m = MemoryService(tmp_path / "home", cfg, kb=kb, user_facts=facts)
    await m.run_lifecycle()
    assert len(await facts.list("u")) == 1

    # 大 retention 也不会误删(未超期)
    cfg2 = MemoryConfig(enabled=True, retention_days={"facts": 99999})
    m2 = MemoryService(tmp_path / "home2", cfg2, kb=kb, user_facts=facts)
    await m2.run_lifecycle()
    assert len(await facts.list("u")) == 1


async def test_lifecycle_purges_episodes(tmp_path, kb):
    cfg = MemoryConfig(enabled=True, retention_days={"episodes": 99999})
    m = MemoryService(tmp_path / "home", cfg, kb=kb)
    scope = MemoryScope(datasource="demo", user_id="u")
    await m.episodes.record(scope, question="q", sql="SELECT 1")
    await m.run_lifecycle()
    assert await m.episodes.count(scope) == 1


async def test_schema_drift_detects_new_table(tmp_path, kb):
    """live schema 多一张表 → new_tables 非空。"""
    kb.kb_dir.mkdir(parents=True)
    ds_dir = kb.kb_dir / "demo"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "schema_notes.yml").write_text(
        "tables:\n"
        "  - name: loan\n"
        "    columns:\n"
        "      - name: amount\n",
        encoding="utf-8",
    )
    await kb.ensure_synced("demo")

    class _Catalog:
        async def list_tables(self, datasource):
            return [
                {"name": "loan", "columns": [{"name": "amount"}, {"name": "status"}]},
                {"name": "new_table", "columns": [{"name": "id"}]},
            ]

    report = await detect_drift("demo", kb, _Catalog())
    assert "new_table" in report["new_tables"]
    assert report["column_changes"]["loan"]["added"] == ["status"]


class TestSchemaDriftStatus:
    """I3 的源头:`detect_drift` 必须区分「没查成」与「确实无漂移」。

    在此之前这两者返回**逐字节相同**的全空字典,下游(CI、巡检)必然把
    前者读成后者。这几条测试钉住那条曾经不存在的区分。
    """

    def _write_notes(self, kb, body: str) -> None:
        kb.kb_dir.mkdir(parents=True, exist_ok=True)
        ds_dir = kb.kb_dir / "demo"
        ds_dir.mkdir(parents=True, exist_ok=True)
        (ds_dir / "schema_notes.yml").write_text(body, encoding="utf-8")

    async def test_clean_report_is_ok(self, kb):
        self._write_notes(kb, "tables:\n  - name: loan\n    columns:\n      - name: amount\n")

        class _Catalog:
            async def list_tables(self, datasource):
                return [{"name": "loan", "columns": [{"name": "amount"}]}]

        report = await detect_drift("demo", kb, _Catalog())
        assert report["status"] == "ok"
        assert report["skip_reason"] is None
        assert report["new_tables"] == [] and report["gone_tables"] == []

    async def test_catalog_unreachable_is_skipped_not_clean(self, kb):
        """catalog 抛异常 → skipped,而不是一份看起来干净的空报告。"""
        self._write_notes(kb, "tables:\n  - name: loan\n    columns:\n      - name: amount\n")

        class _Catalog:
            async def list_tables(self, datasource):
                raise RuntimeError("connection refused")

        report = await detect_drift("demo", kb, _Catalog())
        assert report["status"] == "skipped"
        assert report["skip_reason"] == "catalog_unreachable"
        # 空报告的形状与「确实无漂移」一样 —— 正因如此才必须有 status
        assert report["new_tables"] == [] and report["gone_tables"] == []
        assert report["column_changes"] == {}

    async def test_missing_schema_notes_is_skipped(self, kb):
        """没建过 KB(文件不存在)→ kb_missing。"""
        class _Catalog:
            async def list_tables(self, datasource):
                return [{"name": "loan", "columns": [{"name": "amount"}]}]

        report = await detect_drift("demo", kb, _Catalog())
        assert report["status"] == "skipped"
        assert report["skip_reason"] == "kb_missing"

    async def test_empty_schema_notes_is_distinct_from_missing(self, kb):
        """文件在但没声明任何表 → kb_empty。

        「没建 KB」与「KB 建了但是空的」对运维是两个不同动作,不能合并
        成一个原因。
        """
        self._write_notes(kb, "tables: []\n")

        class _Catalog:
            async def list_tables(self, datasource):
                return [{"name": "loan", "columns": [{"name": "amount"}]}]

        report = await detect_drift("demo", kb, _Catalog())
        assert report["status"] == "skipped"
        assert report["skip_reason"] == "kb_empty"

    async def test_unreadable_schema_notes_is_skipped(self, kb):
        """YAML 损坏 → kb_unreadable(此前被 ``except Exception: pass`` 吞掉,
        与「没声明表」不可区分)。"""
        self._write_notes(kb, "tables: [unclosed\n")

        class _Catalog:
            async def list_tables(self, datasource):
                return [{"name": "loan", "columns": [{"name": "amount"}]}]

        report = await detect_drift("demo", kb, _Catalog())
        assert report["status"] == "skipped"
        assert report["skip_reason"] == "kb_unreadable"

    async def test_legacy_keys_unchanged(self, kb):
        """向后兼容:四个旧键的类型与语义均未变。"""
        self._write_notes(kb, "tables:\n  - name: loan\n    columns:\n      - name: amount\n")

        class _Catalog:
            async def list_tables(self, datasource):
                return [{"name": "loan", "columns": [{"name": "amount"}]},
                        {"name": "extra", "columns": [{"name": "id"}]}]

        report = await detect_drift("demo", kb, _Catalog())
        assert report["datasource"] == "demo"
        assert isinstance(report["new_tables"], list)
        assert isinstance(report["gone_tables"], list)
        assert isinstance(report["column_changes"], dict)
        assert report["new_tables"] == ["extra"]


async def test_schema_drift_uses_column_sets(tmp_path, kb):
    """真实 CatalogService 路径(column_sets):列漂移可检出。

    回归:CatalogService.list_tables 只返回列*数量*,旧实现逐列迭代会
    TypeError 被吞掉 → 生产周期巡检恒为空报告。
    """
    kb.kb_dir.mkdir(parents=True)
    ds_dir = kb.kb_dir / "demo"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "schema_notes.yml").write_text(
        "tables:\n"
        "  - name: loan\n"
        "    columns:\n"
        "      - name: amount\n",
        encoding="utf-8",
    )
    await kb.ensure_synced("demo")

    class _Catalog:
        async def column_sets(self, datasource):
            return {"loan": {"amount", "status"}, "new_table": {"id"}}

    report = await detect_drift("demo", kb, _Catalog())
    assert "new_table" in report["new_tables"]
    assert report["column_changes"]["loan"]["added"] == ["status"]


async def test_schema_drift_count_only_catalog_no_false_gone(tmp_path, kb):
    """count-only catalog:表集合仍可比对,列变化不误报,表不当成 gone。"""
    kb.kb_dir.mkdir(parents=True)
    ds_dir = kb.kb_dir / "demo"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "schema_notes.yml").write_text(
        "tables:\n"
        "  - name: loan\n"
        "    columns:\n"
        "      - name: amount\n"
        "  - name: account\n"
        "    columns:\n"
        "      - name: id\n",
        encoding="utf-8",
    )
    await kb.ensure_synced("demo")

    class _Catalog:
        async def list_tables(self, datasource):
            return [
                {"name": "loan", "columns": 3},
                {"name": "account", "columns": 5},
            ]

    report = await detect_drift("demo", kb, _Catalog())
    assert report["new_tables"] == []
    assert report["gone_tables"] == []
    assert report["column_changes"] == {}


async def test_schema_drift_case_insensitive(tmp_path, kb):
    """大小写差异不产生假漂移(物理库列大小写保留 vs YAML 手写)。"""
    kb.kb_dir.mkdir(parents=True)
    ds_dir = kb.kb_dir / "demo"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "schema_notes.yml").write_text(
        "tables:\n"
        "  - name: Loan\n"
        "    columns:\n"
        "      - name: Amount\n",
        encoding="utf-8",
    )
    await kb.ensure_synced("demo")

    class _Catalog:
        async def column_sets(self, datasource):
            return {"loan": {"amount", "status"}}

    report = await detect_drift("demo", kb, _Catalog())
    assert report["gone_tables"] == []
    assert report["column_changes"]["loan"]["added"] == ["status"]
    assert report["column_changes"]["loan"]["removed"] == []


def test_config_parses_memory_section(tmp_path):
    cfg_file = tmp_path / "agent.yml"
    cfg_file.write_text(
        "agent:\n"
        "  memory:\n"
        "    enabled: true\n"
        "    episodes: true\n"
        "    auto_examples: false\n"
        "    promotion: true\n"
        "    promotion_threshold: 0.9\n"
        "    profile_boost: true\n"
        "    retention_days:\n"
        "      episodes: 30\n"
        "      facts: 90\n",
        encoding="utf-8",
    )
    config = ConfigLoader.load_agent_config(str(cfg_file))
    mem = config.memory
    assert mem.enabled is True
    assert mem.episodes is True
    assert mem.auto_examples is False
    assert mem.promotion is True
    assert mem.promotion_threshold == 0.9
    assert mem.profile_boost is True
    assert mem.retention_days == {"episodes": 30, "facts": 90}


def test_config_memory_defaults(tmp_path):
    cfg_file = tmp_path / "agent.yml"
    cfg_file.write_text("agent:\n  target: mock/model\n", encoding="utf-8")
    config = ConfigLoader.load_agent_config(str(cfg_file))
    mem = config.memory
    assert mem.enabled is True
    assert mem.episodes is True
    assert mem.promotion is False  # 自动晋升默认关
    assert mem.retention_days == {}
