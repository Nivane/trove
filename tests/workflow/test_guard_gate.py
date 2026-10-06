"""org guard 的执行前门(§04 E2):拦截、放行、回滚与有界终止。

本文件钉住的是**门的三条不变量**,不是守卫本身(守卫的判定在
``tests/services/skills/test_guards.py``):

- **拦的是"不执行"**:一条被拦的 SQL 与"拦了但照跑"在返回值上看起来一样,
  差别只在 connector 有没有被调用 —— 每条拦截断言都配一个 spy;
- **零守卫逐字节不变**:``guards=None``(未装配)与"装了但没命中"两条路径的
  update 必须与改造前一致 —— 这是"加门不改路"的安全带;
- **有界终止**:同一条守卫反复命中必须落进版本链的"无进展"检测并在有界轮次内
  停(§3e / R4),不是烧满共享重试预算。
"""

from __future__ import annotations

import pytest

from trove.core.config import AgentConfig
from trove.services.authz.enforcer import Authorizer
from trove.services.authz.policy import Principal, principal_to_wire
from trove.services.skills.guards import GuardRunner
from trove.services.skills.service import SkillService
from trove.workflow.nodes.analyze_error import make_analyze_error
from trove.workflow.nodes.execute_sql import (
    ORG_GUARD_TAG,
    make_execute_sql,
)
from trove.workflow.state import WorkflowState

DEFAULT = "test_db"


# ── fixtures / spies ─────────────────────────────────────────


class _Result:
    def __init__(self) -> None:
        self.columns = ["n"]
        self.rows = [[1]]
        self.row_count = 1
        self.execution_time_ms = 1.0


class _SpyConnectors:
    """记录执行过的 SQL —— 「拦下了」与「拦下了但照跑」靠它区分。"""

    default_name = DEFAULT

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, sql, datasource=None):
        self.executed.append(sql)
        return _Result()


class _RecordingRunner:
    """记录被问到过什么 —— 用来钉门的**顺序**(授权门之前不该问守卫)。"""

    def __init__(self, verdicts=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.verdicts = verdicts or []

    def check(self, sql, *, dialect="", **ctx):
        self.calls.append((sql, dialect))
        return self.verdicts


def _state(**kwargs) -> WorkflowState:
    defaults = {
        "session_id": "s1",
        "question": "q",
        "sql": "SELECT id FROM students",
        "dialect": "sqlite",
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def _blocking_skill(skill_name: str = "sql-hygiene", **check_overrides) -> dict:
    check = {
        "name": "no-select-star",
        "severity": "blocking",
        "expr": "select_star == 0",
        "reason": "不允许裸查全列",
        "hint_zh": "把 * 展开成显式列",
        "hint_en": "Expand * into explicit columns",
    }
    check.update(check_overrides)
    return {
        "name": skill_name,
        "description": "SQL 结构规范",
        "tier": "guard",
        "guard": {"targets": ["sql"], "checks": [check]},
        "body": "note",
    }


def _advisory_skill(skill_name: str = "prefer-limit") -> dict:
    return _blocking_skill(
        skill_name, name="prefer-limit", severity="advisory",
        expr="has_limit == 1", reason="明细建议限量",
    )


def _runner(tmp_path, *skills) -> GuardRunner:
    svc = SkillService(tmp_path / ".trove" / "skills")
    for skill in skills:
        svc.create(dict(skill))
        svc.confirm(skill["name"])
    return GuardRunner(svc)


def _guard_metric(name: str) -> float:
    """``trove_sql_guard_blocks_total{guard="<name>"}`` 的当前值。"""
    from trove.core.metrics import render_metrics

    prefix = f'trove_sql_guard_blocks_total{{guard="{name}"}}'
    total = 0.0
    for line in render_metrics().decode().splitlines():
        if line.startswith(prefix):
            total += float(line.rsplit(" ", 1)[1])
    return total


@pytest.fixture(autouse=True)
def _clean_guard_metric():
    """计数器是进程级、只增不减 —— 每条用例前后各读一次差值,不数行数。"""
    yield


# ── 拦截(两条路径都拦)──────────────────────────────────────


class TestBlockingGate:
    async def test_fast_path_sql_is_blocked(self, tmp_path):
        """快径(KB 精确命中)交付的 SQL 同样过门 —— 门在图上是路径无关的。"""
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, guards=_runner(tmp_path, _blocking_skill()),
        )
        update = await node(_state(
            sql="SELECT * FROM students", fast_path=True,
        ))
        assert spy.executed == [], "被拦的 SQL 不得被执行"
        assert update["error_feedback"].startswith(ORG_GUARD_TAG)
        assert update["retry_count"] == 1
        assert update["row_count"] == -1
        assert "error" not in update

    async def test_generation_path_sql_is_blocked(self, tmp_path):
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, guards=_runner(tmp_path, _blocking_skill()),
        )
        update = await node(_state(sql="SELECT * FROM students"))
        assert spy.executed == []
        assert update["error_feedback"].startswith(ORG_GUARD_TAG)

    async def test_blocked_hits_land_in_state_for_analyze_error(self, tmp_path):
        """拦截路径也要把 ``guard_hits`` 落进 update —— analyze_error 的
        确定性修正指令按它组装(缺了它就只剩 error_feedback 文本)。"""
        node = make_execute_sql(
            _SpyConnectors(), guards=_runner(tmp_path, _blocking_skill()),
        )
        update = await node(_state(sql="SELECT * FROM students"))
        hit = update["guard_hits"][0]
        assert hit["triggered"] is True
        assert hit["severity"] == "blocking"
        assert hit["name"] == "no-select-star"

    async def test_blocking_message_carries_reason_hint_and_target(self, tmp_path):
        """打回生成方的修正指令 = tag + 守卫名 + reason + hint + TARGET
        (§3d:模型没有可补充的信息,所以 analyze_error 零 LLM)。"""
        node = make_execute_sql(
            _SpyConnectors(), guards=_runner(tmp_path, _blocking_skill()),
        )
        fb = (await node(_state(sql="SELECT * FROM students", lang="zh")))["error_feedback"]
        assert fb.count(ORG_GUARD_TAG) == 1        # 标记语义:一类一次,不是每条一次
        assert "no-select-star" in fb
        assert "不允许裸查全列" in fb
        assert "把 * 展开成显式列" in fb
        assert "TARGET: gen_retrieve" in fb

    async def test_blocking_message_localized(self, tmp_path):
        node = make_execute_sql(
            _SpyConnectors(), guards=_runner(tmp_path, _blocking_skill()),
        )
        fb = (await node(_state(sql="SELECT * FROM students", lang="en")))["error_feedback"]
        assert "Org guards blocked this SQL" in fb
        assert "Expand * into explicit columns" in fb

    async def test_multi_guard_block_marks_once_names_both(self, tmp_path):
        a = _blocking_skill("no-star", name="no-star")
        b = _blocking_skill(
            "must-limit", name="must-limit", expr="has_limit == 1",
            reason="明细必须限量", hint_zh="加 LIMIT", hint_en="Add a LIMIT",
        )
        node = make_execute_sql(
            _SpyConnectors(), guards=_runner(tmp_path, a, b),
        )
        fb = (await node(_state(sql="SELECT * FROM students")))["error_feedback"]
        assert fb.count(ORG_GUARD_TAG) == 1
        assert "no-star" in fb and "must-limit" in fb

    async def test_budget_exhausted_degrades(self, tmp_path):
        """预算耗尽 → 优雅降级(error),不再打回生成 —— 与执行失败同一条
        出口(拦截绝不能变成无限循环)。"""
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, max_retries=3, guards=_runner(tmp_path, _blocking_skill()),
        )
        update = await node(_state(sql="SELECT * FROM students", retry_count=3))
        assert spy.executed == []
        assert update["error"].startswith(ORG_GUARD_TAG)
        assert "error_feedback" not in update
        assert update["guard_hits"]  # 判定物照落,错误卡片之外仍可审计

    async def test_block_increments_counter_by_guard_name(self, tmp_path):
        before = _guard_metric("no-select-star")
        node = make_execute_sql(
            _SpyConnectors(), guards=_runner(tmp_path, _blocking_skill()),
        )
        await node(_state(sql="SELECT * FROM students"))
        assert _guard_metric("no-select-star") == before + 1

    async def test_advisory_hit_alongside_blocking_still_blocks(self, tmp_path):
        """advisory 不是豁免:同一轮里 blocking 命中照样拦,advisory 命中
        照常入 ``guard_hits``(两条判定都留痕)。"""
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, guards=_runner(tmp_path, _blocking_skill(), _advisory_skill()),
        )
        update = await node(_state(sql="SELECT * FROM students"))
        assert spy.executed == []
        by_name = {h["name"]: h for h in update["guard_hits"]}
        assert by_name["no-select-star"]["triggered"] is True
        assert by_name["prefer-limit"]["severity"] == "advisory"


# ── advisory / 判不了:绝不拦 ────────────────────────────────


class TestNotBlocked:
    async def test_advisory_hit_does_not_block_and_lands_hits(self, tmp_path):
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, guards=_runner(tmp_path, _advisory_skill()),
        )
        update = await node(_state(sql="SELECT id FROM students"))
        assert len(spy.executed) == 1                     # 照常执行
        assert "error" not in update
        assert update["error_feedback"] == ""             # 成功路径清空反馈
        hit = update["guard_hits"][0]
        assert hit["triggered"] is True
        assert hit["severity"] == "advisory"

    async def test_compliant_sql_verdicts_are_recorded_but_not_hits(self, tmp_path):
        """合规 = 无声:判定照落(triggered False),用户屏幕一个字都不出
        (output 只渲染 advisory 的明确命中)。"""
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, guards=_runner(tmp_path, _blocking_skill()),
        )
        update = await node(_state(sql="SELECT id, name FROM students"))
        assert [h["triggered"] for h in update["guard_hits"]] == [False]
        assert len(spy.executed) == 1

    async def test_unparseable_sql_never_blocks(self, tmp_path):
        """判不了(SQL 解析失败)绝不拦 —— 拿不准就拦下正确结果比不检查更坏。"""
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, guards=_runner(tmp_path, _blocking_skill()),
        )
        update = await node(_state(sql="((("))
        assert update["guard_hits"][0]["triggered"] is None
        assert "error" not in update
        assert len(spy.executed) == 1

    async def test_stale_hits_are_rewritten_every_round(self, tmp_path):
        """每轮**全量重写**该键:上一轮的判词不得活到交付答案上(不写键 =
        陈旧判定渲染成一条针对它从未检查过的 SQL 的告警)。"""
        spy = _SpyConnectors()
        node = make_execute_sql(
            spy, guards=_runner(tmp_path, _blocking_skill()),
        )
        state = _state(
            sql="SELECT id, name FROM students",
            guard_hits=[{"name": "stale", "severity": "advisory",
                         "triggered": True, "reason": "上一轮的判词",
                         "hint": "", "target": "gen_retrieve"}],
        )
        update = await node(state)
        assert "stale" not in [h["name"] for h in update["guard_hits"]]
        assert [h["triggered"] for h in update["guard_hits"]] == [False]


# ── 零守卫:逐字节不变的安全带 ──────────────────────────────


class TestZeroGuardsUnchanged:
    async def test_no_runner_omits_the_key_entirely(self):
        """``guards=None``(未装配)→ 与改造前同形状:update 里没有 guard_hits。"""
        node = make_execute_sql(_SpyConnectors())
        update = await node(_state(sql="SELECT id FROM students"))
        assert "guard_hits" not in update

    async def test_no_runner_and_empty_runner_differ_only_by_the_key(self, tmp_path):
        """装了守卫但一条都没有(空清单)→ 未装配路径 + 一个空键,其余逐字节
        相同 —— 这是"加门不改路"的安全带。"""
        bare = await make_execute_sql(_SpyConnectors())(_state())
        with_runner = await make_execute_sql(
            _SpyConnectors(), guards=_runner(tmp_path),
        )(_state())
        assert with_runner.pop("guard_hits") == []
        assert with_runner == bare

    async def test_error_passthrough_does_not_consult_guards(self):
        """上游已出错 → 节点直通,守卫**不跑**(省一次 AST 解析,也别在
        陈旧 SQL 上产出判词)。"""
        runner = _RecordingRunner()
        node = make_execute_sql(_SpyConnectors(), guards=runner)
        assert await node(_state(error="upstream failed")) == {}
        assert runner.calls == []


# ── 门的顺序(守卫在授权门之后)──────────────────────────────


class TestGateOrdering:
    async def test_authz_denial_wins_and_guards_are_not_consulted(self):
        """被授权拒的 SQL 不该再去判守卫:授权判定在**前面**。"""
        runner = _RecordingRunner()
        authorizer = Authorizer(
            declared_tables=lambda _ds: frozenset({"students"}), mode="enforce",
        )
        spy = _SpyConnectors()
        node = make_execute_sql(spy, authorizer=authorizer, guards=runner)
        update = await node(_state(
            sql="SELECT * FROM students",
            principal=principal_to_wire(
                Principal(subject="7", grants=frozenset({"other_db"}))),
        ))
        assert "AUTHZ" in update["error"]
        assert runner.calls == [], "授权门之后的门不该先于它运行"
        assert spy.executed == []

    async def test_readonly_firewall_wins_and_guards_are_not_consulted(self):
        """只读防火墙在更前面:写语句直奔硬拒,守卫不跑。"""
        runner = _RecordingRunner()
        node = make_execute_sql(_SpyConnectors(), guards=runner)
        update = await node(_state(sql="UPDATE students SET grade = 'A'"))
        assert "SQL_GUARD" in update["error"]
        assert runner.calls == []


# ── analyze_error:零 LLM 的确定性回滚(§3d)──────────────────


class _NoLLM:
    async def chat(self, *a, **k):
        raise AssertionError("org-guard analysis must not call the LLM")


def _guard_feedback(lang: str = "zh") -> str:
    tail = "把 * 展开成显式列" if lang == "zh" else "Expand * into explicit columns"
    head = ("组织守卫拦下了这条 SQL(执行前断言,1 条命中):" if lang == "zh"
            else "Org guards blocked this SQL (pre-execution assertions, 1 hit):")
    return (
        f"{ORG_GUARD_TAG} {head}\n"
        "- guard=no-select-star: 不允许裸查全列\n"
        f"  hint: {tail}\n"
        "  TARGET: gen_retrieve"
    )


def _guard_hits() -> list[dict]:
    return [{
        "name": "no-select-star", "severity": "blocking", "triggered": True,
        "reason": "不允许裸查全列", "hint": "把 * 展开成显式列",
        "target": "gen_retrieve",
    }]


def _analysis_state(**kwargs) -> WorkflowState:
    defaults = {
        "session_id": "s1", "question": "q", "lang": "zh",
        "sql": "SELECT * FROM students",
        "error_feedback": _guard_feedback(), "guard_hits": _guard_hits(),
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


class TestAnalyzeErrorOrgGuard:
    async def test_deterministic_zero_llm_rollback(self):
        node = make_analyze_error(_NoLLM(), AgentConfig(target="m"))
        update = await node(_analysis_state())
        assert "error" not in update                    # 非死胡同
        assert update["rollback_target"] == "gen_retrieve".replace(
            "gen_retrieve", "gen_sql")                  # 归一:gen_retrieve → 生成链入口
        assert update["fix_mode"] == "fixer"
        assert "不允许裸查全列" in update["error_analysis"]
        assert "把 * 展开成显式列" in update["error_analysis"]
        assert "TARGET: gen_sql" in update["error_analysis"]

    async def test_analysis_is_localized(self):
        """指令语言跟问题语言走:hint 在 execute_sql 落 guard_hits 时已按
        ``state.lang`` 本地化,这里原样转发(不二次翻译)。"""
        hits = _guard_hits()
        hits[0]["hint"] = "Expand * into explicit columns"
        node = make_analyze_error(_NoLLM(), AgentConfig(target="m"))
        update = await node(_analysis_state(
            lang="en", error_feedback=_guard_feedback("en"), guard_hits=hits,
        ))
        assert "Org guards blocked this SQL" in update["error_analysis"]
        assert "Expand * into explicit columns" in update["error_analysis"]

    async def test_first_round_counts_as_progress(self):
        node = make_analyze_error(_NoLLM(), AgentConfig(target="m"))
        update = await node(_analysis_state())
        assert update["last_progress"] == "first"
        assert update["no_progress_rounds"] == 0
        assert update["sql_versions"][0]["sig"] == "exec-error"
        assert ORG_GUARD_TAG in update["sql_versions"][0]["error"]

    async def test_declared_target_is_respected(self):
        hits = _guard_hits()
        hits[0]["target"] = "query_sketch"
        node = make_analyze_error(_NoLLM(), AgentConfig(target="m"))
        update = await node(_analysis_state(
            guard_hits=hits,
            error_feedback=_guard_feedback().replace(
                "TARGET: gen_retrieve", "TARGET: query_sketch"),
        ))
        assert update["rollback_target"] == "query_sketch"
        assert "TARGET: query_sketch" in update["error_analysis"]

    async def test_missing_hits_falls_back_to_error_feedback_text(self):
        """``guard_hits`` 缺席(旧检查点)→ 直接转发 error_feedback 全文 ——
        判词与 hint 在里面,别把信息丢成一句"被守卫拦了"。"""
        node = make_analyze_error(_NoLLM(), AgentConfig(target="m"))
        update = await node(_analysis_state(guard_hits=[]))
        assert "把 * 展开成显式列" in update["error_analysis"]

    async def test_same_guard_hit_again_is_invalid_progress(self):
        """同一条守卫再次命中(与上一轮记录的错误逐字相同)→ "无进展",
        且按梯子升一档(gen_sql → query_sketch)。"""
        prev = {"sig": "exec-error", "error": _guard_feedback(), "sql": "SELECT * FROM students"}
        node = make_analyze_error(_NoLLM(), AgentConfig(target="m"))
        update = await node(_analysis_state(
            sql_versions=[prev], last_rollback_target="gen_sql",
        ))
        assert update["last_progress"] == "invalid"
        assert update["rollback_target"] == "query_sketch"
        assert update["no_progress_rounds"] == 1

    async def test_repeated_same_failure_terminates_within_bound(self):
        """反复撞同一条守卫 → ``MAX_NO_PROGRESS_ROUNDS`` 内终止(§3e / R4):
        同一条守卫不许烧满共享重试预算。"""
        from trove.workflow.nodes.analyze_error import MAX_NO_PROGRESS_ROUNDS

        prev = {"sig": "exec-error", "error": _guard_feedback(), "sql": "SELECT * FROM students"}
        node = make_analyze_error(_NoLLM(), AgentConfig(target="m"))
        update = await node(_analysis_state(
            sql_versions=[prev],
            no_progress_rounds=MAX_NO_PROGRESS_ROUNDS - 1,
        ))
        assert update["error"]
        assert "无进展" in update["error"]
        assert update["no_progress_rounds"] >= MAX_NO_PROGRESS_ROUNDS

    async def test_top_of_ladder_degrades_gracefully(self):
        """升到顶(声明 schema_linking 且上一轮就是它)→ 优雅降级,不空转。"""
        hits = _guard_hits()
        hits[0]["target"] = "schema_linking"
        prev = {
            "sig": "exec-error",
            "error": _guard_feedback().replace("TARGET: gen_retrieve", "TARGET: schema_linking"),
            "sql": "SELECT * FROM students",
        }
        node = make_analyze_error(_NoLLM(), AgentConfig(target="m"))
        update = await node(_analysis_state(
            guard_hits=hits, error_feedback=prev["error"],
            sql_versions=[prev], last_rollback_target="schema_linking",
        ))
        assert update["error"]
        assert "无档可升" in update["error"]
