"""typed corpus 检索测试:metrics / entities 分型检索 + 图链接(metric_family)。

覆盖 P2(确定性槽位门)/ P3(search_metrics/search_entities)/ P4(metric_family
图链接)/ P6(类型加权打分)。词法门决定"是否返回",coverage 只在门内排序;
identifier/time 结构列不做原始名子串匹配(与 schema_linking 同哲学)。
"""
import pytest

from trove.services.kb.service import KbService

SEMANTICS = """
semantic_model:
  - name: demo
    datasets:
      - name: loan
        fields:
          - name: amount
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan.amount
            datatype: Integer
            description: 贷款金额
          - name: status
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan.status
            datatype: String
            enum_display:
              A: 已结清
              B: 未结清
            description: 贷款状态
          - name: date
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: loan.date
            datatype: Date
            dimension:
              is_time: true
            description: 贷款日期
      - name: district
        fields:
          - name: A2
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: district.A2
            datatype: String
            ai_context:
              synonyms:
                - region
            description: 地区名
      - name: client
        fields:
          - name: client_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: client.client_id
            datatype: Integer
            description: 客户主键
    metrics:
      - name: 平均贷款金额
        description: 所有贷款的平均金额
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: AVG(loan.amount)
        ai_context:
          synonyms:
            - 贷款均值
            - 平均贷款
      - name: 客户数量
        description: 客户总数
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(client.client_id)
"""

ALL_TABLES = ["loan", "district", "client"]


def _mk_kb(tmp_path):
    d = tmp_path / "kb" / "demo"
    d.mkdir(parents=True)
    (d / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb")
    return kb


async def test_metrics_name_hit_ranks_first(tmp_path):
    kb = _mk_kb(tmp_path)
    await kb.ensure_synced("demo")
    hits = await kb.search_metrics("哪个地区的平均贷款金额最高", "demo", limit=5)
    assert [h.name for h in hits] == ["平均贷款金额", "客户数量"] or hits
    assert hits[0].name == "平均贷款金额"
    assert hits[0].expression == "AVG(loan.amount)"
    assert hits[0].datasets == ["loan"]


async def test_metrics_alias_hit(tmp_path):
    kb = _mk_kb(tmp_path)
    await kb.ensure_synced("demo")
    hits = await kb.search_metrics("贷款均值怎么算", "demo", limit=5)
    assert hits and hits[0].name == "平均贷款金额"


async def test_metrics_unrelated_returns_empty(tmp_path):
    kb = _mk_kb(tmp_path)
    await kb.ensure_synced("demo")
    assert await kb.search_metrics("今天天气怎么样", "demo", limit=5) == []


async def test_metrics_table_filter_drops_unmatched(tmp_path):
    kb = _mk_kb(tmp_path)
    await kb.ensure_synced("demo")
    hits = await kb.search_metrics(
        "有多少个客户", "demo", tables=["loan"], limit=5)
    # 客户数量 绑定 client,未匹配 → 丢弃
    assert [h.name for h in hits] == []


async def test_entities_synonym_and_enum_hits(tmp_path):
    kb = _mk_kb(tmp_path)
    await kb.ensure_synced("demo")
    hits = await kb.search_entities("哪个地区的客户最多", "demo", limit=10)
    names = {(e.field, e.dataset) for e in hits}
    assert ("A2", "district") in names  # region 同义词命中
    # 枚举值命中:status 的枚举值 A/B 是规范 code,中文"已结清"在描述里
    enum_hits = await kb.search_entities("贷款状态已结清", "demo", limit=10)
    assert any(e.field == "status" for e in enum_hits)


async def test_entities_ignore_structural_column_raw_name(tmp_path):
    """time 列原始名 'date' 不因子串出现在普通词里被误命中。"""
    kb = _mk_kb(tmp_path)
    await kb.ensure_synced("demo")
    hits = await kb.search_entities("update rate today", "demo", limit=10)
    assert not any(e.field == "date" for e in hits)


async def test_metric_family_expands_tables_and_entities(tmp_path):
    kb = _mk_kb(tmp_path)
    await kb.ensure_synced("demo")
    fam = await kb.metric_family(
        "哪个地区的平均贷款金额最高", "demo", matched_tables=["district"],
    )
    # 图链接:平均贷款金额(loan) 沿 metric.datasets 扩展表锚
    assert "loan" in fam["tables"]
    assert "district" in fam["tables"]
    assert [m.name for m in fam["metrics"]] == ["平均贷款金额"]
    # entities 只取指标锚定数据集里的问题相关维度
    assert all(e.dataset in ("loan", "district") for e in fam["entities"])
    assert any(e.field == "amount" for e in fam["entities"])
