"""entity_ref 派生(本体形态 E8,设计 §5.6):单维门 / 前端守卫平价 / 写一次即冻。

事件序列:提案创建 = 组被钉进 ``evidence_refs["group"]`` 的**同一个时点**;
若那一组落在一个已声明实体的键上,再钉一份
``{"type": <实体显示名>, "keys": {<键字段>: <值>}}``。之后它和 payload
一样不可改(store 的写一次白名单),也**不进**测量路径(展示与审计
元数据 —— 零改动验证见本文件末尾的 verifier 字节一致断言)。

本文件钉四层:纯函数(``entity_key`` / ``entity_ref``)、前端守卫的平价
(发出去的形状必须逐条过 ``frontend/src/utils/entityRef.ts`` 的守卫)、
服务装配(绑定 / 未绑定 / 失败 / 聚合与多维规则)、冻结(写一次)。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from trove.services.action.dispatcher import ActionDispatcher
from trove.services.action.propose import entity_key, entity_ref
from trove.services.action.service import ActionService
from trove.services.action.store import ActionStore
from trove.services.action.templates import ActionTemplateService
from trove.services.decision import outcome as oc
from trove.services.decision.rules import ActionRef, DecisionRule, Subject
from trove.services.decision.series_source import BlockSeries
from trove.services.decision.service import DecisionOutcome

ANCHOR = "2026-10-03"
PAYLOAD_TEMPLATE = ('{"rule": "{{rule_id}}", "dim": "{{dim}}", '
                    '"current": {{current}}}')
LOAN_SPEC = {"type": "贷款", "field": "loan_id"}


# ── 固件 ────────────────────────────────────────────────

def _rule(**kw):
    base = dict(
        id="revenue-drop", name="Revenue drop", severity="warning",
        priority=2, recommendation="Check the campaign calendar",
        subject=Subject(metrics=["revenue"], dimensions=["loan.region"]),
        action=ActionRef(template="notify-ops", autonomy="propose"),
    )
    base.update(kw)
    return DecisionRule(**base)


def _outcome(*, dim="north", contribution=-166.0):
    return DecisionOutcome(
        triggered=True, message="[warning] Revenue drop",
        rule_id="revenue-drop", severity="warning", error="",
        evidence={
            "rule_digest": "d1",
            "rule_rev": "rev-1",
            "times": {"anchor_date": ANCHOR,
                      "evaluated_at": f"{ANCHOR}T09:00:00"},
            "rows": [{"dim": dim, "triggered": True, "current": 1234,
                      "baseline": 1400, "delta": -166, "delta_pct": -0.1186,
                      "contribution": contribution}],
        },
    )


class _Lookup:
    """记录调用的假解析器(契约:``async (datasource, dim_ref) -> spec|None``)。"""

    def __init__(self, spec=LOAN_SPEC, error=None):
        self.spec = spec
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, datasource, dim_ref):
        self.calls.append((datasource, dim_ref))
        if self.error is not None:
            raise self.error
        return self.spec


async def _noop_transport(url, payload, headers, timeout):
    return 200, "ok"


@pytest.fixture()
async def env(tmp_path):
    store = ActionStore(tmp_path)
    templates = ActionTemplateService(tmp_path / "actions")
    templates.create({
        "name": "notify-ops", "title": "Notify ops",
        "target": {"channel": "ops-alerts"}, "risk": "medium",
        "payload_template": PAYLOAD_TEMPLATE,
    })
    templates.confirm("notify-ops")
    dispatcher = ActionDispatcher(
        {"ops-alerts": {"url": "https://hook.invalid/ops", "secret": "s3cret"}},
        transport=_noop_transport)
    service = ActionService(store, templates, dispatcher, enabled=True)
    e = SimpleNamespace(store=store, templates=templates, service=service)
    try:
        yield e
    finally:
        await store.dispose()


async def _propose(env, rule=None, outcome=None, **kw):
    p = await env.service.propose_from_verdict(
        rule=rule or _rule(), outcome=outcome or _outcome(),
        datasource="financial", **kw)
    assert p is not None
    return await env.store.get_proposal(p.id)


# ── 纯函数:entity_key(维度字段, 组值) ────────────────

def test_single_dimension_fired_row_gives_the_key():
    assert entity_key(_rule(), _outcome()) == ("loan.region", "north")


def test_aggregate_rule_has_no_entity_key():
    rule = _rule(subject=Subject(metrics=["revenue"]))
    assert entity_key(rule, _outcome(dim="")) is None


def test_multi_dimension_rule_has_no_entity_key():
    """多维组标签是 ``" / "`` 拼接值,不是任何实体的键值 —— 宁缺不猜。"""
    rule = _rule(subject=Subject(metrics=["revenue"],
                                 dimensions=["region", "product"]))
    assert entity_key(rule, _outcome(dim="north / 车贷")) is None


def test_no_fired_rows_has_no_entity_key():
    outcome = DecisionOutcome(triggered=False, message="", evidence={"rows": []})
    assert entity_key(_rule(), outcome) is None


def test_empty_or_whitespace_group_label_has_no_entity_key():
    assert entity_key(_rule(), _outcome(dim="")) is None
    assert entity_key(_rule(), _outcome(dim="   ")) is None
    assert entity_key(_rule(), _outcome(dim=None)) is None


def test_group_value_is_returned_verbatim():
    """库里的值原样带出(空格 / 斜杠 / 特殊字符不归一)—— 冻结的是当时
    读到的那一个字符串;``None``/数字先字符串化。"""
    assert entity_key(_rule(), _outcome(dim="L 456 / 北区"))[1] == "L 456 / 北区"
    assert entity_key(_rule(), _outcome(dim=2026))[1] == "2026"


# ── 纯函数:entity_ref(形状与前端守卫逐条对齐) ─────────

def test_entity_ref_shape():
    assert entity_ref("L-456", LOAN_SPEC) == {
        "type": "贷款", "keys": {"loan_id": "L-456"}}


def test_entity_ref_needs_a_complete_spec():
    assert entity_ref("L-456", None) is None
    assert entity_ref("L-456", {}) is None
    assert entity_ref("L-456", {"type": "贷款"}) is None
    assert entity_ref("L-456", {"field": "loan_id"}) is None
    assert entity_ref("L-456", {"type": "  ", "field": "loan_id"}) is None
    assert entity_ref("L-456", "not-a-dict") is None


def test_entity_ref_refuses_empty_values():
    """前端守卫会把空值键丢光、丢光就整块不渲染 —— 所以发不出空值。"""
    assert entity_ref(None, LOAN_SPEC) is None
    assert entity_ref("", LOAN_SPEC) is None
    assert entity_ref("   ", LOAN_SPEC) is None


def test_entity_ref_keeps_special_characters_verbatim():
    ref = entity_ref("L 456（北区）#A", LOAN_SPEC)
    assert ref["keys"]["loan_id"] == "L 456（北区）#A"


def test_entity_ref_trims_spec_fields_but_keeps_the_value_verbatim():
    ref = entity_ref(" L-456 ", {"type": " 贷款 ", "field": " loan_id "})
    assert ref == {"type": "贷款", "keys": {"loan_id": " L-456 "}}


# ── 前端守卫平价(frontend/src/utils/entityRef.ts::parseEntityRef) ──

def _frontend_guard(value):
    """``parseEntityRef`` 的逐条镜像 —— 后端发射必须**逐字**过它。

    镜像只做测试期的平价检查(前端已由 O3+O4 合入,不再改动):type 非空
    字符串;keys 是对象;值为 ``null``/``undefined``/``''`` 的键被丢弃,
    丢光 → null。
    """
    if value is None or not isinstance(value, dict):  # !value / 非对象 / 数组
        return None
    raw_type = value.get("type")
    type_ = raw_type.strip() if isinstance(raw_type, str) else ""
    keys_raw = value.get("keys")
    if not type_ or not isinstance(keys_raw, dict):
        return None
    keys = {k: v for k, v in keys_raw.items() if v is not None and v != ""}
    if not keys:
        return None
    return {"type": type_, "keys": keys}


@pytest.mark.parametrize("value", [
    "north", "L-456", "L 456", "L 456 / 北区", "2026", "O'Brien",
    "a\\b\"c", "北区（三）", "  空格包围  ", "x" * 120,
])
def test_emitted_ref_passes_the_frontend_guard_verbatim(value):
    ref = entity_ref(value, LOAN_SPEC)
    guarded = _frontend_guard(ref)
    assert guarded is not None
    assert guarded == {"type": "贷款", "keys": {"loan_id": value}}
    # 发射侧不变量:恰一个键、无空值(守卫丢弃空值键,丢了就是形状变脸)
    assert len(ref["keys"]) == 1
    assert all(v not in (None, "") for v in ref["keys"].values())


def test_guard_mirror_is_not_vacuous():
    """负样例:空 type / 空 keys 确实过不了守卫(证明上面的平价断言有内容)。"""
    assert _frontend_guard(None) is None
    assert _frontend_guard([]) is None
    assert _frontend_guard({"type": "", "keys": {"a": "b"}}) is None
    assert _frontend_guard({"type": "贷款", "keys": {}}) is None
    assert _frontend_guard({"type": "贷款", "keys": {"a": ""}}) is None


# ── 服务装配:绑定 / 未绑定 / 失败 / 不适用 ─────────────

async def test_declared_key_freezes_the_entity_ref(env):
    lookup = _Lookup()
    env.service.entity_lookup = lookup
    p = await _propose(env)
    assert p.evidence_refs["entity_ref"] == {
        "type": "贷款", "keys": {"loan_id": "north"}}
    # 与 group 同源:同一时点、同一次 primary_group 的答案
    assert p.evidence_refs["group"] == "north"
    assert lookup.calls == [("financial", "loan.region")]


async def test_unbound_lookup_leaves_the_key_absent(env):
    """老装配(未绑解析器)= 老路径:组/版本照钉,entity_ref 整键缺席。"""
    p = await _propose(env)
    assert "entity_ref" not in p.evidence_refs
    assert p.evidence_refs["group"] == "north"
    assert p.evidence_refs["rule_rev"] == "rev-1"


async def test_lookup_returning_none_leaves_the_key_absent(env):
    env.service.entity_lookup = _Lookup(spec=None)
    p = await _propose(env)
    assert "entity_ref" not in p.evidence_refs


async def test_lookup_failure_never_breaks_proposal_creation(env):
    """解析器抛异常 = 交付装饰件时的故障:记 warning、照旧缺席,**提案照建**。"""
    env.service.entity_lookup = _Lookup(error=RuntimeError("semantic boom"))
    p = await _propose(env)
    assert "entity_ref" not in p.evidence_refs
    assert p.evidence_refs["group"] == "north"
    assert p.status == "pending"


async def test_aggregate_rule_never_asks_the_lookup(env):
    lookup = _Lookup()
    env.service.entity_lookup = lookup
    rule = _rule(subject=Subject(metrics=["revenue"]))
    p = await _propose(env, rule=rule, outcome=_outcome(dim=""))
    assert lookup.calls == []            # 花调用之前就判了「不适用」
    assert p.evidence_refs["group"] == ""


async def test_multi_dimension_rule_never_asks_the_lookup(env):
    lookup = _Lookup()
    env.service.entity_lookup = lookup
    rule = _rule(subject=Subject(metrics=["revenue"],
                                 dimensions=["region", "product"]))
    p = await _propose(env, rule=rule, outcome=_outcome(dim="north / 车贷"))
    assert lookup.calls == []
    assert "entity_ref" not in p.evidence_refs
    assert p.evidence_refs["group"] == "north / 车贷"   # 组照钉(老行为)


async def test_caller_supplied_entity_ref_wins(env):
    """evidence_refs 的既有优先级:调用方给的键赢(与 run_id/group 同一
    把 setdefault —— 派生是缺省,不是覆盖)。"""
    env.service.entity_lookup = _Lookup()
    custom = {"type": "自定义", "keys": {"k": "v"}}
    p = await _propose(env, evidence_refs={"entity_ref": custom})
    assert p.evidence_refs["entity_ref"] == custom


async def test_params_display_override_does_not_move_the_entity_ref(env):
    """action.params 改的是消息措辞;entity_ref 与 group 一样钉的是
    **被判定/被测量的那一组**(B7 同款纪律)。"""
    env.service.entity_lookup = _Lookup()
    rule = _rule(action=ActionRef(template="notify-ops", autonomy="propose",
                                  params={"dim": "全局"}))
    p = await _propose(env, rule=rule)
    assert p.evidence_refs["group"] == "north"
    assert p.evidence_refs["entity_ref"]["keys"]["loan_id"] == "north"
    assert p.payload["dim"] == "全局"     # 措辞归措辞


async def test_entity_ref_survives_the_store_round_trip(env):
    env.service.entity_lookup = _Lookup()
    p = await _propose(env, outcome=_outcome(dim="L 456（北区）"))
    again = await env.store.get_proposal(p.id)
    assert again.evidence_refs["entity_ref"] == {
        "type": "贷款", "keys": {"loan_id": "L 456（北区）"}}


def test_assembly_binds_the_lookup_in_main():
    """装配点(main.py)真的把 ``entity_lookup`` 绑上了。

    删掉那一行(或挪出 ``create_app_components``)不会报任何错 ——
    entity_ref 只是永远缺席(「没有异常,只有永远不发生的事」,与
    ``test_wiring_parity`` 同一课)。AST 零运行时钉住:``actions`` 上的
    ``entity_lookup`` 赋值、右侧是 ``make_entity_lookup(...)`` 调用。
    """
    main_py = Path(__file__).resolve().parents[3] / "trove" / "main.py"
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    binds = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "entity_lookup"
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "actions"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "make_entity_lookup"
    ]
    assert len(binds) == 1, (
        "main.py 里 ``actions.entity_lookup = make_entity_lookup(...)`` "
        f"应有且只有一处,实际 {len(binds)} 处")


# ── 冻结:写一次,任何后续路径都改不动 ──────────────────

async def test_evidence_refs_cannot_be_updated_after_creation(env):
    """N38 的零改动验证:entity_ref 搭 group 的同一辆车 —— 写一次白名单
    不含 ``evidence_refs``,不需要(也不许有)任何新冻结机制。"""
    env.service.entity_lookup = _Lookup()
    p = await _propose(env)
    frozen = dict(p.evidence_refs)
    with pytest.raises(ValueError) as e:
        await env.store.update_proposal(
            p.id, evidence_refs={"entity_ref": {"type": "被改了", "keys": {}}})
    assert "immutable" in str(e.value)
    again = await env.store.get_proposal(p.id)
    assert again.evidence_refs == frozen
    assert again.evidence_refs["entity_ref"]["type"] == "贷款"


async def test_entity_ref_survives_a_status_transition(env):
    env.service.entity_lookup = _Lookup()
    p = await _propose(env)
    approved = await env.service.approve(p.id, user_id="admin")
    assert approved.evidence_refs["entity_ref"] == {
        "type": "贷款", "keys": {"loan_id": "north"}}


async def test_idempotent_repropose_returns_the_frozen_ref(env):
    """重跑同一次 firing 不重派生:建时冻结的那一份原样返回(解析器换了
    也不动它 —— 冻结的就是冻结的)。"""
    env.service.entity_lookup = _Lookup()
    first = await _propose(env)
    env.service.entity_lookup = _Lookup(
        spec={"type": "换过的实体", "field": "other"})
    second = await _propose(env)
    assert second.id == first.id
    assert second.evidence_refs["entity_ref"]["type"] == "贷款"


# ── 零改动验证:测量路径不看 entity_ref ─────────────────
class _VerifyDecision:
    """verifier 触达的最小 DecisionService 面(结构复用)。"""

    async def _dialect(self, datasource):
        return "sqlite"

    def _model_for(self, datasource, dialect):
        return "model"

    def _provider_for(self, datasource, dialect):
        return "provider"

    def _query_runner(self, datasource):
        return "runner"

    def _resolve_window(self, rule, anchor):
        return ("2026-09-01", "2026-09-30"), None

    async def _compile(self, rule, model, dialect, datasource, window):
        return {"time_field": "issue_date", "datasets": ["loan"]}


class _VerifyKb:
    def load_decisions(self, datasource):
        return SimpleNamespace(
            digest="d",
            rules=[DecisionRule(id="loan-drop", name="", window="上月",
                                subject=Subject(metrics=["loan_balance"]))],
        )


def _proposal_with(refs):
    return SimpleNamespace(
        id="p-1", rule_id="loan-drop", datasource="demo",
        dispatched_at="2026-09-20T09:00:00", decided_at="2026-09-19T09:00:00",
        evidence_refs=refs)


async def test_measurement_output_is_identical_with_and_without_entity_ref(
        monkeypatch):
    """entity_ref 是展示与审计元数据:verifier 不重算、不读取它,测量
    记录逐键一致(带与不带这个键,同一个字节)。"""
    blocks = [100.0, 102.0, 98.0, 101.0, 99.0, 103.0, 97.0, 101.0] + [150.0]
    series = BlockSeries(
        sql="SELECT ...", grain="month", mode="trailing",
        blocks=[(f"2026-{i + 1:02d}-01", f"2026-{i + 1:02d}-28")
                for i in range(len(blocks))],
        by_dim={"": blocks})

    async def fake_series(**kw):
        return series

    async def fake_control(**kw):
        return None

    monkeypatch.setattr(oc, "fetch_block_series", fake_series)
    monkeypatch.setattr(oc, "fetch_control_series", fake_control)
    verify = oc.make_verifier(_VerifyDecision(), kb=_VerifyKb())

    plain = await verify(_proposal_with({"rule_rev": "rev-1", "group": ""}))
    rich = await verify(_proposal_with(
        {"rule_rev": "rev-1", "group": "",
         "entity_ref": {"type": "贷款", "keys": {"loan_id": "north"}}}))

    assert "error" not in plain                      # 断言不是两个 error 相等
    assert plain["method"] == "its"
    assert rich == plain
    assert "entity_ref" not in rich                  # 测量记录不回显它
