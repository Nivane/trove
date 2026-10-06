"""guard 档 —— 执行前 SQL 域断言:特征提取、三值判定、写时校验。

三层都在这里钉住(纯函数层 / 选人与运行层 / 写入面),零 LLM、零网络:

- ``extract_sql_features``:九个变量逐个正反例 + **int 非 bool** 的钉子,
  以及与 ``referenced_tables`` 的表数对账(两处对「SQL 触及哪些表」必须一致);
- ``run_guards``:断言极性(合规 = False,违反 = True)、三值(None 绝不拦)、
  首败即停、每条降级都带机器可读的 ``none_reason``;
- ``SkillService`` 写入面:域外标识符 / blocking 缺 hint / 非法 severity 等
  一律 400,不留"配了但永远不跑"的静默守卫。
"""

from __future__ import annotations

import pytest
import yaml

from trove.services.authz.enforcer import referenced_tables
from trove.services.decision.expr import UNKNOWN
from trove.services.skills.guards import (
    DEFAULT_GUARD_TARGET,
    GUARD_HOST,
    GUARD_NONE_REASONS,
    GUARD_TARGETS,
    GUARD_VARIABLES,
    GuardRunner,
    GuardVerdict,
    extract_sql_features,
    format_guard_hit,
    run_guards,
)
from trove.services.skills.service import SkillService

# ── helpers ──────────────────────────────────────────────────


def _f(sql: str, dialect: str = "") -> dict:
    return extract_sql_features(sql, dialect)


def _check(
    expr: str = "select_star == 0",
    *,
    name: str = "g1",
    severity: str = "advisory",
    reason: str = "reason text",
    hint_zh: str = "",
    hint_en: str = "",
    target: str | None = None,
) -> dict:
    c: dict = {"name": name, "severity": severity, "expr": expr, "reason": reason}
    if hint_zh:
        c["hint_zh"] = hint_zh
    if hint_en:
        c["hint_en"] = hint_en
    if target is not None:
        c["target"] = target
    return c


def _entry(
    checks: list | str | None = None,
    *,
    name: str = "g-skill",
    target: str | None = None,
    node: str | None = None,
    host_mismatch: str | None = None,
) -> dict:
    """``guards_for`` 交出来的条目形状(已确认、guard 档、guard 块在内)。"""
    if checks is None:
        checks = [_check()]
    guard: dict = {"targets": ["sql"], "checks": checks}
    if target is not None:
        guard["target"] = target
    entry: dict = {"name": name, "tier": "guard", "status": "confirmed",
                   "guard": guard}
    if node is not None:
        entry["triggers"] = {"node": node}
    if host_mismatch is not None:
        entry["host_mismatch"] = host_mismatch
    return entry


def _svc(tmp_path) -> SkillService:
    return SkillService(tmp_path / ".trove" / "skills")


_GUARD_SKILL = {
    "name": "sql-hygiene",
    "description": "SQL 结构规范",
    "tier": "guard",
    "guard": {
        "targets": ["sql"],
        "checks": [{
            "name": "no-select-star",
            "severity": "blocking",
            "expr": "select_star == 0",
            "reason": "不允许裸查全列",
            "hint_zh": "把 * 展开成显式列",
            "hint_en": "Expand * into explicit columns",
        }],
    },
    "body": "人类可读说明",
}


# ── extract_sql_features:select_star ─────────────────────────


def test_select_star_plain_is_one():
    assert _f("SELECT * FROM loans")["select_star"] == 1


def test_select_star_explicit_columns_is_zero():
    assert _f("SELECT id, name FROM loans")["select_star"] == 0


def test_select_star_qualified_star_still_counts():
    """``a.*`` 也是展开星号 —— 排除它需要走 Column 祖先,漏了就是一次误放。"""
    assert _f("SELECT a.* FROM loans a")["select_star"] == 1


def test_count_star_is_not_select_star():
    """``COUNT(*)`` 里的星号不展开列 —— 函数祖先把它排除掉。"""
    assert _f("SELECT COUNT(*) FROM loans")["select_star"] == 0


def test_exists_subquery_star_is_not_select_star():
    """``EXISTS (SELECT *)`` 同理:星号在函数(子查询谓词)里。"""
    assert _f("SELECT EXISTS (SELECT * FROM loans) AS e")["select_star"] == 0


def test_select_star_in_subquery_counts():
    assert _f("SELECT t.x FROM (SELECT * FROM loans) t")["select_star"] == 1


# ── extract_sql_features:table_count / join_count ────────────


def test_table_count_join():
    assert _f("SELECT l.id FROM loans l JOIN districts d ON l.d = d.id"
              )["table_count"] == 2


def test_table_count_dedups_same_table():
    """同一张表起两个别名仍是一张表(与 ``referenced_tables`` 的去重同判据)。"""
    assert _f("SELECT a.id FROM loans a, loans b")["table_count"] == 1


def test_table_count_excludes_cte_alias():
    """CTE 别名不是表 —— 把它算进去,权限与计数都会在有 CTE 的库上误报。"""
    assert _f("WITH x AS (SELECT id FROM loans) SELECT * FROM x"
              )["table_count"] == 1


def test_table_count_strips_schema_qualifier():
    assert _f("SELECT id FROM db.financial.loans")["table_count"] == 1


def test_table_count_zero_for_select_literal():
    assert _f("SELECT 1")["table_count"] == 0


def test_join_count_comma_join_counts():
    """``FROM a, b`` 在 sqlglot 里也是 Join(逗号连接同样是联表)。"""
    assert _f("SELECT a.id FROM loans a, districts d")["join_count"] == 1


def test_join_count_two_joins():
    assert _f(
        "SELECT a.id FROM a JOIN b ON a.id = b.id JOIN c ON b.id = c.id"
    )["join_count"] == 2


def test_join_count_zero_without_join():
    assert _f("SELECT id FROM loans")["join_count"] == 0


# ── extract_sql_features:has_where / has_limit ───────────────


def test_has_where_true_filter():
    assert _f("SELECT id FROM loans WHERE id = 1")["has_where"] == 1


def test_has_where_always_true_one_eq_one_is_not_where():
    """模板里保底拼的 ``WHERE 1=1`` 不是过滤 —— 把它算成"有 WHERE"正是
    这条守卫要抓的形态。"""
    assert _f("SELECT id FROM loans WHERE 1 = 1")["has_where"] == 0


def test_has_where_boolean_true_is_not_where():
    assert _f("SELECT id FROM loans WHERE TRUE")["has_where"] == 0


def test_has_where_always_true_conjunct_with_real_filter():
    assert _f("SELECT id FROM loans WHERE 1 = 1 AND id = 5")["has_where"] == 1


def test_has_where_absent():
    assert _f("SELECT id FROM loans")["has_where"] == 0


def test_has_limit():
    assert _f("SELECT id FROM loans LIMIT 10")["has_limit"] == 1


def test_has_limit_fetch_first():
    """``FETCH FIRST n ROWS ONLY`` 是 LIMIT 的 SQL 标准写法,同样是限量。"""
    assert _f("SELECT id FROM loans ORDER BY id OFFSET 0 ROWS "
              "FETCH FIRST 5 ROWS ONLY")["has_limit"] == 1


def test_has_limit_absent():
    assert _f("SELECT id FROM loans")["has_limit"] == 0


# ── extract_sql_features:has_time_predicate ──────────────────


def test_time_predicate_on_at_suffix_column():
    assert _f("SELECT id FROM loans WHERE created_at > 'x'"
              )["has_time_predicate"] == 1


def test_time_predicate_on_date_named_column():
    assert _f("SELECT id FROM loans WHERE order_date = '2020-01-01'"
              )["has_time_predicate"] == 1


def test_time_predicate_date_literal():
    assert _f("SELECT id FROM loans WHERE d = '2020-01-01'"
              )["has_time_predicate"] == 1


def test_time_predicate_datetime_literal():
    assert _f("SELECT id FROM loans WHERE d = '2020-01-01 12:00:00'"
              )["has_time_predicate"] == 1


def test_time_predicate_extract_function():
    assert _f("SELECT id FROM loans WHERE EXTRACT(YEAR FROM d) = 2020"
              )["has_time_predicate"] == 1


def test_time_predicate_cast_to_date():
    assert _f("SELECT id FROM loans WHERE CAST(d AS DATE) > '2020-01-01'"
              )["has_time_predicate"] == 1


def test_time_predicate_current_date_function():
    assert _f("SELECT id FROM loans WHERE d = CURDATE()"
              )["has_time_predicate"] == 1


def test_time_predicate_in_having():
    """HAVING 里的时间引用同样算(两个子句一起扫)。"""
    assert _f("SELECT d, COUNT(*) FROM loans GROUP BY d "
              "HAVING MAX(t) > '2020-01-01'")["has_time_predicate"] == 1


def test_time_predicate_absent_on_plain_number_filter():
    assert _f("SELECT id FROM loans WHERE amount > 100"
              )["has_time_predicate"] == 0


def test_time_predicate_date_like_string_is_over_inclusive_by_design():
    """启发式**宁可多认**:把日期样子的字符串常量认成时间,只让
    ``has_time_predicate == 1`` 类守卫更松;少认则会把带时间过滤的合规 SQL
    判成不合规(误拦比漏放贵)。这条测试固化的是这个方向,不是这个精度。"""
    assert _f("SELECT id FROM loans WHERE name = '2020-01-01'"
              )["has_time_predicate"] == 1


# ── extract_sql_features:has_aggregate / cte_count / sql_len ─


def test_has_aggregate():
    assert _f("SELECT SUM(amount) FROM loans")["has_aggregate"] == 1


def test_has_aggregate_count():
    assert _f("SELECT COUNT(*) FROM loans")["has_aggregate"] == 1


def test_has_aggregate_absent():
    assert _f("SELECT id FROM loans")["has_aggregate"] == 0


def test_cte_count():
    assert _f("WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a"
              )["cte_count"] == 2


def test_cte_count_zero():
    assert _f("SELECT id FROM loans")["cte_count"] == 0


def test_sql_len_collapses_whitespace():
    """归一化 = 空白折叠:缩进/换行不进长度,与模型的排版风格解耦。"""
    assert _f("SELECT\n    id\nFROM loans")["sql_len"] == _f(
        "SELECT id FROM loans")["sql_len"]


def test_sql_len_counts_characters():
    assert _f("SELECT 1")["sql_len"] == len("SELECT 1")


# ── extract_sql_features:契约(键集 / int / 全 UNKNOWN) ─────


def test_feature_keys_are_exactly_the_guard_variables():
    """提取器与 ``GUARD_VARIABLES`` 是**一对**:声明什么就提取什么 ——
    多一个键是死代码,少一个键是某个变量永远判不了。"""
    assert set(_f("SELECT id FROM loans")) == set(GUARD_VARIABLES)


def test_all_features_are_int_never_bool():
    """``bool`` 会让 ``expr.as_number`` 返回 ``None`` —— 每条守卫都会静默
    变成"判不了"。布尔语义用 0/1 表达,这一条是量纲级钉子。"""
    feats = _f("SELECT * FROM loans l JOIN districts d ON l.d = d.id "
               "WHERE 1 = 1 AND created_at > 'x' LIMIT 5")
    for key, value in feats.items():
        assert type(value) is int, key  # noqa: E721 — 就是要排除 bool


@pytest.mark.parametrize("sql", ["", "   ", "\n\t "])
def test_blank_sql_is_all_unknown(sql):
    """空手而归不是一堆 0:"没看懂"绝不能塌成"没有星号、没有 WHERE"的
    自信判断。"""
    assert set(_f(sql).values()) == {UNKNOWN}


@pytest.mark.parametrize("sql", ["(((", "SELECT * FROM", "not sql at all !!"])
def test_unparseable_sql_is_all_unknown(sql):
    assert set(_f(sql).values()) == {UNKNOWN}


def test_dialect_is_forwarded_to_the_parser():
    """方言喂给解析器才有意义:MySQL 反引号在默认方言下解析不了。"""
    sql = "SELECT `id` FROM loans LIMIT 5"
    assert set(_f(sql).values()) == {UNKNOWN}          # 默认方言:坏句
    assert _f(sql, "mysql")["has_limit"] == 1          # 指定方言:全部算得出来


@pytest.mark.parametrize("sql", [
    "SELECT l.id FROM loans l JOIN districts d ON l.d = d.id",
    "WITH x AS (SELECT id FROM loans) SELECT * FROM x",
    "SELECT a.id FROM loans a, loans b",
    "SELECT * FROM db.schema.loans",
    "SELECT 1",
])
def test_table_count_agrees_with_referenced_tables(sql):
    """两处对「SQL 触及哪些表」必须一致(授权面与守卫面各算各的,
    判据漂移的表现是权限与守卫对同一条 SQL 各说各话)。"""
    refs = referenced_tables(sql)
    assert refs is not None
    assert _f(sql)["table_count"] == len(refs)


# ── run_guards:断言极性 ──────────────────────────────────────


def test_compliant_sql_triggers_false():
    """``expr`` 是合规要求:成立 = 合规 = **未命中**(triggered False)。

    与 validator 的 ``verdict`` 同向(True 通过 / False 违反),"对称物"
    就是这个意思。"""
    out = run_guards([_entry()], sql="SELECT id FROM loans", lang="zh")
    assert len(out) == 1
    assert out[0].triggered is False
    assert out[0].blocking is False


def test_violation_triggers_true():
    out = run_guards([_entry()], sql="SELECT * FROM loans", lang="zh")
    assert out[0].triggered is True


def test_blocking_property_requires_both():
    """命中且 severity=blocking 才拦;advisory 的命中只报告。"""
    blocking = run_guards(
        [_entry([_check(severity="blocking")])], sql="SELECT * FROM loans",
        lang="zh",
    )
    assert blocking[0].triggered is True and blocking[0].blocking is True
    advisory = run_guards(
        [_entry([_check(severity="advisory")])], sql="SELECT * FROM loans",
        lang="zh",
    )
    assert advisory[0].triggered is True and advisory[0].blocking is False


def test_polarity_compliant_direction_on_limit_guard():
    """反方向再来一条(``has_limit == 1`` 的合规 SQL 不命中,明细不带
    LIMIT 才命中)—— 钉子不能只钉一条表达式的某一侧。"""
    entry = _entry([_check("has_limit == 1", name="must-limit")])
    assert run_guards([entry], sql="SELECT id FROM loans LIMIT 10")[0
        ].triggered is False
    assert run_guards([entry], sql="SELECT id FROM loans")[0].triggered is True


# ── run_guards:三值(None 绝不拦) ───────────────────────────


def test_unparseable_sql_yields_none_never_blocks():
    """坏句不是拦截理由(语法由编译期与 SQL_GUARD 挡)—— 解析失败 ⇒ 全部
    ``triggered is None`` ⇒ 绝不拦。把 None 塌成"命中"会拦下正确结果。"""
    out = run_guards([_entry()], sql="(((", lang="zh")
    assert out[0].triggered is None
    assert out[0].blocking is False
    assert out[0].none_reason == "unparseable_sql"


def test_none_verdict_keeps_authored_judgement_text():
    """作者写了 ``reason`` 时,判不了的落条**保留作者判词**(更具体);
    机器码在 ``none_reason`` 上,不靠人话表达成因。"""
    out = run_guards([_entry()], sql="(((", lang="zh")[0]
    assert out.reason == "reason text"
    assert out.none_reason == "unparseable_sql"


def test_none_verdict_fallback_message_is_localized():
    """没写 ``reason`` 的 check 判不了时走兜底串,按原因码分句、跟语言走
    (与 validator 的兜底同一纪律)。"""
    zh = run_guards([_entry([_check(reason="")])], sql="(((", lang="zh")[0]
    en = run_guards([_entry([_check(reason="")])], sql="(((", lang="en")[0]
    assert zh.reason == "判不了（SQL 解析失败，无法提取结构特征）"
    assert en.reason == "cannot evaluate (the SQL could not be parsed)"


def test_as_hit_carries_none_reason_only_when_unjudged():
    """机器码叫 ``none_reason`` 而不是 ``reason``:同名两义(人话 vs 码)是
    最容易被消费方读错的那类字段;判定过的 hit 没有"为什么"。"""
    undecided = run_guards([_entry()], sql="(((", lang="zh")[0].as_hit()
    assert undecided["none_reason"] == "unparseable_sql"
    judged = run_guards([_entry()], sql="SELECT id FROM loans", lang="zh")[0
        ].as_hit()
    assert "none_reason" not in judged


def test_none_reason_codes_stay_within_the_declared_closure():
    """每条降级路径的机器码都在闭集里(消费方按码分支,闭集外的码是死分支)。"""
    assert "host_mismatch" in GUARD_NONE_REASONS


# ── run_guards:hit 形状 / hint / target ─────────────────────


def test_hit_shape_and_judgement_text():
    out = run_guards(
        [_entry([_check(reason="不允许裸查全列")])],
        sql="SELECT * FROM loans", lang="zh",
    )[0].as_hit()
    assert set(out) == {"name", "severity", "triggered", "reason", "hint",
                        "target"}
    assert out["name"] == "g1"
    assert out["reason"] == "不允许裸查全列"
    assert out["target"] == DEFAULT_GUARD_TARGET


def test_hint_is_localized_by_question_language():
    entry = _entry([_check(hint_zh="展开成显式列", hint_en="Expand columns")])
    zh = run_guards([entry], sql="SELECT * FROM loans", lang="zh")[0]
    en = run_guards([entry], sql="SELECT * FROM loans", lang="en")[0]
    assert zh.hint == "展开成显式列"
    assert en.hint == "Expand columns"


def test_hint_falls_back_when_only_one_language_is_written():
    entry = _entry([_check(hint_en="Expand columns")])
    zh = run_guards([entry], sql="SELECT * FROM loans", lang="zh")[0]
    assert zh.hint == "Expand columns"


def test_check_target_overrides_guard_target():
    entry = _entry([_check(target="schema_linking")], target="gen_sql")
    assert run_guards([entry], sql="SELECT id FROM loans")[0
        ].target == "schema_linking"


def test_guard_level_target_applies_when_check_lacks_one():
    entry = _entry([_check()], target="query_sketch")
    assert run_guards([entry], sql="SELECT id FROM loans")[0
        ].target == "query_sketch"


def test_unknown_target_degrades_instead_of_running():
    """目标名打错会让回滚落到梯子首档 —— 实际的纠正方向与作者声明不同,
    记一条"判不了"而不是照跑。"""
    out = run_guards([_entry([_check(target="nowhere")])],
                     sql="SELECT id FROM loans")[0]
    assert out.triggered is None
    assert out.none_reason == "unknown_target"
    assert out.severity == "advisory"  # 没运行的判定不能拦


def test_target_closure_is_the_declared_set():
    assert "gen_retrieve" in GUARD_TARGETS
    assert set(GUARD_TARGETS) >= {"gen_sql", "query_sketch", "schema_linking"}


# ── run_guards:多守卫(互不影响 / 首败即停该条) ─────────────


def test_checks_are_independent():
    """一个 skill 的两条 check 是两条独立守卫:一条命中不影响另一条照常判
    (把它们并成一个多检查 spec,一条判不了会盖掉另一条的命中)。"""
    entry = _entry([
        _check("select_star == 0", name="no-star"),
        _check("has_limit == 1", name="must-limit"),
    ])
    out = {v.name: v for v in run_guards(
        [entry], sql="SELECT * FROM loans", lang="zh")}
    assert out["no-star"].triggered is True
    assert out["must-limit"].triggered is True  # 第二条照跑照判


def test_first_failing_check_wins_within_one_unit():
    """首败即停是共用内核的纪律,guard 域同样继承:一条 spec 里两个 check
    都违反时只报第一个。直接走 ``run_checks`` 钉内核层(守卫的生产形状是
    一检查一 spec,多检查共存在这里是内核契约)。"""
    from trove.services.skills.validators import run_checks

    hits = run_checks(
        [{"name": "spec", "severity": "advisory", "checks": [
            {"expr": "select_star == 0", "message": "first"},
            {"expr": "has_limit == 1", "message": "second"},
        ]}],
        variables=GUARD_VARIABLES,
        scope_for=lambda check: _f("SELECT * FROM loans"),
        unknown_reason=lambda check: "unparseable_sql",
        unknown_texts={"unparseable_sql": ("x", "x")},
        label="guard",
    )
    assert hits[0]["message"] == "first"


def test_blank_check_name_falls_back_to_skill_name():
    entry = _entry([_check(name="")])
    assert run_guards([entry], sql="SELECT id FROM loans")[0].name == "g-skill"


def test_no_entries_is_no_verdicts():
    assert run_guards([], sql="SELECT * FROM loans") == []


# ── run_guards:malformed / 降级(每条都带机器码,不许静默) ──


def test_non_mapping_entry_is_malformed_spec():
    out = run_guards(["junk"], sql="SELECT id FROM loans", lang="zh")
    assert out[0].triggered is None
    assert out[0].none_reason == "malformed_spec"


def test_missing_guard_block_degrades():
    """tier: guard 却没有 guard: 块(手写文件绕过了写入面):文件在、
    确认过、什么都没发生 —— 从外面看和检查通过一样,必须落一条可观测的
    "判不了"。"""
    entry = {"name": "hollow", "tier": "guard", "status": "confirmed"}
    out = run_guards([entry], sql="SELECT id FROM loans", lang="zh")
    assert out[0].none_reason == "missing_guard_block"
    assert "did not run" in out[0].reason


def test_checks_not_a_list_degrades():
    entry = _entry(checks="nope")
    out = run_guards([entry], sql="SELECT id FROM loans", lang="zh")
    assert out[0].none_reason == "malformed_checks"


def test_empty_checks_degrades():
    entry = _entry(checks=[])
    out = run_guards([entry], sql="SELECT id FROM loans", lang="zh")
    assert out[0].none_reason == "empty_checks"


def test_non_mapping_check_degrades():
    entry = _entry(checks=["junk"])
    out = run_guards([entry], sql="SELECT id FROM loans", lang="zh")
    assert out[0].none_reason == "malformed_checks"


def test_bad_expression_degrades_not_raises():
    out = run_guards([_entry([_check("select_star >>> 0")])],
                     sql="SELECT id FROM loans", lang="zh")
    assert out[0].triggered is None
    assert out[0].none_reason == "bad_expression"


def test_missing_expression_degrades():
    out = run_guards([_entry([{"name": "g1", "severity": "advisory"}])],
                     sql="SELECT id FROM loans", lang="zh")
    assert out[0].triggered is None
    assert out[0].none_reason == "bad_expression"


@pytest.mark.parametrize("severity", ["Blocking", "BLOCKING", "blocking "])
def test_unknown_severity_degrades_and_never_blocks(severity):
    """大小写笔误的 severity = 一条明确违反却被两头丢弃(execute_sql 要
    "blocking" 才拦,output 要 "advisory" 才渲染)。降级为"判不了",
    并用非阻断默认档落条 —— "没运行"绝不能拦,用结构保证不靠巧合。"""
    out = run_guards([_entry([_check(severity=severity)])],
                     sql="SELECT * FROM loans", lang="zh")[0]
    assert out.triggered is None
    assert out.none_reason == "unknown_severity"
    assert out.severity == "advisory"
    assert out.blocking is False


def test_host_mismatch_degrades():
    entry = _entry(node="gen_sql", host_mismatch="gen_sql")
    out = run_guards([entry], sql="SELECT id FROM loans", lang="zh")[0]
    assert out.triggered is None
    assert out.none_reason == "host_mismatch"
    assert GUARD_HOST in out.reason


def test_host_mismatch_names_the_check_when_authored():
    """守卫的生产形状是"一检查一单元":host_mismatch 落在每个检查单元上,
    名字取检查自己的名(没写才回落 skill 名)—— 多条检查的技能被错放时,
    每条都点名,不是笼统一句"这个技能没跑"。"""
    entry = _entry(name="misplaced", host_mismatch="gen_sql")
    assert run_guards([entry], sql="SELECT id FROM loans")[0].name == "g1"
    blank = _entry([_check(name="")], name="misplaced", host_mismatch="gen_sql")
    assert run_guards([blank], sql="SELECT id FROM loans")[0].name == "misplaced"


# ── GuardRunner(execute_sql 侧门面)──────────────────────────


class _FakeSkills:
    def __init__(self, entries):
        self.entries = entries
        self.calls: list[tuple[str, dict]] = []

    def guards_for(self, node, **ctx):
        self.calls.append((node, ctx))
        return self.entries


def test_runner_selects_from_service_and_judges():
    skills = _FakeSkills([_entry()])
    runner = GuardRunner(skills)
    out = runner.check("SELECT * FROM loans", dialect="sqlite", lang="zh")
    assert skills.calls[0][0] == GUARD_HOST
    assert out[0].triggered is True


def test_runner_passes_ctx_through_to_selection():
    skills = _FakeSkills([])
    runner = GuardRunner(skills)
    runner.check("SELECT 1", complexity="standard", datasource="demo")
    assert skills.calls[0][1] == {"complexity": "standard", "datasource": "demo"}


def test_runner_lang_comes_from_ctx():
    skills = _FakeSkills([_entry([_check(hint_zh="中文", hint_en="english")])])
    runner = GuardRunner(skills)
    out = runner.check("SELECT * FROM loans", lang="en")
    assert out[0].hint == "english"


def test_runner_none_entries_is_empty():
    runner = GuardRunner(_FakeSkills(None))
    assert runner.check("SELECT 1") == []


def test_verdict_dataclass_is_frozen():
    v = GuardVerdict(name="g", severity="advisory", triggered=False,
                     reason="r")
    with pytest.raises(Exception):
        v.name = "other"


# ── format_guard_hit(用户屏幕渲染的收口)─────────────────────


def test_format_guard_hit_flattens_newlines():
    """判词是手写 YAML 的自由文本,渲染进引用块前要压平 —— 一个换行就把
    引用块冲出三行。"""
    rendered = format_guard_hit({"name": "g", "reason": "第一行\n第二行"})
    assert "\n" not in rendered
    assert rendered == "[g] 第一行 第二行"


def test_format_guard_hit_omits_empty_name():
    assert format_guard_hit({"name": "", "reason": "判词"}) == "判词"


# ── 写入面:guard 档的 create 校验(400,不留静默守卫)───────


def test_guard_skill_round_trips_its_block(tmp_path):
    """``read_skill`` 手工挑 key —— guard 块没被投影的程序"看不见"它。"""
    svc = _svc(tmp_path)
    svc.create(dict(_GUARD_SKILL))
    entry = svc.read_skill("sql-hygiene")
    assert entry["tier"] == "guard"
    assert entry["guard"]["checks"][0]["expr"] == "select_star == 0"


def test_guard_skill_is_pending_until_confirmed(tmp_path):
    """确认门对 guard 同样有效:草稿不进运行。"""
    svc = _svc(tmp_path)
    svc.create(dict(_GUARD_SKILL))
    assert svc.guards_for(GUARD_HOST) == []
    svc.confirm("sql-hygiene")
    assert len(svc.guards_for(GUARD_HOST)) == 1


def test_confirmed_guard_runs_end_to_end(tmp_path):
    """create → confirm → guards_for → GuardRunner.check:整条链上判定生效。"""
    svc = _svc(tmp_path)
    svc.create(dict(_GUARD_SKILL))
    svc.confirm("sql-hygiene")
    out = GuardRunner(svc).check("SELECT * FROM loans", lang="zh")
    assert out[0].blocking is True
    assert out[0].reason == "不允许裸查全列"
    assert out[0].hint == "把 * 展开成显式列"


def test_declared_host_gets_marked_when_queried_off_host(tmp_path):
    """``triggers.node`` 是**标记**不是筛子:声明了 execute_sql 的守卫在宿主
    上干净返回,在别的节点上被挂 ``host_mismatch`` —— 丢掉它从外部看和
    "没写"一样(与 validator 的 host_mismatch 同一条处置)。"""
    svc = _svc(tmp_path)
    svc.create({**_GUARD_SKILL, "triggers": {"node": GUARD_HOST}})
    svc.confirm("sql-hygiene")
    on_host = svc.guards_for(GUARD_HOST)
    assert on_host and "host_mismatch" not in on_host[0]
    off_host = svc.guards_for("validate")
    assert off_host and off_host[0]["host_mismatch"] == GUARD_HOST
    out = run_guards(off_host, sql="SELECT id FROM loans")[0]
    assert out.triggered is None and out.none_reason == "host_mismatch"


def test_out_of_domain_variable_is_rejected_at_write_time(tmp_path):
    """拼错的变量名(域外标识符)是**写时**错误,不是一条永远判不了的
    静默守卫 —— ``parse_condition(expr, GUARD_VARIABLES)`` 报位置级错误。"""
    svc = _svc(tmp_path)
    bad = {
        **_GUARD_SKILL,
        "guard": {
            "targets": ["sql"],
            "checks": [_check("select_start == 0")],
        },
    }
    with pytest.raises(ValueError, match=r"guard\.checks\[0\]\.expr"):
        svc.create(bad)


def test_result_domain_variable_is_rejected_at_write_time(tmp_path):
    """``min >= 0`` 是结果域的变量 —— 在 SQL 域是域外标识符,同样 400
    (域与档位一一对应,不是可换乘的)。"""
    svc = _svc(tmp_path)
    bad = {
        **_GUARD_SKILL,
        "guard": {"targets": ["sql"], "checks": [_check("min >= 0")]},
    }
    with pytest.raises(ValueError, match=r"guard\.checks\[0\]\.expr"):
        svc.create(bad)


def test_blocking_requires_reason(tmp_path):
    svc = _svc(tmp_path)
    bad = {
        **_GUARD_SKILL,
        "guard": {"targets": ["sql"], "checks": [
            {**_check(severity="blocking"), "reason": ""},
        ]},
    }
    with pytest.raises(ValueError, match="reason is required"):
        svc.create(bad)


@pytest.mark.parametrize("missing", ["hint_zh", "hint_en"])
def test_blocking_requires_both_hints(tmp_path, missing):
    """拦了你但不告诉你怎么办,等于把守卫变成一面墙(hint 是升级 blocking
    的准入费)。"""
    svc = _svc(tmp_path)
    check = _check(severity="blocking", hint_zh="改法", hint_en="how to fix")
    check.pop(missing)
    bad = {**_GUARD_SKILL, "guard": {"targets": ["sql"], "checks": [check]}}
    with pytest.raises(ValueError, match="hint_zh and hint_en are required"):
        svc.create(bad)


def test_advisory_checks_need_no_hints(tmp_path):
    """推荐纪律是"从 advisory 起步观察" —— 观察档不该被 blocking 的准入费
    挡住。"""
    svc = _svc(tmp_path)
    ok = {
        **_GUARD_SKILL,
        "guard": {"targets": ["sql"], "checks": [
            _check(severity="advisory", reason="观察项"),
        ]},
    }
    assert svc.create(ok)["status"] == "pending"


def test_illegal_severity_rejected_at_write_time(tmp_path):
    svc = _svc(tmp_path)
    bad = {
        **_GUARD_SKILL,
        "guard": {"targets": ["sql"], "checks": [_check(severity="Blocking")]},
    }
    with pytest.raises(ValueError, match="severity must be one of"):
        svc.create(bad)


def test_targets_must_be_sql_domain(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(ValueError, match="guard.target must be one of"):
        svc.create({**_GUARD_SKILL, "guard": {
            "targets": ["result"], "checks": [_check()]}})
    with pytest.raises(ValueError, match="non-empty list"):
        svc.create({**_GUARD_SKILL, "guard": {
            "targets": [], "checks": [_check()]}})


def test_non_host_node_rejected_at_write_time(tmp_path):
    """写 ``triggers.node: validate`` 是一条"声明了但引擎不会执行"的配置 ——
    写入时就拒(校验器与手写文件两条路的出口都要响)。"""
    svc = _svc(tmp_path)
    with pytest.raises(ValueError, match=GUARD_HOST):
        svc.create({**_GUARD_SKILL, "triggers": {"node": "validate"}})


def test_host_node_itself_is_accepted(tmp_path):
    svc = _svc(tmp_path)
    entry = svc.create({**_GUARD_SKILL, "triggers": {"node": GUARD_HOST}})
    assert entry["status"] == "pending"


def test_check_target_validated_at_write_time(tmp_path):
    svc = _svc(tmp_path)
    bad = {
        **_GUARD_SKILL,
        "guard": {"targets": ["sql"], "checks": [_check(target="nowhere")]},
    }
    with pytest.raises(ValueError, match="guard.checks\\[0\\].target"):
        svc.create(bad)


def test_guard_block_is_required_for_guard_tier(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(ValueError):
        svc.create({"name": "hollow", "description": "d", "tier": "guard",
                    "body": "b"})


def test_guard_block_must_be_a_mapping(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(ValueError, match="guard must be a mapping"):
        svc.create({**_GUARD_SKILL, "guard": "nope"})


def test_checks_shape_rejected_at_write_time(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(ValueError, match="guard.checks is required"):
        svc.create({**_GUARD_SKILL, "guard": {"targets": ["sql"], "checks": []}})
    with pytest.raises(ValueError, match="guard.checks must be a list"):
        svc.create({**_GUARD_SKILL, "guard": {"targets": ["sql"],
                                              "checks": "nope"}})


def test_check_shape_rejected_at_write_time(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(ValueError, match=r"guard\.checks\[0\] must be a mapping"):
        svc.create({**_GUARD_SKILL, "guard": {"targets": ["sql"],
                                              "checks": ["junk"]}})
    with pytest.raises(ValueError, match=r"guard\.checks\[0\]\.name is required"):
        svc.create({**_GUARD_SKILL, "guard": {
            "targets": ["sql"], "checks": [{**_check(), "name": ""}]}})
    with pytest.raises(ValueError, match=r"guard\.checks\[0\]\.expr is required"):
        svc.create({**_GUARD_SKILL, "guard": {
            "targets": ["sql"], "checks": [{**_check(), "expr": ""}]}})


def test_guard_field_is_foreign_to_validator_tier(tmp_path):
    """``guard:`` 出现在 validator 档 = 永不生效的死配置 —— 写入时拒,
    比留一份没人看的配置好。"""
    svc = _svc(tmp_path)
    with pytest.raises(ValueError, match="guard is only valid for tier=guard"):
        svc.create({
            "name": "mixed", "description": "d", "tier": "validator",
            "severity": "blocking", "targets": ["result"],
            "checks": [{"expr": "min >= 0"}],
            "guard": {"targets": ["sql"], "checks": [_check()]},
            "body": "b",
        })


def test_validator_fields_are_foreign_to_guard_tier(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(ValueError, match="mode is only valid for tier=validator"):
        svc.create({**_GUARD_SKILL, "mode": "deterministic"})


def test_set_tier_rejects_guard_both_directions(tmp_path):
    """guard 与 validator 一样只能手写 SKILL.md 设置 —— 切换按钮两个方向
    都 400,并把"去哪里改"说清楚。"""
    svc = _svc(tmp_path)
    svc.create({"name": "soft", "description": "d", "body": "b"})
    with pytest.raises(ValueError, match="guard"):
        svc.set_tier("soft", "guard")


def test_org_switch_stops_guard_selection(tmp_path):
    """总开关停用 → 一条守卫都不跑(断言是组织扩展的消费面之一)。"""
    from trove.core.config import AgentConfig

    cfg = AgentConfig(target="mock/model")
    cfg.extensions.org_extensions_enabled = False
    svc = SkillService(tmp_path / ".trove" / "skills", config=cfg)
    svc.create(dict(_GUARD_SKILL))
    svc.confirm("sql-hygiene")
    assert svc.guards_for(GUARD_HOST) == []
    cfg.extensions.org_extensions_enabled = True      # 现场翻回,不重建
    assert len(svc.guards_for(GUARD_HOST)) == 1


def test_guard_draft_via_llm_path_lands_pending(tmp_path):
    """手写 SKILL.md 是 guard 档的授权路径 —— 管理端起草的草稿走同一条确认门。"""
    svc = _svc(tmp_path)
    skills = tmp_path / ".trove" / "skills" / "manual-guard"
    skills.mkdir(parents=True)
    fm = yaml.safe_dump({
        "name": "manual-guard", "description": "d", "tier": "guard",
        "status": "pending",
        "guard": {"targets": ["sql"], "checks": [_check()]},
    }, allow_unicode=True, sort_keys=False).strip()
    (skills / "SKILL.md").write_text(f"---\n{fm}\n---\n\nnote\n",
                                     encoding="utf-8")
    assert svc.guards_for(GUARD_HOST) == []           # pending 不跑
    entry = svc.read_skill("manual-guard")
    assert entry["guard"]["checks"][0]["expr"] == "select_star == 0"
