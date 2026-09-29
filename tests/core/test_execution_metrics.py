"""执行画像的三条指标 —— 形状契约(设计 §9.2 / R1 / R2)。

放在 ``core`` 是因为它们与 HTTP / LLM / SQL 三条既有指标同一个进程级注册表、
同一个 ``/v1/metrics`` 出口(单进程 serve,拉模式);这里钉的是**形状**:

* 名字与标签集(运维的告警表达式建在上面,改了就是破坏性);
* ``kill == ""`` **不成一条序列**(「没试过」不是终止结果的一种);
* 客户端缺失时全部 no-op —— 指标永远不能拖垮请求路径。

**记录点**在哪个节点(以及为什么不能在服务层)由
``tests/workflow/test_execute_sql_budget.py::TestExecutionMetrics`` 钉:那是路径
问题,这里是形状问题。

断言一律读 :func:`render_metrics` 的文本 —— 与运维抓到的同一份东西,顺带钉住
标签确实渲染出来了;不碰私有 ``_value``。
"""

from __future__ import annotations

from trove.core import metrics as metrics_mod
from trove.core.metrics import (
    record_authz_deny,
    record_masking_applied,
    record_sql_budget_decision,
    record_sql_degraded,
    record_sql_kill,
    render_metrics,
)


def _series(name: str, **labels: str) -> list[str]:
    """文本里匹配 ``name`` + 全部给定标签的行(标签顺序无关)。

    只认 ``name{...}``:exposition 里每个计数器还带一条 ``name_created`` 的
    伴随序列,按前缀匹配会把它一起捞进来。
    """
    text = render_metrics().decode()
    return [
        line for line in text.splitlines()
        if line.startswith(name + "{")
        and all(f'{k}="{v}"' in line for k, v in labels.items())
    ]


class TestSqlBudgetMetrics:
    """三条指标的标签集 —— 数据源名 + 枚举值,低基数。"""

    def test_a_decision_carries_source_and_verdict(self):
        """决策计数要能回答「哪一档判的、判成什么」——
        ``source`` / ``verdict`` 各自的值域见 ``CostEstimate`` / ``BudgetDecision``。"""
        record_sql_budget_decision("p5_unit_db", "conservative", "degrade")
        assert _series(
            "trove_sql_budget_decisions_total",
            datasource="p5_unit_db", source="conservative", verdict="degrade",
        )

    def test_a_degradation_is_counted_under_the_datasource(self):
        record_sql_degraded("p5_unit_db")
        assert _series("trove_sql_degraded_total", datasource="p5_unit_db")

    def test_a_kill_result_is_counted_by_result(self):
        record_sql_kill("p5_unit_db", "kill_unsupported")
        assert _series(
            "trove_sql_kill_total", datasource="p5_unit_db", result="kill_unsupported",
        )

    def test_an_empty_kill_is_not_a_result(self):
        """``""`` = 没试过(没装终止器 / 没超时),**不成一条序列**。

        记一个空串会让「我们根本没接这条轨」混进结果分布 —— 而它与「这个方言
        不支持终止」对运维的含义相反(前者是我们没接,后者是库不给这个能力),
        与 ``kill`` 字段的三态是同一条纪律。
        """
        record_sql_kill("p5_no_kill_db", "")
        assert _series("trove_sql_kill_total", datasource="p5_no_kill_db") == []

    def test_an_unnamed_datasource_does_not_produce_a_blank_label(self):
        """空数据源名折成 ``default`` —— 与 ``record_sql`` 同一处理。

        空串在 exposition 里是合法的,但 ``datasource=""`` 与「真的叫 default 的
        那条数据源」在告警里没法区分,而前者通常意味着装配漏了一步。
        """
        record_sql_degraded("")
        assert _series("trove_sql_degraded_total", datasource="default")


class TestNoClient:
    """``prometheus_client`` 缺席(精简安装)→ 全部 no-op,不抛。

    这是模块级纪律,不是这三个指标独有的:指标是观测,**不该成为依赖**。
    """

    def test_every_helper_is_a_noop_without_the_client(self, monkeypatch):
        monkeypatch.setattr(metrics_mod, "_HAVE_CLIENT", False)

        record_sql_budget_decision("p5_unit_db", "explain", "allow")
        record_sql_degraded("p5_unit_db")
        record_sql_kill("p5_unit_db", "kill_sent")

        assert render_metrics() == b""  # 出口也退化成空载荷,不是半份文本

    def test_a_broken_client_does_not_escape(self, monkeypatch):
        """计数本身炸了也只留一条 debug —— 与既有 helper 同一条兜底。

        记录点全在**用户请求路径**上(执行节点),让一个计数器把一次查询带走是
        最没有收益的失败方式。
        """
        class _Exploding:
            def labels(self, **kwargs):
                raise RuntimeError("registry is on fire")

        monkeypatch.setattr(metrics_mod, "SQL_DEGRADED", _Exploding())
        record_sql_degraded("p5_unit_db")  # 不抛即通过


class TestMaskingMetrics:
    """脱敏指标:`{field, mode}` 两组低基数标签(设计 §9.2 / §5.5)。

    标签选 ``field`` 而不是行数/值,是因为运维要回答的是「这台机器上还有哪些列
    在明文进出」—— 按行或按值记都答不了这个。字段名来自语义层声明(人工维护),
    不来自用户输入,基数可控。
    """

    def test_a_rewrite_carries_field_and_mode(self):
        record_masking_applied("p4_phone_a", "partial")

        assert _series(
            "trove_masking_applied_total", field="p4_phone_a", mode="partial",
        )

    def test_an_empty_report_is_not_a_rewrite(self):
        """空字段名/空模式不成序列 —— 它只可能来自一个坏报告。

        与 ``record_sql_kill`` 的 ``""`` 同一条纪律:让「没发生」混进分布,
        运维读到的每一个数字都要打折。
        """
        record_masking_applied("", "partial")   # 没有字段名
        record_masking_applied("p4_phone_b", "")  # 没有模式

        assert _series("trove_masking_applied_total", field="", mode="partial") == []
        assert _series("trove_masking_applied_total", field="p4_phone_b") == []

    def test_bypass_is_not_counted_here(self):
        """``bypass`` 不进这个计数器 —— 它走审计(§6.3),答的是「谁看过原文」。

        这里没有 ``mode="bypass"`` 这种序列:模式的值域是 ``partial`` /
        ``hash`` / ``null``(声明面的事实),而 bypass 是**运行期的一次决定**,
        两者混在一个系列里,「有多少列在脱敏」这个数会被决策次数污染。
        """
        record_masking_applied("p4_phone_c", "bypass")

        assert _series("trove_masking_applied_total", field="p4_phone_c") == []


class TestAuthzDenyMetrics:
    """授权拒绝指标:``{reason}`` 一组低基数标签(设计 §9.2 / P5)。

    标签取 ``reason`` 而不是表名/SQL:值是**闭集**(``AuthzDecision.reason`` 的
    四个取值 + 拒绝的语义),而表名与 SQL 文本来自用户输入,基数是无限的。
    这条与 ``record_sql_budget_decision`` 同一条纪律:枚举进标签,原文不进。

    ``allowed`` 那一侧**不在这里** —— warn 期的 A3 判定记的是
    ``narrowed_tables``(§8.2 要「先跑一周收集哪些表会被拒」),它的出口是日志
    与 ``state.authz_decision``,不是拒绝计数:把「放行了但记了一笔」算进拒绝,
    运维读到的告警率里会混进一半根本没被拦的查询。
    """

    def test_a_deny_carries_its_reason(self):
        record_authz_deny("no_principal")

        assert _series("trove_authz_deny_total", reason="no_principal")

    def test_every_declared_reason_fits_the_label_domain(self):
        """值域内的每个 reason 都记得进去 —— 守卫不能顺手吃掉真话。"""
        for reason in sorted(metrics_mod.AUTHZ_DENY_REASONS):
            record_authz_deny(reason)
            assert _series("trove_authz_deny_total", reason=reason)

    def test_an_unknown_reason_is_not_a_deny(self):
        """域外的 reason 不记 —— 它只可能来自一个新加的原因没同步到这里。

        与 ``MASKING_MODES`` 同一条纪律:值域是这个计数器定义的,服务层新增
        原因时**这里要有意识地跟着改一次**,而不是让一个自由字符串变成标签
        (那是基数失控的入口)。
        """
        record_authz_deny("p5_unknown")
        record_authz_deny("")

        assert _series("trove_authz_deny_total", reason="p5_unknown") == []
        assert _series("trove_authz_deny_total", reason="") == []
