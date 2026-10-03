"""提案构造纯函数:幂等键 / 主组选取 / 变量闭集 / build_proposal 冻结。

零 I/O 零 LLM —— 这里全是能用眼睛核对的输入输出,所以断言也写成
"哪个 firing 折叠成一个提案"这种可读的形式。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from trove.services.action.models import ActionTemplate
from trove.services.action.propose import (
    ProposalError,
    anchor_of,
    build_proposal,
    build_variables,
    idempotency_key,
    primary_group,
    proposal_key,
)
from trove.services.action.template_render import TEMPLATE_VARIABLES, RenderError
from trove.services.decision.rules import ActionRef, DecisionRule, Subject
from trove.services.decision.service import DecisionOutcome

ANCHOR = "2026-10-03"
NOW = datetime(2026, 10, 3, 9, 0, 0)


def _outcome(*, triggered=True, error="", rows=None, digest="d1",
             anchor=ANCHOR):
    return DecisionOutcome(
        triggered=triggered, message="[warning] Revenue drop",
        rule_id="revenue-drop", severity="warning", error=error,
        evidence={
            "rule_digest": digest,
            "times": {"anchor_date": anchor,
                      "evaluated_at": f"{anchor}T09:00:00"},
            "rows": rows if rows is not None else [
                {"dim": "north", "triggered": True, "current": 1234,
                 "baseline": 1400, "delta": -166, "delta_pct": -0.1186,
                 "contribution": -166.0},
                {"dim": "south", "triggered": True, "current": 900,
                 "baseline": 950, "delta": -50, "delta_pct": -0.0526,
                 "contribution": -50.0},
            ],
        },
    )


def _rule(**kw):
    base = dict(
        id="revenue-drop", name="Revenue drop", severity="warning",
        priority=2, recommendation="Check the campaign calendar",
        subject=Subject(metrics=["revenue"]),
        action=ActionRef(template="notify-ops", autonomy="propose"),
    )
    base.update(kw)
    return DecisionRule(**base)


def _template(**kw):
    base = dict(
        name="notify-ops", title="Notify ops", status="confirmed",
        action_type="notify", target={"channel": "ops-alerts"},
        risk="medium", payload_template='{"rule": "{{rule_id}}"}',
        digest="tpl-digest-1",
    )
    base.update(kw)
    return ActionTemplate(**base)


# ── 幂等键 ──────────────────────────────────────────────

def test_same_firing_same_key():
    a = idempotency_key("r", "d", ANCHOR, "t", {"x": 1})
    b = idempotency_key("r", "d", ANCHOR, "t", {"x": 1})
    assert a == b


def test_params_order_does_not_change_the_key():
    a = idempotency_key("r", "d", ANCHOR, "t", {"x": 1, "y": 2})
    b = idempotency_key("r", "d", ANCHOR, "t", {"y": 2, "x": 1})
    assert a == b


@pytest.mark.parametrize("field,changed", [
    ("rule_id", "other-rule"),
    ("rule_digest", "d2"),
    ("anchor_date", "2026-10-04"),
    ("template", "other-template"),
])
def test_any_identity_component_changes_the_key(field, changed):
    base = {"rule_id": "r", "rule_digest": "d", "anchor_date": ANCHOR,
            "template": "t"}
    base[field] = changed
    assert idempotency_key(**base) != idempotency_key("r", "d", ANCHOR, "t")


def test_key_ignores_the_numbers():
    """重跑时数据微变仍是同一次 firing —— 键不取数值,否则每个 tick 一
    个提案。键只由身份组件构成,这一条用规则侧对象直接验证。"""
    rule = _rule()
    a = proposal_key(rule, _outcome(rows=[{"dim": "n", "triggered": True,
                                           "current": 1, "contribution": 1.0}]),
                     "notify-ops", NOW)
    b = proposal_key(rule, _outcome(rows=[{"dim": "n", "triggered": True,
                                           "current": 999, "contribution": 9.0}]),
                     "notify-ops", NOW)
    assert a == b


def test_proposal_key_reads_params_from_the_rule():
    a = proposal_key(_rule(), _outcome(), "notify-ops", NOW)
    b = proposal_key(_rule(action=ActionRef(template="notify-ops",
                                            autonomy="propose",
                                            params={"dim": "全局"})),
                     _outcome(), "notify-ops", NOW)
    assert a != b


# ── anchor / 主组 ───────────────────────────────────────

def test_anchor_comes_from_evidence_then_falls_back_to_now():
    assert anchor_of(_outcome()) == ANCHOR
    assert anchor_of(_outcome(anchor="")) == NOW.date().isoformat()


def test_primary_group_is_the_largest_absolute_contribution():
    card = primary_group(_outcome())
    assert card["dim"] == "north"  # |−166| > |−50|


def test_primary_group_ignores_untriggered_rows():
    card = primary_group(_outcome(rows=[
        {"dim": "quiet", "triggered": False, "contribution": 999.0},
        {"dim": "loud", "triggered": True, "contribution": -5.0},
    ]))
    assert card["dim"] == "loud"


def test_primary_group_empty_when_nothing_fired_or_rows_missing():
    assert primary_group(_outcome(rows=[])) == {}
    assert primary_group(DecisionOutcome(triggered=True, message="",
                                         evidence={})) == {}
    assert primary_group(DecisionOutcome(
        triggered=True, message="", evidence={"rows": "not-a-list"})) == {}


# ── 变量闭集 ────────────────────────────────────────────

def test_build_variables_fills_every_closed_set_name():
    variables = build_variables(_rule(), _outcome(), "financial",
                               proposal_id="p-1", run_id=7, job_id="job-1")
    assert set(variables) == set(TEMPLATE_VARIABLES)
    assert variables["rule_id"] == "revenue-drop"
    assert variables["rule_name"] == "Revenue drop"
    assert variables["rule_digest"] == "d1"
    assert variables["datasource"] == "financial"
    assert variables["metric"] == "revenue"
    assert variables["dim"] == "north"
    assert variables["current"] == 1234
    assert variables["delta"] == -166
    assert variables["anchor_date"] == ANCHOR
    assert variables["proposal_id"] == "p-1"
    assert variables["run_id"] == 7
    assert variables["job_id"] == "job-1"


def test_absent_values_are_empty_strings_not_none():
    """聚合规则没有 dim、没有 run —— 空串是"确实没有",不是 ``"None"``。"""
    rule = _rule(action=ActionRef(template="notify-ops", autonomy="propose"))
    variables = build_variables(rule, _outcome(rows=[
        {"dim": "", "triggered": True, "current": 5, "contribution": 1.0},
    ]), "financial", proposal_id="p-1")
    assert variables["dim"] == ""
    assert variables["run_id"] == ""


def test_params_override_derived_values():
    rule = _rule(action=ActionRef(template="notify-ops", autonomy="propose",
                                  params={"dim": "北区", "metric": "GMV"}))
    variables = build_variables(rule, _outcome(), "financial", proposal_id="p")
    assert variables["dim"] == "北区"
    assert variables["metric"] == "GMV"


def test_unknown_param_is_refused():
    rule = _rule(action=ActionRef(template="notify-ops", autonomy="propose",
                                  params={"chanel": "ops"}))
    with pytest.raises(ProposalError) as e:
        build_variables(rule, _outcome(), "financial", proposal_id="p")
    assert "chanel" in str(e.value)


# ── build_proposal ──────────────────────────────────────

def test_build_proposal_freezes_payload_and_identity():
    p = build_proposal(
        rule=_rule(), outcome=_outcome(), datasource="financial",
        template=_template(), run_id=7, job_id="job-1", created_by="scheduler",
        ttl_hours=72, now=NOW, proposal_id="p-fixed",
    )
    assert p.id == "p-fixed"
    assert p.status == "pending"
    assert p.origin_kind == "verdict"
    assert p.action_type == "notify"
    assert p.template == "notify-ops"
    assert p.template_digest == "tpl-digest-1"
    assert p.risk == "medium"
    assert p.target == {"channel": "ops-alerts"}
    assert p.payload == {"rule": "revenue-drop"}
    assert p.rationale == "Check the campaign calendar"
    assert p.severity == "warning"
    assert p.priority == 2
    assert p.rule_digest == "d1"
    assert p.created_by == "scheduler"
    assert p.created_at == NOW.isoformat(timespec="seconds")
    assert p.expires_at == (NOW + timedelta(hours=72)).isoformat(
        timespec="seconds")
    assert p.idempotency_key == proposal_key(
        _rule(), _outcome(), "notify-ops", NOW)


def test_build_proposal_ttl_is_at_least_one_hour():
    p = build_proposal(rule=_rule(), outcome=_outcome(), datasource="ds",
                       template=_template(), ttl_hours=0, now=NOW)
    assert p.expires_at > NOW.isoformat(timespec="seconds")


def test_build_proposal_generates_an_id_when_none_given():
    a = build_proposal(rule=_rule(), outcome=_outcome(), datasource="ds",
                       template=_template(), now=NOW)
    b = build_proposal(rule=_rule(), outcome=_outcome(), datasource="ds",
                       template=_template(), now=NOW)
    assert a.id.startswith("p-") and b.id.startswith("p-")
    assert a.id != b.id


def test_build_proposal_refuses_an_unrenderable_template():
    """值落在非字符串位且不构成 JSON 字面量 → 硬错,调用方据此**不建**提案。"""
    with pytest.raises(RenderError):
        build_proposal(
            rule=_rule(), outcome=_outcome(), datasource="ds",
            template=_template(payload_template='{"n": {{message}}}'),
            now=NOW)


def test_build_proposal_raises_on_bad_param():
    rule = _rule(action=ActionRef(template="notify-ops", autonomy="propose",
                                  params={"nope": 1}))
    with pytest.raises(ProposalError):
        build_proposal(rule=rule, outcome=_outcome(), datasource="ds",
                       template=_template(), now=NOW)
