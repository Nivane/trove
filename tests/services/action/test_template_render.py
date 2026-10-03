"""闭集渲染器:白名单命中 / 未知变量响亮报错 / JSON 逃逸 / 注入串不生效。

渲染器的安全是**构造性**的(与 ``decision/expr.py`` 同一纪律):没有表达式
语言、没有 Jinja、没有 eval;值一律先做 JSON 内层逃逸再落进模板。这些测试
钉的就是"外送消息静默空值 / 被值改写结构"这两件最坏的事不会发生。
"""

from __future__ import annotations

import json

import pytest

from trove.services.action.template_render import (
    SAMPLE_VARIABLES,
    TEMPLATE_VARIABLES,
    RenderError,
    render_payload,
    template_variables,
    validate_template,
)


# ── 命中与基本替换 ───────────────────────────────────────

def test_renders_closed_set_hit():
    out = render_payload(
        '{"rule": "{{rule_id}}", "sev": "{{severity}}"}',
        {"rule_id": "revenue-drop", "severity": "warning"},
    )
    assert out == {"rule": "revenue-drop", "sev": "warning"}


def test_unquoted_numeric_placeholder_stays_numeric():
    """``"priority": {{priority}}`` 渲染成数字而不是字符串 —— 值走 JSON
    内层逃逸,数字看起来像数字就还是数字。"""
    out = render_payload('{"priority": {{priority}}}', {"priority": 2})
    assert out == {"priority": 2}
    assert isinstance(out["priority"], int)


def test_template_variables_declaration_order_deduped():
    text = '{"a": "{{rule_id}}", "b": "{{dim}}", "c": "{{rule_id}}"}'
    assert template_variables(text) == ["rule_id", "dim"]


def test_all_sample_variables_are_in_the_closed_set():
    """样本表就是闭集的镜像 —— 少一个,create 时对那个变量的校验就变成
    永远渲染失败的误报。"""
    assert set(SAMPLE_VARIABLES) == set(TEMPLATE_VARIABLES)


def test_validate_template_accepts_every_closed_set_variable():
    """每个合法变量单独出现在模板里都必须可渲染(以样本值为值)。"""
    for name in sorted(TEMPLATE_VARIABLES):
        problems = validate_template('{"v": "{{%s}}"}' % name)
        assert problems == [], f"{name}: {problems}"


# ── 响亮报错 ────────────────────────────────────────────

def test_unknown_variable_is_a_hard_error():
    with pytest.raises(RenderError) as e:
        render_payload('{"m": "{{mesage}}"}', {"message": "x"})
    assert "mesage" in str(e.value)
    assert "rule_id" in str(e.value)  # 错误文本给出合法集合


def test_closed_set_variable_with_no_value_is_a_hard_error():
    """引用合法变量但这次判定给不出值 → 硬错,绝不静默空串。"""
    with pytest.raises(RenderError) as e:
        render_payload('{"d": "{{dim}}"}', {"rule_id": "r"})
    assert "dim" in str(e.value)


def test_malformed_placeholder_name_is_refused():
    with pytest.raises(RenderError):
        render_payload('{"m": "{{Rule Id}}"}', {"Rule Id": "x"})


def test_unclosed_placeholder_is_refused():
    with pytest.raises(RenderError) as e:
        render_payload('{"m": "{{rule_id"}', {"rule_id": "r"})
    assert "unclosed" in str(e.value)


def test_stray_close_is_refused():
    with pytest.raises(RenderError) as e:
        render_payload('{"m": "rule_id}}"}', {})
    assert "unbalanced" in str(e.value)


def test_empty_template_is_refused():
    with pytest.raises(RenderError):
        render_payload("   ", {})


def test_output_that_is_not_json_is_refused():
    with pytest.raises(RenderError) as e:
        render_payload("not json at all", {})
    assert "not valid JSON" in str(e.value)


def test_output_that_is_not_an_object_is_refused():
    with pytest.raises(RenderError) as e:
        render_payload('["{{rule_id}}"]', {"rule_id": "r"})
    assert "JSON object" in str(e.value)


def test_max_bytes_is_enforced():
    with pytest.raises(RenderError) as e:
        render_payload('{"m": "{{message}}"}', {"message": "x" * 100},
                       max_bytes=32)
    assert "limit" in str(e.value)


# ── JSON 逃逸:值不能改写结构 ───────────────────────────

def test_quote_and_newline_round_trip_as_the_same_value():
    value = 'he said "drop the table"\nnext line\ttab'
    out = render_payload('{"m": "{{message}}"}', {"message": value})
    assert out["m"] == value


def test_injection_shaped_value_cannot_break_out_of_its_literal():
    """恶意串整体留在值里 —— 渲染结果仍解析为原值,没多出字段。"""
    value = '"},"admin":true,"x":"'
    out = render_payload('{"m": "{{message}}"}', {"message": value})
    assert out == {"m": value}
    assert "admin" not in out


def test_value_in_unquoted_position_that_is_not_json_is_refused():
    """模板把变量放在非字符串位时,值不是合法 JSON 字面量 → 拒绝,
    而不是把值当语法拼进文档。"""
    with pytest.raises(RenderError):
        render_payload('{"n": {{message}}}', {"message": "hello world"})


def test_unicode_survives_un_escaped():
    out = render_payload('{"m": "{{message}}"}', {"message": "当期 -12.3%"})
    assert out["m"] == "当期 -12.3%"
    assert "当期" in json.dumps(out, ensure_ascii=False)
