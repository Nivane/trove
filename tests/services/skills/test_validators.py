"""validator 档的执行器 —— 聚合级断言,零 LLM,三值判定。"""

from __future__ import annotations

import pytest

from trove.services.decision.expr import UNKNOWN
from trove.services.skills.validators import build_scope, run_validators


def _spec(expr, columns=None, message="violated", severity="advisory", mode=None):
    spec = {
        "name": "g1", "severity": severity,
        "checks": [{"expr": expr, "message": message}],
    }
    if columns is not None:
        spec["checks"][0]["columns"] = columns
    if mode is not None:
        spec["mode"] = mode
    return spec


# ── build_scope ──────────────────────────────────────────────

def test_scope_pools_named_columns():
    """点名多列时聚合是**合并**的:任一列为负 → min < 0。"""
    scope = build_scope(
        {"columns": ["a", "b"]}, ["a", "b"], [[1, -2], [3, 4]], row_count=2,
    )
    assert scope["min"] == -2.0
    assert scope["max"] == 4.0
    assert scope["sum"] == 6.0
    assert scope["avg"] == 1.5


def test_scope_missing_column_is_unknown():
    """点名的列一个都不在结果里 → 判不了,不是通过。"""
    scope = build_scope({"columns": ["nope"]}, ["a"], [[1]], row_count=1)
    assert scope["min"] is UNKNOWN
    assert scope["max"] is UNKNOWN


def test_scope_non_numeric_cells_are_skipped_not_zeroed():
    """文本/None 单元格跳过,不折算成 0.0 —— 0.0 是自信的'没越界'。"""
    scope = build_scope({"columns": ["a"]}, ["a"], [["N/A"], [5]], row_count=2)
    assert scope["min"] == 5.0
    assert scope["sum"] == 5.0


def test_scope_counts_nulls():
    scope = build_scope({"columns": ["a"]}, ["a"], [[None], [1], ["x"]], row_count=3)
    assert scope["null_count"] == 2.0        # None + 非数值


def test_scope_row_count_uses_authoritative_value():
    """行的权威数字是 state.row_count(结果可能被展示层截断)。"""
    scope = build_scope({}, ["a"], [[1]], row_count=999)
    assert scope["row_count"] == 999.0
    assert scope["col_count"] == 1.0


# ── run_validators:三值 ──────────────────────────────────────

def test_verdict_true_when_all_checks_pass():
    hits = run_validators([_spec("min >= 0", ["a"])], columns=["a"], rows=[[1], [2]],
                          row_count=2)
    assert hits[0]["verdict"] is True
    assert hits[0]["name"] == "g1"


def test_verdict_false_on_violation():
    hits = run_validators([_spec("min >= 0", ["a"])], columns=["a"], rows=[[1], [-2]],
                          row_count=2)
    assert hits[0]["verdict"] is False
    assert hits[0]["message"] == "violated"


def test_verdict_none_when_column_absent():
    """缺列必须落 None(判不了),绝不能塌成 True 或 False。"""
    hits = run_validators([_spec("min >= 0", ["nope"])], columns=["a"], rows=[[1]],
                          row_count=1)
    assert hits[0]["verdict"] is None


def test_verdict_none_on_bad_expression_instead_of_raising():
    """表达式写错不得让管线崩 —— 降级为'判不了',不是'通过'。"""
    hits = run_validators([_spec("min >=> 0", ["a"])], columns=["a"], rows=[[1]],
                          row_count=1)
    assert hits[0]["verdict"] is None
    assert "expression" in hits[0]["message"].lower()


def test_first_failing_check_wins_within_one_validator():
    hits = run_validators(
        [{"name": "g1", "severity": "advisory", "checks": [
            {"expr": "min >= 0", "columns": ["a"], "message": "neg"},
            {"expr": "row_count > 100", "message": "too few"},
        ]}],
        columns=["a"], rows=[[-1]], row_count=1,
    )
    assert hits[0]["verdict"] is False
    assert hits[0]["message"] == "neg"


def test_row_count_check_needs_no_columns():
    hits = run_validators([_spec("row_count > 0", [])], columns=["a"], rows=[],
                          row_count=0)
    assert hits[0]["verdict"] is False


def test_llm_mode_reports_cannot_evaluate_not_silence():
    """mode: llm 本期不驱动 —— 但**不许静默消失**。

    直接 `continue` 掉是最省事的写法,也是最坏的:声明了却没人管 = 静默
    失效,管理员看到"配好了、确认了",实际什么都没发生。三值里现成的答案
    是 None(判不了)—— 它不进用户附注(output 只报 advisory 的明确违反),
    但会落进 validator_hits 与质检统计,于是"没人管"是可观测的。
    """
    hits = run_validators([_spec("min >= 0", ["a"], mode="llm")],
                          columns=["a"], rows=[[1]], row_count=1)
    assert len(hits) == 1
    assert hits[0]["verdict"] is None
    assert "llm" in hits[0]["message"].lower()


def test_severity_is_carried_through():
    hits = run_validators([_spec("min >= 0", ["a"], severity="blocking")],
                          columns=["a"], rows=[[-1]], row_count=1)
    assert hits[0]["severity"] == "blocking"


# ── 未知 severity / 非宿主 node:降级,不静默 ─────────────────

@pytest.mark.parametrize("severity", ["Blocking", "BLOCKING", "blocking "])
def test_unknown_severity_degrades_instead_of_being_dropped(severity):
    """大小写笔误的 ``severity`` = 一条**明确违反**却被两头丢弃。

    ``validate.py:161`` 要 ``severity == "blocking"`` 才拦,``output.py:261``
    要 ``"advisory"`` 才渲染 —— 既不拦、也不提醒、连一条质检记录都不落,从
    外面看和"检查通过"一模一样。与 ``mode`` / ``checks`` 同一处置:降级为
    "判不了",进 ``validator_hits``(可观测、说得出原因)不进附注(不进用户
    屏幕)。落条里的 ``severity`` 用**非阻断的默认档** —— "没运行"绝不能拦、
    也绝不能渲染,用结构保证,不靠巧合。
    """
    hits = run_validators([_spec("min >= 0", ["a"], severity=severity)],
                          columns=["a"], rows=[[-2]], row_count=1)
    assert hits[0]["verdict"] is None
    assert "unknown severity" in hits[0]["message"]
    assert severity in hits[0]["message"]
    assert hits[0]["severity"] == "advisory"


@pytest.mark.parametrize("severity", ["", None])
def test_blank_or_null_severity_runs_normally(severity):
    """空 / null 是**缺省的写法**,不是未知值。``severity:`` 留空走默认
    advisory、照常判定;把它们和 ``Blocking`` 划进同一组会让一份检查静默
    失效 —— 与本次修复要消灭的恰好是同一件事(现状 ``str(spec.get(...))``
    会把它变成字符串 ``"None"``,而 ``"None"`` 两头都不接)。"""
    hits = run_validators([_spec("min >= 0", ["a"], severity=severity)],
                          columns=["a"], rows=[[-2]], row_count=1)
    assert hits[0]["verdict"] is False
    assert hits[0]["severity"] == "advisory"


def test_host_mismatch_degrades_instead_of_vanishing():
    """``validators_for`` 标记的"声明了非宿主 node"在这里变成一条可观测的
    判不了:丢掉它 = 从任何外部面看都和"没写"一样(``align_schema`` 同一类
    事故)。"""
    from trove.services.skills.validators import VALIDATOR_HOST

    spec = {**_spec("min >= 0", ["a"]), "host_mismatch": "gen_sql"}
    hits = run_validators([spec], columns=["a"], rows=[[-2]], row_count=1)
    assert hits[0]["verdict"] is None
    assert "gen_sql" in hits[0]["message"]
    assert VALIDATOR_HOST in hits[0]["message"]
    assert hits[0]["severity"] == "advisory"


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_fallback_verdict_messages_are_localized(lang):
    """没写 ``message`` 的 check 走兜底串,而它会**原样进用户屏幕**
    (``output.py`` 的 advisory 附注)—— 所以它必须跟着用户语言走。
    运维诊断那几条(mode / severity / host / 畸形 checks)只进
    ``validator_hits``,保持英文。**能进屏幕的才本地化。**"""
    violated = run_validators([_spec("min >= 0", ["a"], message="")],
                              columns=["a"], rows=[[-2]], row_count=1, lang=lang)
    assert violated[0]["message"] == (
        "违反：min >= 0" if lang == "zh" else "violated: min >= 0"
    )
    unknown = run_validators([_spec("min >= 0", ["nope"], message="")],
                             columns=["a"], rows=[[1]], row_count=1, lang=lang)
    assert unknown[0]["verdict"] is None
    # 兜底判词按原因分句(原先"缺列或非数值数据"一句盖住四种,见
    # test_truncation_text_does_not_blame_a_missing_column)
    assert unknown[0]["message"] == (
        "判不了（结果里没有点名的列）" if lang == "zh"
        else "cannot evaluate (named column is not in the result)"
    )


# ── 截断:窗口内的聚合不是整个结果的聚合 ──────────────────────

def test_truncated_rows_make_aggregates_unknown():
    """结果被展示上限截断时,内容聚合只覆盖窗口 —— 判不了,不是通过。

    ``rows`` 是窗口(execute_sql 按 max_rows 截断),``row_count`` 是真实
    总数。窗口内算出的 min/sum/null_count 都不是整个结果的对应值:拿它
    报"通过"正是本模块最反对的"没查却报平安"。
    """
    scope = build_scope({"columns": ["a"]}, ["a"], [[1], [2]], row_count=5000)
    for k in ("min", "max", "sum", "avg", "null_count"):
        assert scope[k] is UNKNOWN, k
    assert scope["row_count"] == 5000.0
    assert scope["col_count"] == 1.0


def test_untruncated_rows_still_compute():
    """守卫的反面:row_count 与窗口等长时照常算 —— 别把守卫写成恒 UNKNOWN。"""
    scope = build_scope({"columns": ["a"]}, ["a"], [[1], [2]], row_count=2)
    assert scope["min"] == 1.0
    assert scope["sum"] == 3.0


def test_truncated_result_verdict_is_none_not_true():
    hits = run_validators([_spec("min >= 0", ["a"])], columns=["a"],
                          rows=[[1]], row_count=9999)
    assert hits[0]["verdict"] is None


# ── 空 checks / 形状写错:降级,不静默通过 ────────────────────

def test_empty_checks_is_none_not_true():
    """声明了却没有任何检查 = 判不了。静默的"通过"和"没人管"一样坏。"""
    hits = run_validators([{"name": "g1", "severity": "advisory", "checks": []}],
                          columns=["a"], rows=[[1]], row_count=1)
    assert hits[0]["verdict"] is None
    assert "no checks" in hits[0]["message"].lower()


def test_missing_checks_key_is_none_not_true():
    hits = run_validators([{"name": "g1", "severity": "advisory"}],
                          columns=["a"], rows=[[1]], row_count=1)
    assert hits[0]["verdict"] is None


def test_non_dict_spec_degrades_instead_of_raising():
    """SKILL.md 是可以手改的 YAML —— 形状写错不得把管线炸掉。"""
    hits = run_validators([None, _spec("min >= 0", ["a"])],
                          columns=["a"], rows=[[1]], row_count=1)
    assert len(hits) == 2
    assert hits[0]["verdict"] is None
    assert hits[1]["verdict"] is True


def test_mapping_checks_degrades_instead_of_raising():
    """``checks`` 写成映射(常见手误)时迭代出的是键(字符串)。

    形状错走**形状**那条分支(``malformed_checks``),不是"求值抛异常"
    (``check_error``)—— 两者都要管理员做不同的事,原因码必须分得开。
    """
    hits = run_validators([{"name": "g1", "severity": "advisory",
                            "checks": {"expr": "min >= 0"}}],
                          columns=["a"], rows=[[1]], row_count=1)
    assert hits[0]["verdict"] is None
    assert hits[0]["reason"] == "malformed_checks"
    assert "mapping" in hits[0]["message"].lower()


def test_check_error_is_reserved_for_evaluation_raises():
    """``check_error`` 留给**求值真的抛了**那种:形状没错,求值炸了。

    与 ``malformed_checks`` 分开的理由是处置不同 —— 前者查 YAML 缩进,
    后者是表达式实现的问题,查到的地方不一样。
    """
    from trove.services.skills import validators as mod

    class _Boom:
        def eval(self, scope):
            raise RuntimeError("boom")

    real = mod.parse_condition
    mod.parse_condition = lambda expr, vars: _Boom()
    try:
        hits = run_validators([_spec("min >= 0", ["a"])], columns=["a"],
                              rows=[[1]], row_count=1)
    finally:
        mod.parse_condition = real
    assert hits[0]["verdict"] is None
    assert hits[0]["reason"] == "check_error"
    assert "boom" in hits[0]["message"]


def test_scalar_checks_degrades_instead_of_raising():
    """``checks: 5`` / ``checks: yes`` —— 手写 YAML 最省事的笔误,不得炸管线。"""
    hits = run_validators([{"name": "g1", "severity": "advisory", "checks": 5}],
                          columns=["a"], rows=[[1]], row_count=1)
    assert hits[0]["verdict"] is None
    assert "checks" in hits[0]["message"].lower()


def test_string_row_count_still_coerces():
    """row_count 归一化后再比较 —— 可强转的字符串照常算,不因守卫退化。"""
    scope = build_scope({"columns": ["a"]}, ["a"], [[1], [2]], row_count="2")
    assert scope["row_count"] == 2.0
    assert scope["min"] == 1.0


# ── 判不了的原因码:None 比率是这套机制唯一的质量信号 ──────────

def test_every_none_verdict_carries_a_machine_readable_reason():
    """每条"判不了"都要带 ``reason``:自由文本 message 数不出分布。

    十一种 None 各有各的处置路径(手改文件写错 / 声明了非宿主 / 结果太大 /
    真缺列),混在一句"缺列或非数值数据"里,运维只能靠猜 —— 而这些都是
    **管理员侧的配置问题**,本来就该能按类统计。
    """
    from trove.services.skills.validators import NONE_REASONS

    cases = {
        "malformed_spec": ([None], ["a"], [[1]], 1),
        "host_mismatch": ([{**_spec("min >= 0", ["a"]), "host_mismatch": "gen_sql"}],
                          ["a"], [[1]], 1),
        "unknown_severity": ([_spec("min >= 0", ["a"], severity="Blocking")],
                             ["a"], [[1]], 1),
        "unsupported_mode": ([_spec("min >= 0", ["a"], mode="llm")], ["a"], [[1]], 1),
        "malformed_checks": ([{"name": "g1", "severity": "advisory", "checks": 5}],
                             ["a"], [[1]], 1),
        "empty_checks": ([{"name": "g1", "severity": "advisory", "checks": []}],
                         ["a"], [[1]], 1),
        "bad_expression": ([_spec("mn >= 0", ["a"])], ["a"], [[1]], 1),
        "missing_column": ([_spec("min >= 0", ["nope"])], ["a"], [[1]], 1),
        "truncated_rows": ([_spec("min >= 0", ["a"])], ["a"], [[1]], 9999),
        "no_columns_declared": ([_spec("min >= 0")], ["a"], [[1]], 1),
        "non_numeric_data": ([_spec("min >= 0", ["a"])], ["a"], [["N/A"]], 1),
    }
    for expected, (specs, columns, rows, row_count) in cases.items():
        hits = run_validators(specs, columns=columns, rows=rows, row_count=row_count)
        assert hits[0]["verdict"] is None, expected
        assert hits[0]["reason"] == expected, (
            f"{expected}: 拿到 reason={hits[0].get('reason')!r}"
        )
        assert hits[0]["reason"] in NONE_REASONS


def test_unknown_reason_tracks_scope_degradation():
    """原因码的分支必须与 ``build_scope`` 的退化条件一一对应。

    两处各写一遍条件必然漂移,而漂移的表现是判词解释错了原因 —— 正是这次
    要修的那个 bug("缺列或非数值数据" 盖住了截断)。所以这条同时断言两侧。
    """
    from trove.services.skills.validators import _unknown_reason

    checks = [
        ({"columns": ["nope"]}, ["a"], [[1]], 1, "missing_column"),
        ({"columns": []}, ["a"], [[1]], 1, "no_columns_declared"),
        ({"columns": ["a"]}, ["a"], [[1]], 9999, "truncated_rows"),
        ({"columns": ["a"]}, ["a"], [["N/A"]], 1, "non_numeric_data"),
    ]
    for check, columns, rows, row_count, expected in checks:
        scope = build_scope(check, columns, rows, row_count)
        assert scope["min"] is UNKNOWN, expected      # 退化确实发生了
        assert _unknown_reason(check, columns, rows, row_count) == expected

    # 反向:不退化的 check 不该被判成退化
    ok = {"columns": ["a"]}
    assert build_scope(ok, ["a"], [[1]], 1)["min"] == 1.0


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_truncation_text_does_not_blame_a_missing_column(lang):
    """截断的兜底判词不得说"缺列" —— 那是**误导**(列在,是窗口太小)。

    这句话进 ``validator_hits`` 供运维看:照"缺列"去查,查的是一个并不缺失
    的列。今天它会出现在 1000 行以上任何阻塞档 validator 的记录里。
    """
    hits = run_validators([_spec("min >= 0", ["a"], message="")], columns=["a"],
                          rows=[[1]], row_count=9999, lang=lang)
    text = hits[0]["message"]
    assert hits[0]["reason"] == "truncated_rows"
    assert ("缺列" if lang == "zh" else "missing column") not in text
    # 说清是窗口的事(中英各一份,词面不同但都得指向结果集大小)
    assert ("窗口" if lang == "zh" else "window") in text


def test_format_hit_omits_empty_name_and_collapses_newlines():
    """hit 的人类可读渲染:没有名字不留 ``[]``;换行压平。

    两条都会进用户屏幕(Markdown 引用块)与 ``error_feedback``:``[] 消息``
    是残缺的排版,而一个换行就把引用块冲出三行、把后续内容顶成正文。
    """
    from trove.services.skills.validators import format_hit

    assert format_hit({"name": "", "message": "boom"}) == "boom"
    assert format_hit({"name": "g1", "message": "a\nb\n\nc"}) == "[g1] a b c"
    assert format_hit({"message": None}) == ""

