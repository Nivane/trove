"""语义层统一构造 —— 生产启动与体检共用一条。

钉的是**构造契约**:同样的输入(配置目录、KB semantics 路径、schema、
方言)产出同样的 provider;「未启用」是 ``None`` 而不是一个假装可用的
半成品 —— 体检拿 ``None`` 就该判 ``unmatched``,而不是拿着空 provider
把每道题都编成「无模型」。
"""

from __future__ import annotations

from pathlib import Path

from trove.core.config import AgentConfig
from trove.core.types import ColumnInfo, SchemaInfo, TableInfo
from trove.services.semantic_layer.factory import build_provider

DS = "demo"


class _Adapter:
    def __init__(self, dialect="mysql", schema=None):
        self._dialect = dialect
        self._schema = schema or SchemaInfo(tables=[
            TableInfo(name="loan", columns=[
                ColumnInfo(name="loan_id", type="integer"),
                ColumnInfo(name="Amount", type="decimal"),
            ]),
        ])
        self.schema_calls = 0

    def dialect(self) -> str:
        return self._dialect

    async def get_schema(self):
        self.schema_calls += 1
        return self._schema


class _Kb:
    def __init__(self, path: Path | None):
        self._path = path

    def semantics_path(self, datasource: str) -> Path | None:
        return self._path


def _config(**kw) -> AgentConfig:
    return AgentConfig(target="mock/model", **kw)


async def test_enabled_by_kb_semantics_file(tmp_path):
    kb_path = tmp_path / "kb" / DS / "semantics.yml"
    kb_path.parent.mkdir(parents=True)
    kb_path.write_text("version: 0.2.0.dev0\n")
    adapter = _Adapter()
    provider = await build_provider(
        _Kb(kb_path), _config(), DS, adapter, root=tmp_path)
    assert provider is not None
    assert provider.datasource == DS
    assert provider.directory == tmp_path / ".trove" / "semantic" / DS
    # catalog / table_exists 都来自同一次 schema 读(小写归一)
    assert provider._catalog["loan"] == {"loan_id", "amount"}
    assert provider._table_exists("LOAN") is True
    assert provider._table_exists("nope") is False


async def test_disabled_when_neither_source_exists(tmp_path):
    adapter = _Adapter()
    provider = await build_provider(
        _Kb(tmp_path / "missing.yml"), _config(), DS, adapter, root=tmp_path)
    assert provider is None


async def test_config_directory_counts_as_enabled(tmp_path):
    semantic_dir = tmp_path / ".trove" / "semantic" / DS
    semantic_dir.mkdir(parents=True)
    (semantic_dir / "model.yml").write_text("datasets: []\n")
    provider = await build_provider(
        _Kb(None), _config(), DS, _Adapter(), root=tmp_path)
    assert provider is not None
    assert provider.directory == semantic_dir


async def test_semantic_layer_path_prefix_is_honoured(tmp_path):
    prefix = tmp_path / "custom"
    (prefix / DS).mkdir(parents=True)
    (prefix / DS / "model.yml").write_text("datasets: []\n")
    provider = await build_provider(
        _Kb(None), _config(semantic_layer_path="custom"), DS, _Adapter(),
        root=tmp_path)
    assert provider is not None
    assert provider.directory == prefix / DS


async def test_open_source_dir_does_not_report_itself_enabled(tmp_path):
    # 目录存在但没有任何 yaml —— 与「未建模」同义,不能因为 mkdir 过就启用。
    (tmp_path / ".trove" / "semantic" / DS).mkdir(parents=True)
    assert await build_provider(
        _Kb(None), _config(), DS, _Adapter(), root=tmp_path) is None


async def test_injected_schema_skips_the_live_read(tmp_path):
    kb_path = tmp_path / "semantics.yml"
    kb_path.write_text("version: 0.2.0.dev0\n")
    adapter = _Adapter()
    schema = SchemaInfo(tables=[TableInfo(name="t", columns=[
        ColumnInfo(name="c", type="text")])])
    provider = await build_provider(
        _Kb(kb_path), _config(), DS, adapter, schema=schema, root=tmp_path)
    assert adapter.schema_calls == 0
    assert provider._catalog == {"t": {"c"}}


async def test_dialect_is_taken_from_the_adapter(tmp_path):
    kb_path = tmp_path / "semantics.yml"
    kb_path.write_text("version: 0.2.0.dev0\n")
    provider = await build_provider(
        _Kb(kb_path), _config(), DS, _Adapter(dialect="snowflake"), root=tmp_path)
    assert provider._read == "snowflake"
