"""效果验证器 —— 行动的闭环验收(包外执行面,唯一持有连接器的一侧)。

行动柱(B5)把判定触发送到系统外;判定柱(B2/B3)把「为什么」说清楚。
缺的最后一环是**回来的路**:送出去 N 天之后,那条度量到底动了没有。
这就是本模块。

**为什么 verifier 在这里而不是 action 包里**:action 包的姿态是结构性
的 —— 构造签名不收连接器、整包不 import 数据源(见
``tests/services/action/test_action_readonly_posture.py``)。测量要读业务
库,所以执行面必须留在包外,由 ``ActionService`` 以**可注入 callable**
的形式接收(``verifier=``)。这与 ``ActionDispatcher`` 的 transport 同款
姿态:能力从外面给,包本身够不到。

**契约**:``async (proposal) -> dict | None``

  - ``None`` = 「**还不能测**」:规则的测量期还没走到行动之后(期还没有
    滚过行动日)→ 本次跳过,下次 sweep 再试。这不是失败,也不该写任何行;
  - ``dict`` = 一次测量记录。带 ``error`` 键 = 一次**响亮的失败**(规则
    被删/被停用、窗口不可解析、编译 MISS、查询失败、tie 不回组)—— 由
    ``measure_due`` 落成 error outcome,一行,不重试;
  - 异常 = 未预期的故障,同样被 ``measure_due`` 接成 error outcome。

**测量口径(每一条都是刻意的)**:

  1. **窗口 = 规则自己的窗口,在测量时按它的时钟重算** —— 与那条
     触发它的判定用的是同一套 period 语义(grain 对齐 / same_phase /
     过滤口径),不发明第二套时间词汇。``window`` 就是测量的「行动后
     期间」,历史块是它的行动前;
  2. **期必须滚过行动日**(``window_end > 行动日``)才测 —— 行动当天
     就在期内的测量测的是「行动前的数」,不是效果。没滚过 → 返回
     ``None``,下个小时再看;
  3. **只测发出消息的那一组**:提案在创建时就把「消息点名的那一组」
     钉进了 ``evidence_refs["group"]``(propose 那一刻 ``primary_group``
     的答案,与消息内容同源)。验收读它,不重算 —— 重算会读到**现在**
     的证据,而验收要的是**当时**点的那一组。per_dimension 规则取不到
     组 → ``group_unresolved`` **error**(拿别的总体冒充被点名的组,
     正是这一层最不能出的错);聚合规则的组标签本来就是 ``""``;
  4. **有因果声明才做净效应**:规则声明了 ``causal.control`` 且对照帧
     取得到 → ``att``(2×2,前值两侧都用前窗稳健中心 —— 与 ITS 的
     ``pre.center`` 同一个量,单块噪声不主导一个 2×2);取不到 →
     ``causal: "unavailable"`` + 原因,ITS 结论原样保留(对照不可用是
     常见形态,不是错);
  5. **一次测量,一个结局**。测量不是重试循环:verifier 恢复之后**不会**
     回填 —— 那时窗口已经滚走,回填出来的会是**另一份测量**(测的是
     另一个期间),拿它冒充当初的验收结果是这一层最不能出的错。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from trove.core.logging import get_logger
from trove.services.analysis.effect import attribute_effect, measure_effect
from trove.services.analysis.stats import median
from trove.services.decision.causal_source import (
    causal_lookback,
    fetch_control_series,
    split_post,
)
from trove.services.decision.rules import RuleError, Seasonal, rule_rev
from trove.services.decision.series_source import fetch_block_series
from trove.services.decision.service import DecisionError

logger = get_logger(__name__)

#: 观测量:``(proposal) -> 测量记录 | None``(见模块 docstring 的契约)。
Verifier = Callable[[Any], Awaitable[dict[str, Any] | None]]


def _refs(proposal: Any) -> dict[str, Any]:
    """提案的证据指针(``evidence_refs``;缺失/坏形 → 空 dict)。"""
    refs = getattr(proposal, "evidence_refs", None)
    return refs if isinstance(refs, dict) else {}


def _exposure_days(window: Any, left_at: str) -> int | None:
    """测量期对行动的实际暴露天数(行动日之后窗口还有几天;算不出 → None)。

    审计用:一个 10 月 5 日发出的行动,在「上个月」这种窗口下最早会被
    10 月 31 日之后的测量覆盖 —— 31 天里 26 天在行动之后。读者看得见
    这个数,就看得见测量滞后有多少是「期间还没滚完」。
    """
    try:
        start = date.fromisoformat(str(window[0]))
        end = date.fromisoformat(str(window[1]))
        left = date.fromisoformat(str(left_at)[:10])
    except (TypeError, ValueError, IndexError):
        return None
    base = max(start, left)
    return max((end - base).days, 0)


def _error(reason: str, **fields: Any) -> dict[str, Any]:
    return {"error": str(reason)[:200], **fields}


def make_verifier(decision: Any, *, kb: Any) -> Verifier:
    """装配一个 verifier —— ``decision`` = ``DecisionService``(语义解析
    与只读执行契约都从它复用),``kb`` = ``KbService``(规则从它读)。

    复用 ``DecisionService`` 的私有解析口(``_dialect`` / ``_model_for`` /
    ``_provider_for`` / ``_query_runner`` / ``_resolve_window`` / ``_compile``)
    而不是复写一遍:测量与判定读到不同口径的数字,正是闭环验收最不能
    出的错 —— 同一批 helper 是这件事的结构性保证。
    """
    async def verify(proposal: Any) -> dict[str, Any] | None:
        rule_id = str(getattr(proposal, "rule_id", "") or "")
        datasource = str(getattr(proposal, "datasource", "") or "")
        if not rule_id:
            # 手动提案(origin_kind=manual)没有规则可测 —— 没有口径就没有测量。
            return _error("no_rule")
        if kb is None:
            return _error("no_kb")
        try:
            doc = kb.load_decisions(datasource)
        except RuleError as e:
            return _error(f"decisions_unreadable: {e}")
        rule = next((r for r in doc.rules if r.id == rule_id), None)
        if rule is None:
            return _error(f"rule_missing: {rule_id}")
        if not getattr(rule, "enabled", True):
            return _error(f"rule_disabled: {rule_id}")

        left_at = str(getattr(proposal, "dispatched_at", "")
                      or getattr(proposal, "decided_at", "") or "")
        if not left_at:
            return _error("no_dispatch_time")

        # 测哪一组:propose 那一刻钉下的答案(见 docstring 第 3 条)。
        refs = _refs(proposal)
        group = str(refs.get("group") or "")
        if rule.subject.dimensions and not group:
            return _error("group_unresolved")
        if not rule.subject.dimensions:
            group = ""

        anchor = date.today()
        try:
            window, _baseline = decision._resolve_window(rule, anchor)
            dialect = await decision._dialect(datasource)
            model = decision._model_for(datasource, dialect)
        except DecisionError as e:
            return _error(f"window_unresolved: {e}")
        if window is None:
            # 没有 window 的规则(纯 literal 基线)没有测量期 —— 无法验收。
            return _error("no_window")
        if str(window[1]) <= left_at[:10]:
            return None  # 期还没滚过行动日 —— 下个小时再看

        metric = rule.subject.metrics[0] if rule.subject.metrics else ""
        if not metric:
            return _error("no_metric")
        try:
            info = await decision._compile(rule, model, dialect, datasource,
                                           window)
        except (DecisionError, RuleError) as e:
            return _error(f"compile_failed: {e}")
        time_field = str(info.get("time_field") or "")
        matched = [str(d) for d in (info.get("datasets") or [])]
        if not time_field:
            return _error("no_time_field")

        seasonal = rule.seasonal or Seasonal()
        try:
            series = await fetch_block_series(
                semantic_layer=decision._provider_for(datasource, dialect),
                runner=decision._query_runner(datasource),
                datasource=datasource, dialect=dialect, metric_name=metric,
                matched=matched, filters=rule.subject.filters, window=window,
                time_field=time_field, grain=seasonal.grain,
                mode=seasonal.mode, lookback=seasonal.lookback,
                dimensions=list(rule.subject.dimensions),
                include_current=True,
            )
        except Exception as e:  # noqa: BLE001 — 一次失败 = 一行 error，不是崩
            return _error(f"query_failed: {e}")
        if series is None:
            return _error("series_unavailable")

        group_missing = bool(rule.subject.dimensions) and group not in series.by_dim
        pre_blocks, post_value = split_post(series.values(group))

        effect = measure_effect(
            pre_blocks, [] if post_value is None else [post_value],
            k=float(seasonal.k),
        )
        if group_missing:
            effect["degraded"] = list(effect["degraded"]) + ["group_missing"]

        # ── 因果腿(声明了才做;取不到对照只降级,绝不上抛) ──
        causal_reason = "no_control"
        control = getattr(rule.causal, "control", None)
        if rule.causal is not None and control is not None:
            frame = None
            try:
                frame = await fetch_control_series(
                    semantic_layer=decision._provider_for(datasource, dialect),
                    runner=decision._query_runner(datasource),
                    datasource=datasource, dialect=dialect, metric_name=metric,
                    matched=matched, subject_filters=rule.subject.filters,
                    control=control, window=window, time_field=time_field,
                    grain=seasonal.grain, mode=seasonal.mode,
                    lookback=causal_lookback(seasonal, rule.causal),
                )
            except Exception as e:  # noqa: BLE001 — 对照取数失败只降级，不上抛
                logger.info("outcome: control fetch failed for %s: %s",
                            rule_id, e)
            c_pre: list[Any] = []
            c_post = None
            got_frame = frame is not None and frame.series is not None
            if got_frame:
                label = frame.label if frame.mode == "dim" else ""
                c_pre, c_post = split_post(frame.series.values(label))
            effect = attribute_effect(
                effect,
                treated_pre=effect["pre"]["center"],
                treated_post=effect["post"]["center"],
                control_pre=median(c_pre),
                control_post=c_post,
            )
            # 「对照帧取不到」与「对照帧有、值不全」是两种修法:前者查
            # 对照组声明/取数(日志里有异常原文),后者查对照组有没有数据。
            causal_reason = (str(effect.get("causal_reason") or "")
                             if got_frame else "control_unavailable")

        observed: dict[str, Any] = {
            **effect,
            "proposal_id": str(getattr(proposal, "id", "") or ""),
            "rule_id": rule_id,
            # rule_rev = **测量所依据的规则内容**(当前版本);判定当时那一版
            # 另存 rule_rev_judged —— 两者不同说明规则在丈量之前被改过,
            # 回评分桶按前者(测量属于哪一版的口径,就记在哪一版账上)。
            "rule_rev": rule_rev(rule),
            "rule_rev_judged": str(refs.get("rule_rev") or ""),
            "metric": metric,
            "group": group,
            "group_missing": group_missing,
            "window": [str(window[0]), str(window[1])],
            "grain": series.grain,
            "mode": series.mode,
            "blocks": len(series.blocks),
            "unmatched": series.unmatched,
            "left_at": left_at,
            "exposure_days": _exposure_days(window, left_at),
            "sql": series.sql,
            "causal": str(effect.get("causal") or "unavailable"),
            "causal_reason": causal_reason,
            "att": effect.get("att"),
        }
        return observed

    return verify
