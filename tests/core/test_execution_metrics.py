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
