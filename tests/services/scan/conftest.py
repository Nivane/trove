"""扫描包测试的共享夹具:真实(内存)数据源 + 一份最小语义模型。

窗口锚定在 2026-09(与决策测试同一套罐头数字风格):历史块 2026-05..08,
当期 2026-09。华东当期飙到 500(超带),华北平稳(判过、在带内)。

一个刻意的细节:两个历史序列都**不是常数**(100/110/90/100 与
50/52/48/50)—— 常数序列的 MAD 为 0,噪声带 degraded,``outside``
返回 None(判不了),那条路径由专门的用例覆盖,不混在这里。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from trove.core.types import DatasourceConfig
from trove.services.kb.service import KbService

NOW = datetime(2026, 9, 27, 10, 0, 0)
TODAY = date(2026, 9, 27)

#: 与 ``trove/services/scan/service.py`` 的 provider 构造同源:语义模型
#: 写在 KB 的 semantics.yml 里,provider 直接读它。
SEMANTICS = """
semantic_model:
  - name: demo
    datasets:
      - name: loan
        source: loan
        description: "loan records; columns: region, amount, date"
        fields:
          - name: region
            expression:
              dialects: [{dialect: sqlite, expression: loan.region}]
            datatype: TEXT
            semantic_role: dimension
          - name: amount
            expression:
              dialects: [{dialect: sqlite, expression: loan.amount}]
            datatype: DOUBLE
            semantic_role: measure
          - name: date
            expression:
              dialects: [{dialect: sqlite, expression: loan.date}]
            datatype: DATE
            semantic_role: time
    metrics:
      - name: loan_balance
        expression:
          dialects: [{dialect: sqlite, expression: SUM(loan.amount)}]
        agg_time_dimension: loan.date
      - name: loan_count
        expression:
          dialects: [{dialect: sqlite, expression: COUNT(loan.amount)}]
        agg_time_dimension: loan.date
"""

ROWS = """
INSERT INTO loan (region, amount, date) VALUES
  ('华东', 100, '2026-05-10'), ('华东', 110, '2026-06-10'),
  ('华东',  90, '2026-07-10'), ('华东', 100, '2026-08-10'),
  ('华东', 500, '2026-09-10'),
  ('华北',  50, '2026-05-10'), ('华北',  52, '2026-06-10'),
  ('华北',  48, '2026-07-10'), ('华北',  50, '2026-08-10'),
  ('华北',  52, '2026-09-10')
"""


@pytest.fixture
def kb(tmp_path):
    """KB 里放一份 demo 的语义模型(provider 与 API 写时校验都读它)。"""
    service = KbService(tmp_path / "proj")
    (service.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
    service.semantics_path("demo").write_text(SEMANTICS, encoding="utf-8")
    return service


@pytest.fixture
async def registry():
    from trove.services.datasource.registry import ConnectorRegistry

    reg = ConnectorRegistry()
    config = DatasourceConfig(
        name="demo", type="sqlite",
        connection_params={"path": ":memory:"}, default=True,
    )
    adapter = await reg.register(config, set_default=True)
    await adapter.execute("CREATE TABLE loan (region TEXT, amount REAL, date DATE)")
    await adapter.execute(ROWS)
    yield reg
    await reg.close_all()


@pytest.fixture
def runner(registry):
    """只读一跳(与图内 ``_run_hop`` 同形状):(sql, datasource) → (列, 行)。"""
    async def _run(sql: str, datasource: str):
        result = await registry.execute(sql, datasource or None)
        return list(result.columns), list(result.rows)

    return _run


@pytest.fixture
def dialect_of():
    async def _dialect(datasource: str) -> str:
        return "sqlite"

    return _dialect
