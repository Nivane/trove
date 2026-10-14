"""声明验证单测(对库证伪)。

钉住五件事:

1. **三类探测的判据**:自然键/主键唯一(含复合键)、关系「一」侧唯一
   (1:1 加验 from 侧)、fan_out=dedup 前提(DISTINCT * 后每键恰一行);
2. **NULL 语义是承重的**:SQL 唯一语义对 NULL 不去重,键含 NULL 只出
   advisory —— 两行 NULL 键绝不能被读成「重复键违例」(假阳性);
3. **四态闭集、判不了 ≠ 通过**:表缺失/方言不支持 → unverifiable,绝不
   折算成 verified;
4. **只探测声明过的东西**:未声明键/边 = 覆盖率 advisory(零查询),不
   产生对库探测;声明形态不改存量输出;
5. **对库真跑**:探测 SQL 走真实 SQLite 适配器,不打桩 —— SQL 本身
   (子查询 DISTINCT、反引号引用)就是被测对象的一部分。
"""
from __future__ import annotations

import pytest

from trove.core.types import DatasourceConfig
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.semantic_layer.ossie import parse_ossie
from trove.services.semantic_layer.verify import (
    ADVISORY,
    UNVERIFIABLE,
    VERIFIED,
    VIOLATED,
    verify_declarations,
)


@pytest.fixture
async def make_adapter():
    """真实 SQLite 适配器工厂:``ddl`` 逐句执行,用完统一关。"""
    registries = []

    async def _make(ddl: str):
        registry = ConnectorRegistry()
        adapter = await registry.register(DatasourceConfig(
            name="verify_db", type="sqlite",
            connection_params={"path": ":memory:"}))
        for stmt in ddl.split(";"):
            if stmt.strip():
                await adapter.execute(stmt)
        registries.append(registry)
        return adapter

    yield _make
    for r in registries:
        await r.close_all()


def _model(yaml_text: str):
    return parse_ossie(yaml_text)


def _kinds(probes, kind: str):
    return [p for p in probes if p.kind == kind]


def _verdicts(probes, kind: str):
    return [p.verdict for p in probes if p.kind == kind]


_KEY_YAML = """
semantic_model:
  - name: shop
    datasets:
      - name: loan
        source: loan
        primary_key: [loan_id]
        entity:
          display_name: 贷款
          natural_keys: [loan_id]
        fields:
          - name: loan_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan_id
"""


# ── 自然键 / 主键 ────────────────────────────────────────


async def test_natural_key_unique_verified(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE loan (loan_id INTEGER, amount REAL);"
        "INSERT INTO loan VALUES (1, 10.0), (2, 20.0), (3, 30.0)")
    probes = await verify_declarations(adapter, _model(_KEY_YAML), "sqlite")
    assert _verdicts(probes, "keys") == [VERIFIED]
    assert "非空 3 行" in _kinds(probes, "keys")[0].message


async def test_natural_key_duplicate_violated(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE loan (loan_id INTEGER, amount REAL);"
        "INSERT INTO loan VALUES (1, 10.0), (1, 20.0), (2, 30.0)")
    probes = await verify_declarations(adapter, _model(_KEY_YAML), "sqlite")
    hits = _kinds(probes, "keys")
    assert [p.verdict for p in hits] == [VIOLATED]
    assert "重复 1 行" in hits[0].message
    assert hits[0].target == "loan.loan_id"


async def test_null_keys_advisory_never_a_violation(make_adapter):
    """两行 NULL 键不是重复键违例 —— NULL 不去重是 SQL 语义,不是数据错。

    混口径(COUNT(*) 含 NULL、COUNT(DISTINCT) 排除 NULL)会把这条读成
    VIOLATED;探测必须在非空行上判唯一,把 NULL 单列成 advisory。
    """
    adapter = await make_adapter(
        "CREATE TABLE loan (loan_id INTEGER, amount REAL);"
        "INSERT INTO loan VALUES (1, 10.0), (2, 20.0), (NULL, 30.0), (NULL, 40.0)")
    probes = await verify_declarations(adapter, _model(_KEY_YAML), "sqlite")
    assert _verdicts(probes, "keys") == [VERIFIED, ADVISORY]
    advisory = _kinds(probes, "keys")[1]
    assert "NULL 2 行" in advisory.message


async def test_composite_natural_key_duplicate_violated(make_adapter):
    yaml_text = """
semantic_model:
  - name: shop
    datasets:
      - name: txn
        source: txn
        entity:
          natural_keys: [account_id, seq]
        fields:
          - name: account_id
            datatype: Integer
            expression:
              dialects: [{dialect: ANSI_SQL, expression: account_id}]
          - name: seq
            datatype: Integer
            expression:
              dialects: [{dialect: ANSI_SQL, expression: seq}]
"""
    adapter = await make_adapter(
        "CREATE TABLE txn (account_id INTEGER, seq INTEGER);"
        "INSERT INTO txn VALUES (1, 1), (1, 2), (1, 1)")
    probes = await verify_declarations(adapter, _model(yaml_text), "sqlite")
    assert _verdicts(probes, "keys") == [VIOLATED]  # (1,1) 出现两次


async def test_primary_key_fallback_probed_without_entity(make_adapter):
    """实体缺席但声明了主键 → 按主键探测(读侧回退口径),不是覆盖缺口。"""
    yaml_text = """
semantic_model:
  - name: shop
    datasets:
      - name: loan
        source: loan
        primary_key: [loan_id]
        fields:
          - name: loan_id
            datatype: Integer
            expression:
              dialects: [{dialect: ANSI_SQL, expression: loan_id}]
"""
    adapter = await make_adapter(
        "CREATE TABLE loan (loan_id INTEGER);"
        "INSERT INTO loan VALUES (7), (7)")
    probes = await verify_declarations(adapter, _model(yaml_text), "sqlite")
    hits = _kinds(probes, "keys")
    assert [p.verdict for p in hits] == [VIOLATED]
    assert "主键" in hits[0].message
    # 未声明实体是覆盖率的事,与键探测并存
    assert any("未声明实体" in p.message for p in _kinds(probes, "coverage"))


async def test_no_declared_key_is_coverage_not_probe(make_adapter):
    """实体在场但一个键都没声明 → 零 keys 探测,覆盖率 advisory 顶上。"""
    yaml_text = """
semantic_model:
  - name: shop
    datasets:
      - name: loan
        source: loan
        entity:
          display_name: 贷款
"""
    adapter = await make_adapter("CREATE TABLE loan (loan_id INTEGER)")
    probes = await verify_declarations(adapter, _model(yaml_text), "sqlite")
    assert _kinds(probes, "keys") == []
    assert any("未声明任何键" in p.message for p in _kinds(probes, "coverage"))


# ── 关系基数 ─────────────────────────────────────────────


_REL_YAML = """
semantic_model:
  - name: shop
    datasets:
      - name: child
        source: child
        fields:
          - name: parent_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: parent_id
      - name: parent
        source: parent
        primary_key: [pid]
        fields:
          - name: pid
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: pid
    relationships:
      - name: child_to_parent
        from: child
        to: parent
        from_columns: [parent_id]
        to_columns: [pid]
        cardinality: {card}
"""


async def test_relationship_one_side_unique_verified(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE child (parent_id INTEGER);"
        "CREATE TABLE parent (pid INTEGER);"
        "INSERT INTO parent VALUES (1), (2), (3);"
        "INSERT INTO child VALUES (1), (1), (2)")
    probes = await verify_declarations(
        adapter, _model(_REL_YAML.format(card="1:N")), "sqlite")
    rels = _kinds(probes, "relationships")
    assert [p.verdict for p in rels] == [VERIFIED]
    assert "to 侧" in rels[0].message


async def test_relationship_one_side_duplicate_violated(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE child (parent_id INTEGER);"
        "CREATE TABLE parent (pid INTEGER);"
        "INSERT INTO parent VALUES (1), (1), (2);"
        "INSERT INTO child VALUES (1), (2)")
    probes = await verify_declarations(
        adapter, _model(_REL_YAML.format(card="1:N")), "sqlite")
    rels = _kinds(probes, "relationships")
    assert [p.verdict for p in rels] == [VIOLATED]
    assert "行倍增" in rels[0].message


async def test_relationship_1_1_checks_both_sides(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE child (parent_id INTEGER);"
        "CREATE TABLE parent (pid INTEGER);"
        "INSERT INTO parent VALUES (1), (2);"
        "INSERT INTO child VALUES (1), (1)")   # from 侧重复
    # YAML 1.1 会把未加引号的 1:1 解析成六十进制整数 61 —— 声明里必须带引号
    probes = await verify_declarations(
        adapter, _model(_REL_YAML.format(card='"1:1"')), "sqlite")
    rels = _kinds(probes, "relationships")
    assert [p.verdict for p in rels] == [VERIFIED, VIOLATED]
    assert "1:1" in rels[1].message


async def test_mn_without_exemption_declares_no_uniqueness(make_adapter):
    """M:N 本就不主张「一」侧唯一 —— 数据两侧都重复也不该报违例;
    豁免(它唯一的可证伪面)没声明 → 零关系探测。"""
    adapter = await make_adapter(
        "CREATE TABLE child (parent_id INTEGER);"
        "CREATE TABLE parent (pid INTEGER);"
        "INSERT INTO parent VALUES (1), (1);"
        "INSERT INTO child VALUES (1), (1)")
    probes = await verify_declarations(
        adapter, _model(_REL_YAML.format(card="M:N")), "sqlite")
    assert _kinds(probes, "relationships") == []
    assert _kinds(probes, "fan_out") == []


# ── fan_out=dedup 前提 ───────────────────────────────────


_DEDUP_YAML = """
semantic_model:
  - name: shop
    datasets:
      - name: trans
        source: trans
        fields:
          - name: disp_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: disp_id
      - name: disp
        source: disp
        fields:
          - name: disp_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: disp_id
    relationships:
      - name: trans_disp
        from: trans
        to: disp
        from_columns: [disp_id]
        to_columns: [disp_id]
        cardinality: M:N
        fan_out: {fan_out}
"""


async def test_dedup_verified_on_exact_duplicate_rows(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE trans (disp_id INTEGER, note TEXT);"
        "INSERT INTO trans VALUES (1, 'a'), (1, 'a'), (2, 'b')")
    probes = await verify_declarations(
        adapter, _model(_DEDUP_YAML.format(fan_out="dedup")), "sqlite")
    fans = _kinds(probes, "fan_out")
    assert [p.verdict for p in fans] == [VERIFIED]
    assert "折叠 1 个精确重复" in fans[0].message


async def test_dedup_violated_when_same_key_rows_differ(make_adapter):
    """同键不同行:DISTINCT * 去不掉 —— 豁免看着放行了,行倍增照旧。"""
    adapter = await make_adapter(
        "CREATE TABLE trans (disp_id INTEGER, note TEXT);"
        "INSERT INTO trans VALUES (1, 'a'), (1, 'b')")
    probes = await verify_declarations(
        adapter, _model(_DEDUP_YAML.format(fan_out="dedup")), "sqlite")
    fans = _kinds(probes, "fan_out")
    assert [p.verdict for p in fans] == [VIOLATED]
    assert "同键不同行" in fans[0].message


async def test_dedup_without_duplicates_is_advisory(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE trans (disp_id INTEGER, note TEXT);"
        "INSERT INTO trans VALUES (1, 'a'), (2, 'b')")
    probes = await verify_declarations(
        adapter, _model(_DEDUP_YAML.format(fan_out="dedup")), "sqlite")
    fans = _kinds(probes, "fan_out")
    assert [p.verdict for p in fans] == [ADVISORY]
    assert "无实际作用" in fans[0].message


async def test_bridge_exemption_is_advisory_dead_config(make_adapter):
    """bridge 编译期未实现、仍拒 —— 看起来放行、实际什么都没放行。"""
    adapter = await make_adapter(
        "CREATE TABLE trans (disp_id INTEGER, note TEXT);"
        "CREATE TABLE disp (disp_id INTEGER)")
    probes = await verify_declarations(
        adapter, _model(_DEDUP_YAML.format(fan_out="bridge:agg")), "sqlite")
    fans = _kinds(probes, "fan_out")
    assert [p.verdict for p in fans] == [ADVISORY]
    assert "未实现" in fans[0].message


# ── 判不了 ≠ 通过 / 边界 ─────────────────────────────────


async def test_missing_table_is_unverifiable(make_adapter):
    adapter = await make_adapter("CREATE TABLE other (x INTEGER)")
    yaml_text = """
semantic_model:
  - name: shop
    datasets:
      - name: loan
        source: ghost_table
        entity:
          natural_keys: [loan_id]
"""
    probes = await verify_declarations(adapter, _model(yaml_text), "sqlite")
    hits = _kinds(probes, "keys")
    assert [p.verdict for p in hits] == [UNVERIFIABLE]
    assert "探测失败" in hits[0].message


async def test_unsupported_dialect_single_honest_skip():
    """不支持反引号引用的方言:一条 unverifiable,不是 N 条假警报。

    适配器刻意传 None —— 该路径必须在碰连接之前返回。
    """
    probes = await verify_declarations(None, _model(_KEY_YAML), "postgres")
    assert [(p.kind, p.verdict) for p in probes] == [("verify", UNVERIFIABLE)]
    assert "postgres" in probes[0].message


async def test_multi_dataset_without_relationships_is_coverage_gap(make_adapter):
    yaml_text = """
semantic_model:
  - name: shop
    datasets:
      - name: a
        source: a
        entity:
          display_name: A
      - name: b
        source: b
        entity:
          display_name: B
"""
    adapter = await make_adapter("CREATE TABLE a (x INTEGER);"
                                 "CREATE TABLE b (y INTEGER)")
    probes = await verify_declarations(adapter, _model(yaml_text), "sqlite")
    assert _kinds(probes, "keys") == []           # 无键声明 → 零探测
    cov = _kinds(probes, "coverage")
    assert cov and {p.verdict for p in cov} == {ADVISORY}
    assert any("未声明任何关系" in p.message for p in cov)
