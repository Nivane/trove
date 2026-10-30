"""Version & compatibility registry — closed vocabularies, pure detectors, frozen rows.

版本号在这个仓里承担**几种互不相同**的职责:持久化结构迁移、持久化内容/载荷
格式、单调修订计数、纯信息戳、派生缓存键、派生产物结构版本。混在一起谈会得到
错误结论(「B 规则改了以后 A 的历史全错位」就是一次实例)。本模块把它们拆成
闭集词汇,并用纯 AST 检测器把「新增一处版本」变成一次必红的登记要求。

**这些是棘轮,不是沙箱。** 动态 import、``getattr``、运行时拼出的键名没有 AST
形状,检测不到;``MAX_SQL_VERSIONS`` 这类「名字像版本、实为版本链长度上限」
由显式排除表处理(新误报必须登记理由,不许静默放宽)。

每个 ``VersionPoint`` 引用运行时权威并冻结其值;读到更新/更老版本时的行为必须
写进 ``newer``/``older``(无兼容问题写 ``n/a``)。有读者分支的行必须引用证据
节点;缺口行也照常引用 golden 钉住现状 —— 补门时 golden 必红,逼迫同步改注册
表与文档。这就是「有意的摩擦」。
"""
from __future__ import annotations

import ast
import importlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest

import trove
from tests.architecture.test_state_ownership import STORE_CLASSES
from tests.helpers import arch

Sources = Mapping[str, str]
REAL = arch.py_sources()
ROOT = Path(__file__).resolve().parents[2]

VersionValue = int | str | dict[str, str]


# ── 词汇闭集(冻结) ─────────────────────────────────────────────────────
#
# kind:
#   schema        — 持久化结构迁移版本(表/列的形状)
#   format        — 持久化内容/载荷格式(读端可能拒绝或降级)
#   revision      — 单调修订计数(不是兼容门,回滚也只会 +1)
#   informational — 写入但无消费方分支的戳
#   cache_key     — 派生缓存键(不持久化,升级即失效)
#   derived       — 派生产物结构版本(重生即升级)
# newer/older 是**读到更新/更老版本时的行为**,不是「我们希望怎样」。

KINDS = frozenset({
    "schema", "format", "revision", "informational", "cache_key", "derived",
})
NEWER = frozenset({
    "refuse", "degrade", "fold_unknown", "rebuild", "pass_through",
    "cache_invalidate", "n/a",
})
OLDER = frozenset({
    # 老版本也可能降级(跨 taxonomy diff 照算但打标)或被拒绝
    # (action ``approvals_required`` 是双向闭集,不是「老的就兼容」)。
    "migrate", "adopt", "rebuild", "accept", "degrade", "pass_through",
    "refuse", "cache_invalidate", "n/a",
})

#: 版本链长度上限,不是版本号。名字像版本是唯一理由,冻结在这里而不是放宽规则。
NON_VERSION_CONSTANTS: frozenset[tuple[str, str]] = frozenset({
    ("trove/workflow/state.py", "MAX_SQL_VERSIONS"),
})

WRITE_KEYS = frozenset({"version", "record_version"})


# ── 纯检测器(AST) ─────────────────────────────────────────────────────


def _is_versionish(name: str) -> bool:
    return name == "__version__" or (name.isupper() and "VERSION" in name)


def _scalar_constant(node: ast.AST) -> int | str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)):
        if isinstance(node.value, bool):
            return None
        return node.value
    return None


def _constant_value(node: ast.AST) -> VersionValue | None:
    scalar = _scalar_constant(node)
    if scalar is not None:
        return scalar
    if not isinstance(node, ast.Dict):
        return None
    out: dict[str, str] = {}
    for key, value in zip(node.keys, node.values):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return None
        out[key.value] = value.value
    return out


def module_version_constants(
    sources: Sources,
    *,
    excluded: frozenset[tuple[str, str]] = NON_VERSION_CONSTANTS,
) -> dict[tuple[str, str], VersionValue]:
    """模块 body 的版本常量 → ``{(rel, name): value}``。

    只认模块顶层 ``Assign``/``AnnAssign``(函数/类里的局部变量不是权威);
    名字为 ``__version__`` 或全大写且含 ``VERSION``;值为 int/str 标量或
    ``dict[str, str]``(``DOMAIN_VERSIONS`` 形态)。``excluded`` 让测试能证明
    排除表仍然活着(传空集即可看到被排除项)。
    """
    found: dict[tuple[str, str], VersionValue] = {}
    for rel, source in sources.items():
        for node in ast.parse(source).body:
            targets: list[ast.Name] = []
            value_node: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets = [t for t in node.targets if isinstance(t, ast.Name)]
                value_node = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                targets = [node.target]
                value_node = node.value
            if value_node is None:
                continue
            for target in targets:
                if (rel, target.id) in excluded:
                    continue
                if not _is_versionish(target.id):
                    continue
                value = _constant_value(value_node)
                if value is None:
                    continue
                key = (rel, target.id)
                if key in found:
                    raise ValueError(f"{rel} defines version constant {target.id} twice")
                found[key] = value
    return found


class _LiteralWriteVisitor(ast.NodeVisitor):
    """收集 dict 字面量与下标赋值里的常量版本写入,作用域不含行号。"""

    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.found: dict[tuple[str, str, str], int | str] = {}

    def _context(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _record(self, key: str, value: int | str) -> None:
        site = (self.rel, self._context(), key)
        if site in self.found:
            raise ValueError(
                f"{self.rel}::{site[1]}::{key} has two constant version writes")
        self.found[site] = value

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Dict(self, node: ast.Dict) -> None:
        for key_node, value_node in zip(node.keys, node.values):
            if not isinstance(key_node, ast.Constant):
                continue
            if key_node.value not in WRITE_KEYS:
                continue
            scalar = _scalar_constant(value_node)
            if scalar is not None:
                self._record(key_node.value, scalar)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            key_node = target.slice
            if not isinstance(key_node, ast.Constant):
                continue
            if key_node.value not in WRITE_KEYS:
                continue
            scalar = _scalar_constant(node.value)
            if scalar is not None:
                self._record(key_node.value, scalar)
        self.generic_visit(node)


def literal_version_writes(sources: Sources) -> dict[tuple[str, str, str], int | str]:
    """载荷里的常量版本写入 → ``{(rel, context, key): value}``。

    两种形态:① dict 字面量键 ``version``/``record_version`` 且值为常量;
    ② ``x["version"] = <常量>``。动态值(``preset.version``)不认。``context``
    是 ``Class.method`` / ``<module>``,不含行号(搬代码不应引起重登记)。
    """
    found: dict[tuple[str, str, str], int | str] = {}
    for rel, source in sources.items():
        visitor = _LiteralWriteVisitor(rel)
        visitor.visit(ast.parse(source))
        found.update(visitor.found)
    return found


def migration_versions(sources: Sources) -> dict[str, int]:
    """``Migration(version=<int 常量>)`` → ``{rel: 最大版本}``。

    import 了 ``Migration`` 却无可解析调用、或 version 关键字非常量,都**报错**
    (解析盲区不能静默漏登)。别名导入同样会因为「无调用」而响亮失败。
    """
    found: dict[str, int] = {}
    for rel, source in sources.items():
        tree = ast.parse(source)
        imported = any(
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "Migration" for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imported:
            continue
        versions: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            named = (
                isinstance(func, ast.Name) and func.id == "Migration"
            ) or (
                isinstance(func, ast.Attribute) and func.attr == "Migration"
            )
            if not named:
                continue
            keyword = next(
                (k for k in node.keywords if k.arg == "version"), None)
            if keyword is None:
                raise ValueError(
                    f"{rel}: Migration(...) without a version= keyword")
            version = _scalar_constant(keyword.value)
            if not isinstance(version, int):
                raise ValueError(
                    f"{rel}: Migration(version=...) must be an int constant")
            versions.append(version)
        if not versions:
            raise ValueError(
                f"{rel}: imports Migration but has no parseable "
                "Migration(version=...) call")
        found[rel] = max(versions)
    return found


def store_posture(
    store_classes: Mapping[str, str] = STORE_CLASSES,
) -> dict[str, str]:
    """durable/derived 的 store 类 → ``{模块路径: 分类}``。

    不扫 ``CREATE TABLE``:27 个文件含 DDL 字符串(解析器/demo/适配器/
    information_schema 都是噪声)。``STORE_CLASSES`` 是已有权威清单。
    """
    out: dict[str, str] = {}
    for qname, kind in store_classes.items():
        if kind not in {"durable", "derived"}:
            continue
        module = qname.rpartition(".")[0]
        rel = module.replace(".", "/") + ".py"
        previous = out.get(rel)
        if previous is not None and previous != kind:
            raise ValueError(
                f"{rel} has both {previous} and {kind} store classes")
        out[rel] = kind
    return out


# ── 冻结发现集 ─────────────────────────────────────────────────────────


CONSTANT_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("trove/__init__.py", "__version__"),
    ("trove/core/run_ledger.py", "LEDGER_VERSION"),
    ("trove/services/decision/rules.py", "SCHEMA_VERSION"),
    ("trove/services/evalset/models.py", "EVALSET_VERSION"),
    ("trove/services/extensions/envelope.py", "DOMAIN_VERSIONS"),
    ("trove/services/kb/provenance.py", "FORMAT_VERSION"),
    ("trove/services/kb/service.py", "_MIRROR_VERSION"),
    ("trove/services/question_coverage/taxonomy.py", "TAXONOMY_VERSION"),
    ("trove/services/semantic_layer/ossie.py", "OSSIE_VERSION"),
    ("trove/workflow/nodes/gen_sql.py", "SQL_TOOL_VERSION"),
})

#: 名字不符合自动发现规则(不含 VERSION / 大小写混排),但是真正的版本声明。
CURATED_CONSTANT_KEYS: frozenset[tuple[str, str]] = frozenset({
    ("trove/services/action/models.py", "APPROVALS_REQUIRED_V1"),
    ("trove/services/extensions/pack.py", "PACK_SCHEMA"),
})

LITERAL_WRITE_KEYS: frozenset[tuple[str, str, str]] = frozenset({
    ("trove/services/analysis/engine.py", "analysis_payload", "version"),
    ("trove/services/skills/service.py", "SkillService.create", "version"),
    ("trove/storage/run_record_store.py", "<module>", "record_version"),
    ("trove/tracing/run_record.py", "_Buffer.__init__", "record_version"),
})


# ── 注册表 ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VersionPoint:
    id: str
    authority: str
    value: VersionValue
    kind: str
    newer: str
    older: str
    evidence: tuple[str, ...] = ()
    gap: str | None = None


@dataclass(frozen=True)
class MigrationStore:
    path: str
    max_version: int
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class UnversionedStore:
    path: str
    reason: str


REGISTRY: tuple[VersionPoint, ...] = (
    VersionPoint(
        id="product-version",
        authority="trove/__init__.py::__version__",
        value="0.1.0",
        kind="informational",
        newer="pass_through",
        older="pass_through",
        evidence=(
            "tests/architecture/test_version_compatibility.py"
            "::test_product_version_matches_pyproject",
        ),
    ),
    VersionPoint(
        id="run-ledger-version",
        authority="trove/core/run_ledger.py::LEDGER_VERSION",
        value=1,
        kind="derived",
        newer="n/a",
        older="n/a",
        evidence=(
            "tests/architecture/test_run_lifecycle_contracts.py"
            "::TestLedgerVocabulary::test_top_level_keys_are_frozen",
        ),
    ),
    VersionPoint(
        id="sql-tool-version",
        authority="trove/workflow/nodes/gen_sql.py::SQL_TOOL_VERSION",
        value="2.0",
        kind="informational",
        newer="n/a",
        older="n/a",
    ),
    VersionPoint(
        id="taxonomy-version",
        authority="trove/services/question_coverage/taxonomy.py"
        "::TAXONOMY_VERSION",
        value=1,
        kind="informational",
        newer="degrade",
        older="degrade",
        evidence=(
            "tests/services/question_coverage/test_service.py"
            "::test_diff_flags_taxonomy_version_change",
        ),
    ),
    VersionPoint(
        id="kb-mirror-version",
        authority="trove/services/kb/service.py::_MIRROR_VERSION",
        value=1,
        kind="derived",
        newer="refuse",
        older="adopt",
        evidence=(
            "tests/services/kb/test_hybrid_backend.py"
            "::TestFtsMirror::test_a_newer_mirror_is_refused",
            "tests/services/kb/test_kb_service.py"
            "::TestMirrorSchema::test_mirror_rebuilt_without_datasource_column",
        ),
    ),
    VersionPoint(
        id="kb-format-version",
        authority="trove/services/kb/provenance.py::FORMAT_VERSION",
        value=1,
        kind="format",
        newer="refuse",
        older="adopt",
        evidence=(
            "tests/services/kb/test_provenance.py"
            "::TestFormatGate::test_newer_format_is_refused",
            "tests/services/kb/test_provenance.py"
            "::TestStampAndRead::test_absent_meta_reads_as_legacy",
        ),
    ),
    VersionPoint(
        id="ossie-version",
        authority="trove/services/semantic_layer/ossie.py::OSSIE_VERSION",
        value="0.2.0.dev0",
        kind="format",
        newer="pass_through",
        older="pass_through",
        evidence=(
            "tests/services/semantic_layer/test_ossie.py"
            "::test_unknown_newer_version_is_preserved_without_refusal",
            "tests/services/semantic_layer/test_ossie.py"
            "::test_written_documents_share_one_version_authority",
        ),
        gap="ossie-version-gate",
    ),
    VersionPoint(
        id="evalset-version",
        authority="trove/services/evalset/models.py::EVALSET_VERSION",
        value=1,
        kind="format",
        newer="pass_through",
        older="pass_through",
        evidence=(
            "tests/services/evalset/test_store.py"
            "::test_newer_version_round_trips_without_refusal",
            "tests/services/evalset/test_store.py"
            "::test_non_integer_version_folds_to_default",
        ),
        gap="evalset-newer-refusal",
    ),
    VersionPoint(
        id="decision-schema-version",
        authority="trove/services/decision/rules.py::SCHEMA_VERSION",
        value=4,
        kind="format",
        newer="refuse",
        older="accept",
        evidence=(
            "tests/services/decision/test_decision_rules.py"
            "::TestParse::test_newer_document_version_refused",
            "tests/services/decision/test_decision_rules.py"
            "::TestSchemaV4Parse::test_current_schema_version_is_four",
        ),
    ),
    VersionPoint(
        id="pack-schema",
        authority="trove/services/extensions/pack.py::PACK_SCHEMA",
        value=1,
        kind="format",
        newer="refuse",
        older="accept",
        evidence=(
            "tests/services/extensions/test_export_import.py"
            "::test_newer_pack_schema_lands_nothing",
            "tests/services/extensions/test_pack.py"
            "::test_pack_schema_two_refuses",
        ),
    ),
    VersionPoint(
        id="action-approvals-required",
        authority="trove/services/action/models.py::APPROVALS_REQUIRED_V1",
        value=1,
        kind="format",
        newer="refuse",
        older="refuse",
        evidence=(
            "tests/services/action/test_action_templates.py"
            "::test_create_refuses_invalid_entries",
        ),
    ),
    VersionPoint(
        id="domain-cache-versions",
        authority="trove/services/extensions/envelope.py::DOMAIN_VERSIONS",
        value={"decision": "decision-v1", "validator": "validator-v1"},
        kind="cache_key",
        newer="cache_invalidate",
        older="cache_invalidate",
        evidence=(
            "tests/services/extensions/test_envelope.py"
            "::test_cache_key_carries_domain_version",
        ),
    ),
    VersionPoint(
        id="analysis-payload-version",
        authority="trove/services/analysis/engine.py::analysis_payload::version",
        value=2,
        kind="format",
        newer="degrade",
        older="accept",
        evidence=(
            "tests/workflow/test_output_analysis.py"
            "::TestNoiseBandSection::test_v1_payload_fixture_renders_unchanged",
            "tests/workflow/test_output_analysis.py"
            "::TestNoiseBandSection::test_v1_payload_with_series_still_renders_band",
        ),
    ),
    VersionPoint(
        id="skills-entry-version",
        authority="trove/services/skills/service.py::SkillService.create::version",
        value=1,
        kind="revision",
        newer="n/a",
        older="n/a",
        evidence=(
            "tests/services/skills/test_skills_service.py"
            "::test_create_version_starts_at_one_and_increments_on_rewrites",
            "tests/services/skills/test_skills_service.py"
            "::test_legacy_frontmatter_without_version_reads_as_one",
        ),
    ),
    VersionPoint(
        id="run-record-version",
        authority="trove/tracing/run_record.py::_Buffer.__init__::record_version",
        value=1,
        kind="informational",
        newer="pass_through",
        older="pass_through",
        evidence=(
            "tests/tracing/test_run_record_recorder.py"
            "::TestRecordVersionStamp"
            "::test_header_carries_the_record_version_stamp",
        ),
    ),
    VersionPoint(
        id="run-record-version-default",
        authority="trove/storage/run_record_store.py::<module>::record_version",
        value=1,
        kind="informational",
        newer="pass_through",
        older="pass_through",
        evidence=(
            "tests/storage/test_run_record_store.py"
            "::TestRecordVersionStamp"
            "::test_record_version_is_preserved_as_an_informational_stamp",
        ),
    ),
)


MIGRATION_STORES: tuple[MigrationStore, ...] = (
    MigrationStore(
        "trove/services/action/store.py", 2, (
            "tests/services/action/test_action_store.py"
            "::test_schema_setup_is_idempotent",
            "tests/services/action/test_action_outcomes_store.py"
            "::TestMigrationV2::test_legacy_v1_db_is_adopted_and_upgraded",
        )),
    MigrationStore(
        "trove/services/decision/verdict_store.py", 1, (
            "tests/services/decision/test_verdict_store.py"
            "::TestSchema::test_migrations_are_idempotent",
        )),
    MigrationStore(
        "trove/services/drift/store.py", 1, (
            "tests/services/drift/test_store.py"
            "::test_schema_is_created_and_reopen_is_idempotent",
            "tests/services/drift/test_store.py"
            "::test_store_is_usable_without_explicit_migration_call",
        )),
    MigrationStore(
        "trove/services/kb/ledger.py", 1, (
            "tests/services/kb/test_ledger.py"
            "::test_ledger_records_its_schema_version",
            "tests/services/kb/test_ledger.py"
            "::test_a_database_from_a_newer_trove_is_refused_but_never_fails_a_query",
        )),
    MigrationStore(
        "trove/services/memory/episode.py", 2, (
            "tests/storage/test_migrations.py"
            "::TestAbsorbedSites"
            "::test_legacy_episodes_db_gets_the_embedding_column",
            "tests/storage/test_migrations.py"
            "::TestAbsorbedSites::test_adopted_store_is_not_probed_again",
        )),
    MigrationStore(
        "trove/services/question_coverage/store.py", 1, (
            "tests/services/question_coverage/test_store.py"
            "::test_legacy_tables_without_version_row_are_adopted",
            "tests/services/question_coverage/test_store.py"
            "::test_too_new_schema_is_refused",
        )),
    MigrationStore(
        "trove/services/retrieval/pg_store.py", 2, (
            "tests/storage/test_migrations_pg.py"
            "::TestOnPostgres::test_fresh_database_reaches_the_top_version",
            "tests/storage/test_migrations_pg.py"
            "::TestOnPostgres::test_too_new_database_is_refused",
        )),
    MigrationStore(
        "trove/services/retrieval/sqlite_store.py", 4, (
            "tests/storage/test_migrations.py"
            "::TestAbsorbedSites"
            "::test_legacy_retrieval_db_gets_the_sparse_column",
            "tests/storage/test_migrations.py"
            "::TestAbsorbedSites"
            "::test_fresh_retrieval_db_lands_on_the_same_version",
            "tests/storage/test_migrations.py"
            "::TestAbsorbedSites"
            "::test_legacy_retrieval_db_drops_orphan_fts_rows",
        )),
    MigrationStore(
        "trove/storage/run_record_store.py", 1, (
            "tests/storage/test_run_record_store.py"
            "::TestRoundTrip::test_write_then_get",
        )),
)

#: 已版本化但无生产构造点的存储 —— 单独登记,不计入 store posture 分区。
UNWIRED_VERSIONED_STORES: frozenset[str] = frozenset({
    "trove/services/kb/ledger.py",
})

UNVERSIONED_STORES: tuple[UnversionedStore, ...] = (
    UnversionedStore(
        "trove/services/admin_settings/store.py",
        "设置面键值表,无 schema 版本行;计划接入迁移框架(本批不实现)"),
    UnversionedStore(
        "trove/services/auth/store.py",
        "认证表由幂等 DDL 创建,无迁移版本;计划接入迁移框架(本批不实现)"),
    UnversionedStore(
        "trove/services/datasource/config_store.py",
        "数据源注册表是 K/V 设置面,无 schema 版本行;计划接入迁移框架"),
    UnversionedStore(
        "trove/storage/config_store.py",
        "配置存储是 K/V 设置面,无 schema 版本行;计划接入迁移框架"),
    UnversionedStore(
        "trove/services/decision/drafts.py",
        "草稿侧车文件,坏形状按空集解析;无持久 schema 版本"),
    UnversionedStore(
        "trove/services/jobs/store.py",
        "jobs 表用 ensure_column 探针加列(scan_spec),版本由列在场性"
        "表达;计划迁入迁移框架"),
    UnversionedStore(
        "trove/services/kb/backends/dense.py",
        "向量索引是派生产物,重建即可升级;无持久版本行"),
    UnversionedStore(
        "trove/services/lineage/service.py",
        "血缘记录是观测数据,无兼容语义;表由幂等 DDL 创建"),
    UnversionedStore(
        "trove/services/memory/preferences.py",
        "偏好存储按保留期清理,无 schema 版本行;计划接入迁移框架"),
    UnversionedStore(
        "trove/services/retrieval/query_log.py",
        "检索查询日志是观测数据,无兼容语义;表由幂等 DDL 创建"),
    UnversionedStore(
        "trove/services/user_facts/store.py",
        "用户事实表无 schema 版本行;清理走保留期,计划接入迁移框架"),
    UnversionedStore(
        "trove/storage/session_store.py",
        "会话表由幂等 DDL 创建,无版本行;计划接入迁移框架(本批不实现)"),
    UnversionedStore(
        "trove/storage/task_store.py",
        "任务表由幂等 DDL 创建,无版本行;计划接入迁移框架(本批不实现)"),
)

#: 缺口集合(只减不增)。补齐时删条目、补正式门证据,并同步文档。
GAPS: dict[str, str] = {
    "ossie-version-gate": (
        "OSSIE 解析端透传任意 version,0.2 破坏性根形状没有门;"
        "计划在语义层升级对齐时加根形状检查(本批不实现)"),
    "evalset-newer-refusal": (
        "evalset 解析/写回原样保留任意整数 version,较新文件的未知字段"
        "保存时可能被丢弃;计划拒绝较新版本或保留未知字段(本批不实现)"),
}


# ── 辅助 ───────────────────────────────────────────────────────────────


def _parts(authority: str) -> tuple[str, list[str]]:
    rel, *rest = authority.split("::")
    return rel, rest


def _runtime_constant(rel: str, name: str) -> VersionValue:
    module = importlib.import_module(arch.module_name(rel))
    return getattr(module, name)


def _all_evidence() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for point in REGISTRY:
        out.extend((point.id, node) for node in point.evidence)
    for store in MIGRATION_STORES:
        out.extend((store.path, node) for node in store.evidence)
    return out


# ── 注册表契约(冻结 + 双向精确) ────────────────────────────────────────


def test_registry_ids_are_unique():
    ids = [point.id for point in REGISTRY]
    assert len(ids) == len(set(ids))


def test_vocabularies_are_closed():
    assert KINDS and NEWER and OLDER
    for point in REGISTRY:
        assert point.kind in KINDS, f"{point.id}: unknown kind {point.kind}"
        assert point.newer in NEWER, f"{point.id}: unknown newer {point.newer}"
        assert point.older in OLDER, f"{point.id}: unknown older {point.older}"
        if point.newer != "n/a" or point.older != "n/a":
            assert point.evidence, (
                f"{point.id}: tolerance must cite at least one evidence node")


def test_gaps_are_frozen():
    used = {point.gap for point in REGISTRY if point.gap}
    assert used == set(GAPS), (
        f"gap rows drifted — missing={set(GAPS) - used} extra={used - set(GAPS)}")
    for point in REGISTRY:
        if point.gap:
            assert point.gap in GAPS
            assert point.evidence, (
                f"{point.id}: a gap row still cites a golden pinning today")


def test_every_version_constant_is_registered():
    discovered = module_version_constants(REAL)
    arch.assert_frozen(CONSTANT_KEYS, set(discovered), "version constants")
    raw = module_version_constants(REAL, excluded=frozenset())
    arch.assert_frozen(
        CONSTANT_KEYS | NON_VERSION_CONSTANTS, set(raw), "raw version constants")

    registered = set()
    for point in REGISTRY:
        rel, rest = _parts(point.authority)
        if len(rest) == 1:
            registered.add((rel, rest[0]))
    arch.assert_frozen(
        CONSTANT_KEYS | CURATED_CONSTANT_KEYS, registered,
        "registered constants")


def test_every_literal_version_write_is_registered():
    found = literal_version_writes(REAL)
    arch.assert_frozen(LITERAL_WRITE_KEYS, set(found), "literal version writes")

    registered = set()
    for point in REGISTRY:
        rel, rest = _parts(point.authority)
        if len(rest) == 2:
            registered.add((rel, rest[0], rest[1]))
    arch.assert_frozen(
        LITERAL_WRITE_KEYS, registered, "registered literal writes")


def test_registered_values_are_frozen():
    constants = dict(module_version_constants(REAL))
    literals = literal_version_writes(REAL)
    seen: set[str] = set()
    for point in REGISTRY:
        assert point.authority not in seen, f"duplicate authority for {point.id}"
        seen.add(point.authority)
        rel, rest = _parts(point.authority)
        if len(rest) == 1:
            key = (rel, rest[0])
            if key in CURATED_CONSTANT_KEYS:
                actual = _runtime_constant(rel, rest[0])
            else:
                actual = constants[key]
        else:
            actual = literals[(rel, rest[0], rest[1])]
        assert actual == point.value, (
            f"{point.id}: {point.authority} is {actual!r}, registry says "
            f"{point.value!r} — edit the registry row")


def test_migration_max_versions_match():
    detected = migration_versions(REAL)
    arch.assert_frozen(
        {store.path for store in MIGRATION_STORES}, set(detected),
        "migration store modules")
    for store in MIGRATION_STORES:
        assert detected[store.path] == store.max_version, (
            f"{store.path}: max migration version is {detected[store.path]}, "
            f"registry says {store.max_version} — edit the registry row")
        assert store.evidence, f"{store.path}: migration rows need evidence"


def test_store_posture_partition():
    posture = store_posture()
    migration = {store.path for store in MIGRATION_STORES}
    unversioned = {store.path for store in UNVERSIONED_STORES}
    assert len(migration) == len(MIGRATION_STORES)
    assert len(unversioned) == len(UNVERSIONED_STORES)
    assert not migration & unversioned
    assert unversioned == set(posture) - migration
    assert migration - set(posture) == UNWIRED_VERSIONED_STORES
    assert set(migration_versions(REAL)) == migration
    assert all(store.reason.strip() for store in UNVERSIONED_STORES)


def test_evidence_nodes_exist():
    for owner, node_id in _all_evidence():
        rel, class_name, func = arch.parse_node_id(node_id)
        assert arch.node_exists(rel, class_name, func), (
            f"{owner}: evidence node not found: {node_id}")


def test_product_version_matches_pyproject():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == trove.__version__


# ── 检测器自测(纯函数,合成源码,不碰磁盘) ──────────────────────────────


def test_detector_module_constants_catches_and_ignores():
    sources = {
        "pkg/versioned.py": (
            "__version__ = '1.2.3'\n"
            "API_VERSION = 4\n"
            "MAX_SQL_VERSIONS = 3\n"
            "DOMAIN_VERSIONS = {'a': 'a-v1'}\n"
        ),
        "pkg/not_versions.py": (
            "PACK_SCHEMA = 1\n"
            "FLAG_VERSION = True\n"
            "dynamic_version = 2\n"
            "VERSION_FROM_ENV = os.environ['X']\n"
        ),
    }
    found = module_version_constants(sources)
    assert found == {
        ("pkg/versioned.py", "__version__"): "1.2.3",
        ("pkg/versioned.py", "API_VERSION"): 4,
        ("pkg/versioned.py", "MAX_SQL_VERSIONS"): 3,
        ("pkg/versioned.py", "DOMAIN_VERSIONS"): {"a": "a-v1"},
    }
    excluded = module_version_constants(
        sources,
        excluded=frozenset({("pkg/versioned.py", "MAX_SQL_VERSIONS")}),
    )
    assert ("pkg/versioned.py", "MAX_SQL_VERSIONS") not in excluded


def test_detector_literal_writes_catches_and_ignores():
    source = (
        "PAYLOAD = {'version': 2, 'source': 'x'}\n"
        "def build(flag):\n"
        "    literal = {'record_version': 1}\n"
        "    dynamic = {'version': flag}\n"
        "    data = {}\n"
        "    data['version'] = 3\n"
        "    return literal, dynamic, data\n"
        "class Writer:\n"
        "    def make(self):\n"
        "        return {'version': 1}\n"
    )
    found = literal_version_writes({"pkg/mod.py": source})
    assert found == {
        ("pkg/mod.py", "<module>", "version"): 2,
        ("pkg/mod.py", "build", "record_version"): 1,
        ("pkg/mod.py", "build", "version"): 3,
        ("pkg/mod.py", "Writer.make", "version"): 1,
    }


def test_detector_literal_writes_rejects_duplicate_sites():
    source = "def f():\n    a = {'version': 1}\n    b = {'version': 2}\n"
    with pytest.raises(ValueError, match="two constant version writes"):
        literal_version_writes({"pkg/mod.py": source})


def test_detector_migrations_takes_the_max_and_is_loud():
    good = (
        "from trove.storage.migrations import Migration\n"
        "MIGRATIONS = [\n"
        "    Migration(version=1, description='a', ops=[]),\n"
        "    Migration(version=2, description='b', ops=[]),\n"
        "]\n"
    )
    assert migration_versions({"pkg/m.py": good}) == {"pkg/m.py": 2}

    no_calls = "from trove.storage.migrations import Migration\n"
    with pytest.raises(ValueError, match="no parseable"):
        migration_versions({"pkg/m.py": no_calls})

    dynamic = (
        "from trove.storage.migrations import Migration\n"
        "Migration(version=VERSION, description='x', ops=[])\n"
    )
    with pytest.raises(ValueError, match="int constant"):
        migration_versions({"pkg/m.py": dynamic})


def test_detector_store_posture_partitions():
    classes = {
        "pkg.a.StoreA": "durable",
        "pkg.b.StoreB": "derived",
        "pkg.b.StoreB2": "derived",
        "pkg.c.StoreC": "protocol",
        "pkg.d.StoreD": "in_memory",
    }
    assert store_posture(classes) == {
        "pkg/a.py": "durable",
        "pkg/b.py": "derived",
    }
