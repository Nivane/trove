"""L3 活库漂移检测 —— 声明的内容是否仍然为真。

钉住五件事:

1. **值快照方向唯一**:声明的值从活库消失 = 漂移;活库多出值 = 快照陈旧,
   不报(把「多出来」也报成漂移会让每次加新客户都变红);
2. **三种等价都窄**:精确 / 数值 / 大小写;绝不做相似度或翻译;
3. **判不了 ≠ 通过**:非裸列表达式、域超 cap、方言不支持都进
   ``unverified``,不产条目;
4. **关系可加复用 verify**:键重复 / 「一」侧不唯一 / ``fan_out: dedup``
   前提不成立 → critical 条目;
5. **真跑 SQL**:用真实 SQLite 适配器,不打桩 —— 探测语句本身是被测对象。
"""
from __future__ import annotations

import pytest

from trove.core.types import DatasourceConfig
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.semantic_layer.live_drift import detect_live_drift
from trove.services.semantic_layer.ossie import parse_ossie


@pytest.fixture
async def make_adapter():
    """真实 SQLite 适配器工厂:``ddl`` 逐句执行,用完统一关。"""
    registries = []

    async def _make(ddl: str):
        registry = ConnectorRegistry()
        adapter = await registry.register(DatasourceConfig(
            name="live_drift_db", type="sqlite",
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


def _values_model(field_expr: str = "status", values=("A", "B")) -> str:
    vals = "".join(f"\n        - '{v}'" for v in values)
    return f"""
version: "0.2.0.dev0"
name: shop
datasets:
  - name: loan
    source: loan
    fields:
      - name: status
        datatype: String
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: {field_expr}
        values:{vals}
"""


async def _run(adapter, yaml_text: str, dialect: str = "sqlite") -> dict:
    return await detect_live_drift(adapter, _model(yaml_text), dialect)


# ── 取值可锚定 ──────────────────────────────────────────


async def test_all_declared_values_present_is_clean(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE loan (status TEXT);"
        "INSERT INTO loan VALUES ('A'), ('B'), ('C')")
    report = await _run(adapter, _values_model())

    assert report["status"] == "ok"
    assert report["items"] == []
    assert report["unverified"] == []


async def test_missing_declared_value_is_drift(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE loan (status TEXT);"
        "INSERT INTO loan VALUES ('A'), ('C')")
    report = await _run(adapter, _values_model())

    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["level"] == "L3"
    assert item["kind"] == "value_missing"
    assert item["severity"] == "warning"
    assert item["subject"] == "loan.status"
    assert item["detail"]["missing"] == ["B"]
    assert item["detail"]["missing_count"] == 1
    assert item["detail"]["declared"] == 2
    assert item["detail"]["live_distinct"] == 2


async def test_extra_live_values_are_not_drift(make_adapter):
    """快照陈旧不是漂移:多出来的值只说明该重新探测,不说明声明为假。"""
    adapter = await make_adapter(
        "CREATE TABLE loan (status TEXT);"
        "INSERT INTO loan VALUES ('A'), ('B'), ('NEW')")
    report = await _run(adapter, _values_model())

    assert report["items"] == []


async def test_numeric_string_matches_integer_column(make_adapter):
    """``values`` 是文本快照,INTEGER 列读回来是 int —— 不能因此判缺失。"""
    adapter = await make_adapter(
        "CREATE TABLE loan (status INTEGER);"
        "INSERT INTO loan VALUES (10), (11)")
    report = await _run(adapter, _values_model(values=("10", "11")))

    assert report["items"] == []
    assert report["unverified"] == []


async def test_case_difference_matches(make_adapter):
    """MySQL 的 CI 排序规则下 'A' 与 'a' 是同一个值。"""
    adapter = await make_adapter(
        "CREATE TABLE loan (status TEXT);"
        "INSERT INTO loan VALUES ('a'), ('b')")
    report = await _run(adapter, _values_model(values=("A", "B")))

    assert report["items"] == []


async def test_non_bare_expression_is_unverified(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE loan (status TEXT);"
        "INSERT INTO loan VALUES ('A')")
    report = await _run(adapter, _values_model(field_expr="UPPER(status)"))

    assert report["items"] == []
    assert len(report["unverified"]) == 1
    claim = report["unverified"][0]
    assert claim["subject"] == "loan.status"
    assert claim["reason"].startswith("field_expression_not_physical")


async def test_probe_failure_is_unverified_not_clean(make_adapter):
    """列不存在(SQL 探不动)→ unverified;不能读成「声明值都还在」。"""
    adapter = await make_adapter(
        "CREATE TABLE loan (status TEXT);"
        "INSERT INTO loan VALUES ('A')")
    report = await _run(adapter, _values_model(field_expr="nope"))

    assert report["items"] == []
    assert len(report["unverified"]) == 1
    assert report["unverified"][0]["reason"].startswith("value_probe_failed")


async def test_domain_too_large_is_unverified(make_adapter):
    """活值超过 cap 就不猜 —— 「域太宽」不能被读成「声明值还在」。"""
    rows = ", ".join(f"('v{i}')" for i in range(101))
    adapter = await make_adapter(
        f"CREATE TABLE loan (status TEXT); INSERT INTO loan VALUES {rows}")
    report = await _run(adapter, _values_model(values=("v0",)))

    assert report["items"] == []
    assert len(report["unverified"]) == 1
    assert report["unverified"][0]["reason"].startswith("value_domain_too_large")


# ── 关系可加(复用 verify)───────────────────────────────


_KEY_MODEL = """
version: "0.2.0.dev0"
name: shop
datasets:
  - name: loan
    source: loan
    primary_key: [loan_id]
    fields:
      - name: loan_id
        datatype: Integer
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: loan_id
"""


async def test_duplicate_key_is_critical_drift(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE loan (loan_id INTEGER, amount REAL);"
        "INSERT INTO loan VALUES (1, 10.0), (1, 20.0)")
    report = await _run(adapter, _KEY_MODEL)

    hits = [i for i in report["items"] if i["kind"] == "key_not_unique"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "critical"
    assert hits[0]["detail"]["probe"] == "keys"
    assert hits[0]["detail"]["target"] == "loan.loan_id"


_REL_MODEL = """
version: "0.2.0.dev0"
name: shop
datasets:
  - name: loan
    source: loan
    fields:
      - name: customer_id
        datatype: Integer
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: customer_id
  - name: customer
    source: customer
    fields:
      - name: customer_id
        datatype: Integer
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: customer_id
relationships:
  - name: loan_to_customer
    from: loan
    to: customer
    from_columns: [customer_id]
    to_columns: [customer_id]
    cardinality: 1:N
"""


async def test_relationship_one_side_not_unique(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE loan (customer_id INTEGER);"
        "INSERT INTO loan VALUES (1), (2);"
        "CREATE TABLE customer (customer_id INTEGER);"
        "INSERT INTO customer VALUES (1), (1)")
    report = await _run(adapter, _REL_MODEL)

    hits = [i for i in report["items"]
            if i["kind"] == "relationship_not_additive"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "critical"
    assert hits[0]["subject"] == "loan_to_customer"
    assert hits[0]["detail"]["probe"] == "relationships"


_FANOUT_MODEL = """
version: "0.2.0.dev0"
name: shop
datasets:
  - name: account
    source: account
    fields:
      - name: account_id
        datatype: Integer
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: account_id
      - name: client_id
        datatype: Integer
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: client_id
  - name: client
    source: client
    fields:
      - name: client_id
        datatype: Integer
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: client_id
relationships:
  - name: account_owners
    from: account
    to: client
    from_columns: [account_id]
    to_columns: [client_id]
    cardinality: M:N
    fan_out: dedup
"""


async def test_fan_out_dedup_precondition_broken(make_adapter):
    """同键但非整行相同的多行 —— ``DISTINCT *`` 去不掉,豁免不成立。"""
    adapter = await make_adapter(
        "CREATE TABLE account (account_id INTEGER, client_id INTEGER);"
        "INSERT INTO account VALUES (1, 10), (1, 20);"
        "CREATE TABLE client (client_id INTEGER);"
        "INSERT INTO client VALUES (10), (20)")
    report = await _run(adapter, _FANOUT_MODEL)

    hits = [i for i in report["items"]
            if i["kind"] == "fan_out_not_deduplicable"]
    assert len(hits) == 1
    assert hits[0]["severity"] == "critical"
    assert hits[0]["subject"] == "account_owners"
    assert hits[0]["detail"]["probe"] == "fan_out"


# ── 方言边界 ────────────────────────────────────────────


async def test_unsupported_dialect_is_one_honest_unverified(make_adapter):
    """不支持引用的方言给**一条**诚实跳过,而不是逐条刷屏或假装干净。"""
    adapter = await make_adapter(
        "CREATE TABLE loan (loan_id INTEGER, status TEXT);"
        "INSERT INTO loan VALUES (1, 'A')")
    report = await _run(adapter, _KEY_MODEL, dialect="oracle")

    assert report["items"] == []
    assert len(report["unverified"]) == 1
    claim = report["unverified"][0]
    assert claim["level"] == "L3"
    assert "标识符引用" in claim["reason"]
    assert "oracle" in claim["reason"]
