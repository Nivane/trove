"""schema_linking 指标相关性选择 + 图链接测试(P4/P5 消费端)。

覆盖:
- metric_family 沿 metric.datasets 扩展表锚(图链接);
- "Relevant metrics" 块(带口径)替换逐 dataset 全量渲染 Metrics;
- 零锚定不被指标复活(语义优先边界,no_semantic_match 拒绝保留)。
"""

from trove.services.kb.service import KbService
from trove.workflow.nodes.schema_linking import (
    _render_semantic_context,
    _semantic_linking,
)

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
          - name: gender
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: client.gender
            datatype: String
            description: 客户性别
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
      - name: 客户数量
        description: 客户总数
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(client.client_id)
"""


class _Field:
    def __init__(self, name, role="dimension", synonyms=None, enum_display=None,
                 description=""):
        self.name = name
        self.semantic_role = role
        self.synonyms = synonyms or []
        self.is_time = role == "time"
        self.enum_display = enum_display or {}
        self.description = description


class _Dataset:
    def __init__(self, name, description="", synonyms=None, fields=None):
        self.name = name
        self.description = description
        self.synonyms = synonyms or []
        self.fields = fields or []


class _Metric:
    def __init__(self, name, expression, datasets=None, definition=""):
        self.name = name
        self.expression = expression
        self.datasets = datasets or []
        self.definition = definition


class _Model:
    def __init__(self, datasets, metrics=None, instructions=""):
        self.datasets = datasets
        self.metrics = metrics or []
        self.instructions = instructions


class _SemanticLayer:
    enabled = True

    def model(self):
        return _Model(
            [
                _Dataset("loan", description="贷款", fields=[
                    _Field("amount", description="贷款金额"),
                ]),
                _Dataset("district", description="地区", fields=[
                    _Field("A2", synonyms=["region"], description="地区名"),
                ]),
                _Dataset("client", description="客户", fields=[
                    _Field("gender", description="客户性别"),
                ]),
            ],
            metrics=[
                _Metric("平均贷款金额", "AVG(loan.amount)", datasets=["loan"],
                        definition="所有贷款的平均金额"),
                _Metric("客户数量", "COUNT(client.client_id)", datasets=["client"],
                        definition="客户总数"),
            ],
        )

    def terms_for(self, query):
        return []

    def field_hits(self, question, matched):
        return []


def _state(question, datasource="demo"):
    class _S:
        error_analysis = ""
        error = None
    s = _S()
    s.question = question
    s.datasource = datasource
    return s


async def _kb(tmp_path):
    d = tmp_path / "kb" / "demo"
    d.mkdir(parents=True)
    (d / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb")
    await kb.ensure_synced("demo")
    return kb


async def test_metric_selection_renders_relevant_metrics(tmp_path):
    kb = await _kb(tmp_path)
    state = _state("哪个地区的平均贷款金额最高")
    term_hits = await kb.search_terms("哪个地区的平均贷款金额最高", "demo")
    base = await _semantic_linking(
        state, kb, None, _SemanticLayer(), term_hits,
        "哪个地区的平均贷款金额最高", "demo")
    ctx = base["semantic_context"]
    # 相关性选择的指标(带口径)渲染,替换逐 dataset 全量 Metrics
    assert "Relevant metrics" in ctx
    assert "平均贷款金额 = AVG(loan.amount) — 所有贷款的平均金额" in ctx
    assert "Metrics:" not in ctx  # 全量渲染被替换


async def test_metric_family_expands_matched_tables(tmp_path):
    kb = await _kb(tmp_path)
    state = _state("哪个地区的平均贷款金额最高")
    term_hits = await kb.search_terms("哪个地区的平均贷款金额最高", "demo")
    base = await _semantic_linking(
        state, kb, None, _SemanticLayer(), term_hits,
        "哪个地区的平均贷款金额最高", "demo")
    # 图链接:平均贷款金额 → loan 进入 matched
    assert "loan" in base["matched_tables"]


async def test_no_match_not_resurrected_by_metric(tmp_path):
    """零 dataset 锚定 = 拒绝;metric 检索不得把拒绝复活。"""
    kb = await _kb(tmp_path)
    state = _state("今天天气怎么样")
    base = await _semantic_linking(
        state, kb, None, _SemanticLayer(), [], "今天天气怎么样", "demo")
    assert base["refusal"]["reason"] == "no_semantic_match"
    assert base["matched_tables"] == []


async def test_cjk_zero_anchor_recalled_by_metric(tmp_path):
    """中文问题零 dataset 锚定 → 指标经字符重叠召回锚回数据集。

    「贷款平均金额是多少」词元切分不到 loan(dataset 描述"贷款"纯中文、
    字段名全英文),但指标"平均贷款金额"的定义/别名经 CJK 字符 bigram
    重叠被召回,沿 metric.datasets 把 loan 拉回锚定——不再 no_semantic_match,
    也无需为每个年份新建指标。
    """
    kb = await _kb(tmp_path)
    q = "贷款平均金额是多少"
    state = _state(q)
    term_hits = await kb.search_terms(q, "demo")
    base = await _semantic_linking(
        state, kb, None, _SemanticLayer(), term_hits, q, "demo")
    assert "loan" in base["matched_tables"]
    assert base.get("refusal") is None


async def test_render_context_without_metric_hits_keeps_full_metrics():
    model = _SemanticLayer().model()
    ctx = _render_semantic_context(model, ["loan"], None, "loan", metric_hits=None)
    assert "Metrics:" in ctx  # 无选择时保留旧行为(全量锚定渲染)


# ── 官方枚举标签的链接可达性 + 字段描述渲染(C6/C3) ──


class _LongLabelLayer(_SemanticLayer):
    """loan.status 用官方文档长标签(自带逗号、多词)。"""

    def model(self):
        return _Model([
            _Dataset("loan", description="loan", fields=[
                _Field("status", role="enum", enum_display={
                    "C": "running contract, OK so far",
                    "D": "running contract, client in debt",
                }),
            ]),
        ])


class TestEnumLabelLinkReach:
    """官网长标签("running contract, OK so far")的 dataset 锚定(0485 前提)。

    现行"整串 ⊂ 问句"规则对官方长标签实测 0.0 分(问句不可能整串复述标签)
    → 补 token 级命中:标签词元与问句词元(尾 s 屈折归一)≥2 命中、且至少
    一个命中词元长 ≥5。短词双命中不构成锚定信号(噪声控制)。
    """

    def _score(self, disp, field_name="status", query="q"):
        from trove.workflow.nodes.schema_linking import (
            _semantic_dataset_score,
            _word_tokens,
        )
        # dataset 名取问句里不可能出现的词,隔离枚举锚定这一条通路
        d = _Dataset("loanrec", fields=[_Field(field_name, role="enum",
                                               enum_display={"C": disp})])
        return _semantic_dataset_score(d, query, _word_tokens(query))

    def test_long_official_label_anchors_by_tokens(self):
        s = self._score("running contract, OK so far",
                        query="How many running contracts are there in district 1?")
        assert s == 2.5

    def test_plural_folding_matches(self):
        """"contracts" 归一为 "contract" 后与标签词元命中。"""
        s = self._score("running contract, OK so far",
                        query="list loans with status of running contracts")
        assert s == 2.5

    def test_single_token_hit_not_enough(self):
        s = self._score("running contract, OK so far",
                        query="the contract of each client")
        assert s < 2.5

    def test_all_short_tokens_not_enough(self):
        """双命中但全部词元 <5 字符 → 不锚定(短词是噪声源)。"""
        s = self._score("paid by cash", query="cash paid")
        assert s < 2.5

    def test_whole_label_substring_still_wins(self):
        s = self._score("running contract", query="how many running contract loans")
        assert s == 2.5

    async def test_0485_shape_loan_enters_matched(self, tmp_path):
        """端到端:英文问句点名官方标签 → loan 进 matched_tables。

        B2 把官方标签写进 enum_display 后,链接命中规则不扩则 loan 仍进不了
        matched(实测 0.0 分)——这里钉住两者合流的可达性。
        """
        kb = await _kb(tmp_path)
        q = "How many running contracts are there in district 1?"
        state = _state(q)
        base = await _semantic_linking(
            state, kb, None, _LongLabelLayer(), [], q, "demo")
        assert "loan" in base["matched_tables"]
        assert base.get("refusal") is None


class TestFieldDescriptionRender:
    """字段 description 渲染进 planner 上下文:仅业务列(dimension/enum/
    measure),单行、截 80;identifier/time 的结构样板描述不渲染。"""

    def _ctx(self, *fields):
        model = _Model([_Dataset("district", fields=list(fields))])
        return _render_semantic_context(model, ["district"], None, "q")

    def test_dimension_desc_rendered(self):
        ctx = self._ctx(_Field("A11", role="dimension", description="average salary"))
        assert "desc=average salary" in ctx

    def test_enum_desc_rendered_and_multiline_collapsed(self):
        ctx = self._ctx(_Field("status", role="enum",
                               description="  running\n  contract  "))
        assert "desc=running contract" in ctx

    def test_identifier_desc_not_rendered(self):
        ctx = self._ctx(_Field("district_id", role="identifier",
                               description="identifier of the district"))
        assert "identifier of the district" not in ctx

    def test_long_desc_truncated(self):
        ctx = self._ctx(_Field("A11", role="dimension", description="x" * 100))
        assert "desc=" + "x" * 80 + "…" in ctx

    def test_enum_mapping_separator_semicolon(self):
        """官方标签自带逗号 → 映射分隔符用 "; ",边界不歧义。"""
        ctx = self._ctx(_Field("status", role="enum", enum_display={
            "C": "running contract, OK so far",
            "D": "running contract, client in debt",
        }))
        assert "C=running contract, OK so far; D=running contract, client in debt" in ctx
