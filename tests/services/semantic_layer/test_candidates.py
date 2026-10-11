"""候选收件箱:软 MISS → 确定性语义候选(pending,零 LLM)。

覆盖两层:
- ``candidate_specs`` 纯映射(锚定/跳过规则 —— 宁缺勿错的那一半);
- ``capture_candidates`` 在真实 KB 目录上的落库行为(幂等/上限/绝不
  自应用),并证明候选**可被确认**(payload 就是可应用的声明,不是
  诱导管理员确认出错误模型的半成品)。
"""

from __future__ import annotations

import yaml
import pytest

from trove.services.kb.service import KbService
from trove.services.semantic_layer.candidates import (
    MAX_CANDIDATES_PER_RUN,
    candidate_specs,
    capture_candidates,
)
from trove.services.semantic_layer.manage import SemanticManager
from trove.services.semantic_layer.ossie import parse_ossie


def _doc(metrics: list[dict] | None = None) -> dict:
    return {
        "version": "0.2.0.dev0",
        "semantic_model": [{
            "name": "demo",
            "datasets": [
                {
                    "name": "loan", "source": "loan",
                    "primary_key": ["loan_id"],
                    "fields": [
                        {"name": "loan_id", "datatype": "Integer",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "loan_id"}]}},
                        {"name": "amount", "datatype": "Decimal",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "amount"}]}},
                        {"name": "status", "datatype": "String",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "status"}]}},
                    ],
                },
                {
                    "name": "district", "source": "district",
                    "primary_key": ["district_id"],
                    "fields": [
                        {"name": "A3", "datatype": "String",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "A3"}]}},
                    ],
                },
            ],
            "metrics": metrics or [],
        }],
    }


def _model(metrics: list[dict] | None = None):
    return parse_ossie(yaml.safe_dump(_doc(metrics), allow_unicode=True))


def _fields(specs: list[dict]) -> list[dict]:
    """只取 field 规格:实体候选与字段候选同轮发射(E2),断言各归各。"""
    return [s for s in specs if s["kind"] == "field"]


def _entities(specs: list[dict]) -> list[dict]:
    return [s for s in specs if s["kind"] == "entity"]


_LOAN_ENTITY_SPEC = {
    "kind": "entity", "name": "loan",
    "payload": {"dataset": "loan", "natural_keys": ["loan_id"]},
    "reason": "entity_naming",
    "evidence": "loan natural_keys=loan_id",
}


@pytest.fixture
def kb(tmp_path):
    k = KbService(tmp_path / "proj")
    ds_dir = k.kb_dir / "demo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "semantics.yml").write_text(
        yaml.safe_dump(_doc(), allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return k


# ── 纯映射 ──────────────────────────────────────────────


class TestCandidateSpecs:
    def test_metric_from_qualified_aggregate(self):
        specs = candidate_specs(
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
            _model())
        assert specs == [{
            "kind": "metric", "name": "avg_amount",
            "payload": {"expression": "AVG(loan.amount)", "datasets": ["loan"]},
            "reason": "no_metric_match",
        }]

    def test_metric_skips_unanchorable_forms(self):
        model = _model()
        for component in (
            "SUM(amount)",                     # 裸列:锚不住数据集
            "SUM(ghost.amount)",               # 未声明数据集
            "SUM(loan.amount + district.A3)",  # 多数据集聚合
            "SUM(loan.amount) / COUNT(loan.loan_id)",  # 多聚合算式
            "COUNT(*)",                        # 无列
            "平均贷款金额",                      # 非 SQL
        ):
            assert candidate_specs(
                [{"reason": "no_metric_match", "component": component}],
                model) == [], component

    def test_metric_skips_taken_name(self):
        """推导名已声明 → 跳过:候选绝不诱导覆盖既有声明。"""
        model = _model(metrics=[{
            "name": "avg_amount",
            "expression": {"dialects": [{
                "dialect": "ANSI_SQL", "expression": "AVG(loan.amount)"}]},
        }])
        assert candidate_specs(
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
            model) == []

    def test_field_from_dotted_ref(self):
        specs = candidate_specs(
            [{"reason": "unresolved_filter_field", "component": "loan.grade"}],
            _model())
        assert specs == [{
            "kind": "field", "name": "loan.grade",
            "payload": {"expression": "loan.grade"},
            "reason": "unresolved_filter_field",
        }, _LOAN_ENTITY_SPEC]

    def test_time_field_reason_marks_is_time(self):
        """时间分桶的 field 解析失败 → 候选带上 is_time(按时间轴起草)。"""
        specs = candidate_specs(
            [{"reason": "time_field_not_declared",
              "component": "loan.issue_date"}],
            _model())
        assert specs[0]["payload"] == {
            "expression": "loan.issue_date", "is_time": True}

    def test_field_skips_declared_undeclared_and_unanchorable(self):
        """字段候选只在锚得住且未声明时发射;锚不住的输入连实体候选都没有
        (实体候选信任同一个锚,见 _entity_spec)。"""
        model = _model()
        for component in (
            "ghost.x",         # 数据集未声明
            "amount",          # 裸列:锚不住
            "loan.amount * 2",  # 非列引用
        ):
            assert candidate_specs(
                [{"reason": "unresolved_filter_field", "component": component}],
                model) == [], component
        # 已声明字段 → field 候选跳过(缺口另有原因);数据集还没实体声明
        # → 实体候选照发射(对象命名与字段声明是两件事)
        specs = candidate_specs(
            [{"reason": "unresolved_filter_field", "component": "loan.status"}],
            model)
        assert [s["kind"] for s in specs] == ["entity"]

    def test_non_candidate_reasons_and_empty_components_ignored(self):
        model = _model()
        # 两键枚举缺口(无 value 第三键)不产生候选,且**连实体候选也不发射**
        # —— 老形状输入 → 候选集与今天逐字节相同(值类候选另有
        # TestValueCandidates,证据门控)。
        for miss in (
            {"reason": "enum_value_unresolved", "component": "loan.status"},
            {"reason": "invalid_op", "component": "="},
            {"reason": "having_without_aggregation", "component": ""},
            {"reason": "no_metric_match", "component": ""},
        ):
            assert candidate_specs([miss], model) == [], miss

    def test_metric_priority_and_dedupe(self):
        specs = candidate_specs([
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
            {"reason": "no_metric_match", "component": "AVG(loan.amount)"},
            {"reason": "unresolved_answer_column", "component": "loan.grade"},
        ], _model())
        # metric > field > entity(对象声明是元一级的,单轮上限下让位)
        assert [s["name"] for s in specs] == ["avg_amount", "loan.grade", "loan"]
        assert [s["kind"] for s in specs] == ["metric", "field", "entity"]

    def test_no_model_yields_no_specs(self):
        assert candidate_specs(
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
            None) == []


def _model_with_values(values: list[str] | None, aliases: dict | None = None):
    """status 字段带实测取值(probe 结构事实)+ 可选既有值别名。"""
    doc = _doc()
    status = doc["semantic_model"][0]["datasets"][0]["fields"][2]
    if values is not None:
        status["values"] = values
    if aliases is not None:
        status.setdefault("ai_context", {})["value_aliases"] = aliases
    return parse_ossie(yaml.safe_dump(doc, allow_unicode=True))


class TestValueCandidates:
    """值类缺口候选(enum_value_unresolved + 第三键 value):证据门控。

    唯一可以落候选的情形 = 提问字面量与**实测取值**大小写无关地相等
    (库里确实这么存)。翻译缺口(男性/月结 → M/monthly)机器定不了目标码
    ——自映射会把错字面量冻结进骨架,聚合题的 0 行过滤没守卫兜底 → 不猜。
    """

    def test_value_hit_lands_alias_candidate(self):
        specs = candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "Z"}],
            _model_with_values(["A", "B", "Z"]))
        assert specs[0] == {
            "kind": "field", "name": "loan.status",
            "payload": {"expression": "status", "value_aliases": {"Z": ["Z"]}},
            "reason": "enum_value_unresolved",
            "evidence": "loan.status=Z",
        }
        # 值类缺口是字段形状的缺口 → 同轮也发射该数据集的对象命名候选
        assert _entities(specs) == [_LOAN_ENTITY_SPEC]

    def test_value_match_is_case_insensitive_stored_spelling_wins(self):
        specs = candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": " z "}],
            _model_with_values(["A", "Z"]))
        assert specs[0]["payload"]["value_aliases"] == {"Z": ["Z"]}

    def test_value_misses_without_probe_evidence_skipped(self):
        # 值不在实测取值里(翻译缺口)= 主导失败模式:不产生 field 候选
        assert _fields(candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "男性"}],
            _model_with_values(["M", "F"]))) == []
        # 未探测(values 缺席)= 没有证据:同样不猜
        assert _fields(candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "Z"}],
            _model_with_values(None))) == []

    def test_value_merges_existing_aliases_not_replaces(self):
        """payload 整体替换 —— 既有别名必须全量并入,否则确认即丢失。"""
        specs = candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "Z"}],
            _model_with_values(["A", "Z"], aliases={"A": ["active"]}))
        assert specs[0]["payload"]["value_aliases"] == {
            "A": ["active"], "Z": ["Z"]}

    def test_value_alias_deduped_when_already_declared(self):
        specs = candidate_specs(
            [{"reason": "enum_value_unresolved", "component": "loan.status",
              "value": "z"}],
            _model_with_values(["Z"], aliases={"Z": ["z"]}))
        assert specs[0]["payload"]["value_aliases"] == {"Z": ["z"]}

    def test_value_junk_is_never_echoed(self):
        """提问字面量当不可信输入:只有命中实测取值的输入才进 payload,
        且落进去的是**探测到的存储写法**(probe 已净化),不是用户原文。
        实体候选的 payload 只由模型事实构成(dataset/pk/描述)→ junk 落
        不进任何候选。"""
        for junk in ("Z\n- evil: yaml", "Z'", "Z" * 500, "", "  "):
            specs = candidate_specs(
                [{"reason": "enum_value_unresolved", "component": "loan.status",
                  "value": junk}],
                _model_with_values(["A", "B"]))
            assert _fields(specs) == [], repr(junk)
            if junk.strip():
                # 空白字面量的「不出现」由上面那条断言表达 —— 空串在任意
                # 文本里都成立,做包含断言没有信息量。
                assert junk not in yaml.safe_dump(specs, allow_unicode=True), repr(junk)

    def test_value_spec_deduped_against_field_gap_same_name(self):
        """同一字段的声明缺口与值缺口同轮出现 → (kind, name) 去重只留一条;
        实体候选同轮只发射一次。"""
        specs = candidate_specs([
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
            {"reason": "enum_value_unresolved", "component": "loan.status",
             "value": "Z"},
        ], _model_with_values(["Z"]))
        assert [s["name"] for s in specs] == ["loan.grade", "loan.status", "loan"]
        assert len(_entities(specs)) == 1


class TestEntitySpec:
    """实体命名候选(E2):与 field 候选信任同一个锚,payload 只写模型事实。

    ``_entity_spec`` 的原料只有三样:数据集名、``primary_key``(自然键)、
    数据集描述(显示名,与 kb init 后置起草共用 ``entity_display_name``)。
    schema notes / terms 是 init 现场的原料,捕获路径拿不到也不猜 —— 那份
    更富的版本由 ``init_pipeline._draft_entity_naming`` 负责,两个生产者以
    (kind, name) 幂等收口。
    """

    def test_emitted_for_field_shaped_misses(self):
        specs = candidate_specs(
            [{"reason": "unresolved_filter_field", "component": "loan.grade"}],
            _model())
        assert _entities(specs) == [_LOAN_ENTITY_SPEC]

    def test_display_name_from_cjk_dataset_description(self):
        """中文描述 → 显示名(复用 business_label 的标题词规则)。"""
        doc = _doc()
        doc["semantic_model"][0]["datasets"][0]["description"] = "贷款信息表"
        specs = candidate_specs(
            [{"reason": "unresolved_filter_field", "component": "loan.grade"}],
            parse_ossie(yaml.safe_dump(doc, allow_unicode=True)))
        assert _entities(specs)[0]["payload"]["display_name"] == "贷款"

    def test_english_prose_description_yields_no_display_name(self):
        """英文散文首词是坏名字(「Bank」)→ 宁可不给:英文库表名本身就是
        业务词,读侧回退(显示名缺省 → 数据集名)已经正确。"""
        doc = _doc()
        doc["semantic_model"][0]["datasets"][0]["description"] = (
            "Bank accounts with statement issuance frequency")
        specs = candidate_specs(
            [{"reason": "unresolved_filter_field", "component": "loan.grade"}],
            parse_ossie(yaml.safe_dump(doc, allow_unicode=True)))
        assert "display_name" not in _entities(specs)[0]["payload"]

    def test_skipped_when_entity_already_declared(self):
        doc = _doc()
        doc["semantic_model"][0]["datasets"][0]["entity"] = {"display_name": "贷款"}
        specs = candidate_specs(
            [{"reason": "unresolved_filter_field", "component": "loan.grade"}],
            parse_ossie(yaml.safe_dump(doc, allow_unicode=True)))
        assert _entities(specs) == []

    def test_skipped_when_nothing_can_be_declared(self):
        """主键空且推不出显示名 → 不起草:一条确认了也什么都没声明的
        草稿是纯噪声(entity: {} 在解析器里等于未声明)。"""
        doc = _doc()
        doc["semantic_model"][0]["datasets"] = [{
            "name": "raw_events", "source": "raw_events", "primary_key": [],
            "fields": [{"name": "tag", "datatype": "String",
                        "expression": {"dialects": [{
                            "dialect": "ANSI_SQL", "expression": "tag"}]}}],
        }]
        assert candidate_specs(
            [{"reason": "unresolved_filter_field",
              "component": "raw_events.tag"}],
            parse_ossie(yaml.safe_dump(doc, allow_unicode=True))) == []

    def test_deduped_across_misses_of_the_same_dataset(self):
        specs = candidate_specs([
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
            {"reason": "unresolved_answer_column", "component": "loan.status"},
        ], _model())
        assert len(_entities(specs)) == 1

    def test_not_emitted_for_metric_only_misses(self):
        """指标形状的缺口锚在聚合表达式上,不携带「用到了哪张表」的确定性
        事实 → 不发射实体候选。"""
        specs = candidate_specs(
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
            _model())
        assert _entities(specs) == []

    def test_not_emitted_for_unresolvable_anchor(self):
        for component in ("ghost.x", "amount", "loan.status = 'A'"):
            specs = candidate_specs(
                [{"reason": "unresolved_filter_field", "component": component}],
                _model())
            assert _entities(specs) == [], component


# ── 落库(真实 KB 目录) ──────────────────────────────────


class TestCaptureCandidates:
    async def test_capture_creates_pending_only_and_keeps_semantics_intact(self, kb):
        before = (kb.kb_dir / "demo" / "semantics.yml").read_text(encoding="utf-8")
        created = await capture_candidates(kb, "demo", "哪个地区贷款金额最高?", [
            {"reason": "no_metric_match", "component": "AVG(loan.amount)"},
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
        ])
        assert {c["kind"] for c in created} == {"metric", "field", "entity"}
        pending = SemanticManager(kb).drafts("demo")["pending"]
        assert len(pending) == 3
        assert pending[0]["note"] == "auto:no_metric_match:哪个地区贷款金额最高?"
        entity_draft = next(d for d in pending if d["kind"] == "entity")
        assert entity_draft["name"] == "loan"
        assert entity_draft["payload"] == {
            "dataset": "loan", "natural_keys": ["loan_id"]}
        assert entity_draft["note"].startswith(
            "auto:entity_naming:loan natural_keys=loan_id:")
        assert all(d["status"] == "pending" for d in pending)
        # 候选绝不自动应用:semantics.yml 逐字节未动
        assert (kb.kb_dir / "demo" / "semantics.yml").read_text(
            encoding="utf-8") == before

    async def test_capture_dedupes_against_pending_queue(self, kb):
        misses = [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}]
        first = await capture_candidates(kb, "demo", "q", misses)
        assert len(first) == 1
        # 同题重跑:create_draft 自身无去重,幂等闸门在这里
        assert await capture_candidates(kb, "demo", "q", misses) == []
        assert len(SemanticManager(kb).drafts("demo")["pending"]) == 1

    async def test_capture_caps_per_run(self, kb):
        misses = [
            {"reason": "unresolved_filter_field", "component": f"loan.f{i}"}
            for i in range(5)
        ]
        created = await capture_candidates(kb, "demo", "q", misses)
        assert len(created) == MAX_CANDIDATES_PER_RUN
        # 实体候选排在最后:单轮上限下让位给能直接解锁本题的字段声明
        assert [c["kind"] for c in created] == ["field", "field", "field"]

    async def test_entity_capture_dedupes_across_all_statuses(self, kb):
        """实体候选全状态去重:拒绝过的命名不再回灌(与 metric/field 的
        pending-only 刻意不对称 —— 实体命名是「定过一次就不用再问」的
        元一级声明,且拒绝是一种决定)。"""
        misses = [{"reason": "unresolved_filter_field", "component": "loan.grade"}]
        created = await capture_candidates(kb, "demo", "q", misses)
        assert [c["kind"] for c in created] == ["field", "entity"]
        manager = SemanticManager(kb)
        for c in created:
            await manager.reject_draft("demo", c["id"])

        # 字段候选:拒绝后仍可再捕获(pending-only,与老行为逐字节一致)
        second = await capture_candidates(kb, "demo", "q", misses)
        assert [c["kind"] for c in second] == ["field"]
        # 实体候选不再回灌:再拒再捕,字段照旧、实体始终缺席
        await manager.reject_draft("demo", second[0]["id"])
        third = await capture_candidates(kb, "demo", "q", misses)
        assert [c["kind"] for c in third] == ["field"]

    async def test_entity_capture_skips_already_declared_entity(self, kb):
        """已确认(模型里看得见)的实体声明不再起草 —— 第二次捕获 0 条。"""
        created = await capture_candidates(kb, "demo", "q", [
            {"reason": "unresolved_filter_field", "component": "loan.grade"}])
        manager = SemanticManager(kb)
        for entry in created:
            await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert manager.model("demo") is not None
        loan = next(d for d in manager.model("demo").datasets if d.name == "loan")
        assert loan.entity is not None
        again = await capture_candidates(kb, "demo", "q", [
            {"reason": "unresolved_filter_field", "component": "loan.status2"}])
        assert [c["kind"] for c in again] == ["field"]

    async def test_metric_only_miss_creates_no_entity_draft(self, kb):
        created = await capture_candidates(kb, "demo", "q", [
            {"reason": "no_metric_match", "component": "AVG(loan.amount)"}])
        assert [c["kind"] for c in created] == ["metric"]

    async def test_capture_without_model_or_kb_is_noop(self, kb, tmp_path):
        bare = KbService(tmp_path / "bare")
        assert await capture_candidates(
            bare, "demo", "q",
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}]) == []
        assert await capture_candidates(
            None, "demo", "q",
            [{"reason": "no_metric_match", "component": "AVG(loan.amount)"}]) == []

    async def test_candidate_is_confirmable(self, kb):
        """候选 payload 必须是**可应用的正确声明** —— 确认后模型真的长出来。"""
        created = await capture_candidates(kb, "demo", "q", [
            {"reason": "no_metric_match", "component": "AVG(loan.amount)"},
            {"reason": "unresolved_filter_field", "component": "loan.grade"},
        ])
        manager = SemanticManager(kb)
        for entry in created:
            await manager.confirm_draft("demo", entry["id"], dialect="sqlite")

        model = manager.model("demo")
        assert "avg_amount" in {m.name for m in model.metrics}
        loan = next(d for d in model.datasets if d.name == "loan")
        assert "grade" in {f.name for f in loan.fields}
        # 实体候选同样可确认:确认后对象声明在模型里可见
        assert loan.entity is not None
        assert loan.entity.natural_keys == ["loan_id"]
        assert manager.drafts("demo")["pending"] == []


def test_read_faces_have_no_draft_queue_entry():
    """未确认声明对所有读面不可见 —— 机制断言(与上面的功能断言互补)。

    功能面(``test_capture_creates_pending_only...`` 的逐字节对比、
    ``test_pending_entity_draft_is_invisible_to_projection`` 的投影对比)
    证明的是「现在没漏」;这条钉住的是「漏不进来」:读面(refuse 的冲突
    检测 / schema linking / 对象投影 / 语义 provider)只经 semantics.yml
    的解析模型,谁把草稿队列(``.drafts(``)接进读面 —— 比如让投影显示
    未确认对象 —— 先红在这里。
    """
    import ast
    from pathlib import Path

    import trove.services.semantic_layer.object_graph as object_graph
    import trove.services.semantic_layer.provider as provider
    import trove.workflow.nodes.refuse as refuse
    import trove.workflow.nodes.schema_linking as schema_linking

    for mod in (object_graph, provider, schema_linking, refuse):
        source = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "drafts", f"{mod.__name__}:{node.lineno}"
