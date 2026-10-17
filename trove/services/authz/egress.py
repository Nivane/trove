"""LLM 数据出境治理 —— 工具回喂与提示样例的结构化脱敏。

**为什么需要这一层。** 生成阶段的 ``probe_query`` / ``check_result`` 会真实
执行草稿 SQL,``search_values`` 会检索真实值,这些结果经
``agent_loop._model_observation`` 回喂模型。交付前的 ``masking`` 节点发生在
**最终执行之后**——它保护的是用户界面,管不到模型这个接收方。本模块把同一份
语义模型 ``mask`` 声明复用到那个更早的出口。

三个刻意选择
------------

1. **与 ``masking.enabled`` 解耦。** 那个开关关的是交付前脱敏;关掉它不等于
   允许把已声明 ``mask`` 的字段原文发给模型。egress 只看 ``egress.enabled``
   与 ``egress.redact_tool_results``,复用同一套声明与同一套掩码实现
   (:class:`~trove.services.authz.masking.Masker` /
   :func:`~trove.services.authz.masking.mask_values`),不写第二份。
2. **``principal=None``。** 工具回喂没有「谁能看原文」的授权面:admin 也不能
   未经显式 egress 配置把声明字段发给模型。``Masker`` 在 principal 为 None 时
   照常脱敏(安全方向是多脱敏),这正是要的。
3. **``strict=True``(仅结果集路径)。** 交付面在拿不到投影位置时按结果列名
   退化匹配,``SELECT email AS phone`` 这类别名会漏;出口不接受这个边界 ——
   位置信息不可用且有 ``mask`` 声明 → 抛 ``MaskingError``,整条观测扣留。

失败方向
--------

**扣留,不回落原文。** 结构异常 / 声明非法 / hash salt 解析不出 / 位置信息
不可用 → 观测整条替换为 :data:`WITHHELD_OBSERVATION`(工具路径),few-shot
样例值全部剥掉并报告 ``withheld``(提示词路径)。日志与 ``tool_history`` 仍保留
原文 —— 审计现场是有意的,egress 管的是模型接收方。

``check_result`` 只回规则文本(``workflow/rules.py`` 的 reason 只插值
``row_count`` 与列名,不插值单元格值),因此**不做文本替换**:替换会给「已
覆盖」的错觉,真正的门在执行前的列拒绝(Phase 2)。
"""

from __future__ import annotations

from typing import Any, Callable

from trove.core.config import AgentConfig, EgressConfig
from trove.core.logging import get_logger
from trove.core.metrics import record_egress_redaction
from trove.services.authz.masking import Masker, MaskingError, mask_values
from trove.services.semantic_layer.models import SemanticModel

__all__ = [
    "DATA_TOOLS",
    "EgressRedactor",
    "WITHHELD_OBSERVATION",
    "egress_entry",
    "merge_egress_reports",
]

log = get_logger(__name__)

#: 观测里回填的扣留标记。模型看到它应当换一种验证方式,而不是把标记当数据。
WITHHELD_OBSERVATION = "[ERR:EGRESS_REDACT] observation withheld"

#: 会携带真实数据值的工具(闭集)。集合之外的工具原样通过。
DATA_TOOLS = frozenset({"probe_query", "search_values", "check_result"})

#: 脱敏发生时的观测注记:让模型知道样例值是掩码,不要拿掩码串去 WHERE。
SENSITIVE_NOTE = "sensitive field values are redacted"


def _split_ref(ref: str) -> tuple[str, str]:
    """``loan.region`` → ``("loan", "region")``;无前缀 → ``("", ref)``。"""
    text = str(ref or "").strip()
    if "." not in text:
        return "", text
    table, _, column = text.rpartition(".")
    return table.strip(), column.strip()


class EgressRedactor:
    """工具观测 / few-shot 样例 → 脱敏副本 + 审计报告(零 LLM)。

    ``config`` 可以是 ``AgentConfig`` 或零参 callable(管理端热改活对象时
    用 callable 读现值)。``semantic_provider`` 缺失 = 没有声明面(原样通过);
    provider 读失败 = **判不了有没有声明** → ``MaskingError``(扣留方向)。
    """

    def __init__(
        self,
        semantic_provider: Any | None = None,
        config: AgentConfig | Callable[[], AgentConfig] | None = None,
    ) -> None:
        self._semantic = semantic_provider
        self._config = config

    # ── 配置 / 模型读取 ────────────────────────────────────────

    def _agent_config(self) -> AgentConfig:
        cfg = self._config
        if callable(cfg):
            try:
                cfg = cfg()
            except Exception:
                # 读不到现值 → 用默认(config.egress 默认开),不是关。
                return AgentConfig()
        return cfg if isinstance(cfg, AgentConfig) else AgentConfig()

    def _egress(self) -> EgressConfig:
        return self._agent_config().egress

    def _model(self) -> SemanticModel | None:
        if self._semantic is None:
            return None
        try:
            return self._semantic.model()
        except Exception as e:  # noqa: BLE001 —— provider 异常类型不承诺稳定
            raise MaskingError(
                f"egress: 语义模型读不出来,无法确认脱敏声明: {e}"
            ) from e

    # ── 工具观测 ─────────────────────────────────────────────

    def redact_tool_result(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        value: Any,
    ) -> tuple[Any, dict[str, Any]]:
        """一个工具返回值 → (可回喂的副本, 报告)。

        报告为空 dict = 什么都没改(未声明 / 非数据工具 / 已关闭)。报告非空
        时由调用方累计进 run 级 egress 汇总。**绝不改写入参对象**:probe 结果
        在 ``probe_cache`` 里跨轮共享,改写它会让第二次观测脱敏两次。
        """
        cfg = self._egress()
        if not cfg.enabled or not cfg.redact_tool_results:
            return value, {}
        if tool_name not in DATA_TOOLS:
            return value, {}
        try:
            model = self._model()
            if model is None:
                return value, {}
            if tool_name == "probe_query":
                return self._probe(value, args or {}, model)
            if tool_name == "search_values":
                return self._search(value, args or {}, model)
            # check_result:只有规则文本,无单元格值(见模块 docstring)。
            return value, {}
        except Exception as e:  # noqa: BLE001 —— 任何失败都按扣留处理
            log.warning(
                "egress redaction withheld observation (tool=%s): %s",
                tool_name, e,
            )
            return WITHHELD_OBSERVATION, {
                "channel": "tool_result",
                "tool": tool_name,
                "withheld": True,
            }

    def _probe(
        self, value: Any, args: dict[str, Any], model: SemanticModel,
    ) -> tuple[Any, dict[str, Any]]:
        if not isinstance(value, dict):
            raise MaskingError("probe_query 观测不是对象")
        if not value.get("ok"):
            return value, {}
        columns = value.get("columns")
        rows = value.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise MaskingError("probe_query 观测缺少 columns/rows")
        if not rows:
            return value, {}
        masker = Masker(
            default_salt_ref=self._agent_config().masking.hash_salt_ref,
        )
        new_rows, report = masker.apply(
            rows,
            [str(c) for c in columns],
            model=model,
            principal=None,   # 刻意:工具回喂没有 bypass 面
            sql=str(args.get("sql") or ""),
            strict=True,      # 出口不接受按列名退化的别名边界
        )
        fields = dict(report.get("fields") or {})
        if not fields:
            return value, {}
        out = {**value, "rows": new_rows, "note": SENSITIVE_NOTE}
        record_egress_redaction("tool_result", "probe_query")
        return out, {
            "channel": "tool_result",
            "tool": "probe_query",
            "fields": fields,
            "rows": len(new_rows),
            "values": 0,
        }

    def _search(
        self, value: Any, args: dict[str, Any], model: SemanticModel,
    ) -> tuple[Any, dict[str, Any]]:
        if not isinstance(value, dict):
            raise MaskingError("search_values 观测不是对象")
        if not value.get("ok"):
            return value, {}
        table = str(value.get("table") or args.get("table") or "")
        salt_ref = self._agent_config().masking.hash_salt_ref
        out = dict(value)
        fields: dict[str, str] = {}
        masked_values = 0

        if isinstance(value.get("values"), list):
            column = str(value.get("column") or args.get("column") or "")
            if not column:
                raise MaskingError("search_values 观测缺少列名")
            new_values, rep = mask_values(
                model, table, column, value["values"],
                default_salt_ref=salt_ref,
            )
            if rep.get("mode"):
                out["values"] = new_values
                masked_values += len(new_values)
                for name in rep.get("fields") or []:
                    fields.setdefault(name, rep["mode"])

        hits = value.get("hits")
        if hits is not None:
            if not isinstance(hits, dict):
                raise MaskingError("search_values hits 形状非法")
            new_hits: dict[str, list] = {}
            for column, values in hits.items():
                if not isinstance(values, list):
                    raise MaskingError("search_values hits 值形状非法")
                new_values, rep = mask_values(
                    model, table, str(column), values,
                    default_salt_ref=salt_ref,
                )
                if rep.get("mode"):
                    masked_values += len(new_values)
                    for name in rep.get("fields") or []:
                        fields.setdefault(name, rep["mode"])
                new_hits[column] = new_values
            out["hits"] = new_hits

        if not fields:
            return value, {}
        out["note"] = SENSITIVE_NOTE
        record_egress_redaction("search_values", "search_values")
        return out, {
            "channel": "search_values",
            "tool": "search_values",
            "fields": fields,
            "rows": 0,
            "values": masked_values,
        }

    # ── few-shot 样例值 ──────────────────────────────────────

    def redact_shots(
        self, shots: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """``few_shots[].parameters[].sample_values`` → 脱敏副本 + 报告。

        提示词渲染(``gen_sql/user.*.j2``)与 token 估算(``render_shots``)必须
        用**同一份**脱敏后的 shots —— 调用方在装配阶段替换一次。
        """
        items = list(shots or [])
        if not items or not self._egress().enabled:
            return items, {}
        if not any(
            (p.get("sample_values") and p.get("column"))
            for shot in items
            for p in (shot.get("parameters") or [])
        ):
            # 没有样例值可脱敏:不读模型(零成本,也不产生噪音报告)。
            return items, {}
        try:
            model = self._model()
            if model is None:
                return items, {}
            salt_ref = self._agent_config().masking.hash_salt_ref
            fields: dict[str, str] = {}
            masked_values = 0
            out: list[dict[str, Any]] = []
            for shot in items:
                params = shot.get("parameters") or []
                if not params:
                    out.append(shot)
                    continue
                changed = False
                new_params: list[dict[str, Any]] = []
                for p in params:
                    values = p.get("sample_values")
                    column = str(p.get("column") or "")
                    if not isinstance(values, list) or not values or not column:
                        new_params.append(p)
                        continue
                    table, column_name = _split_ref(column)
                    new_values, rep = mask_values(
                        model, table, column_name, values,
                        default_salt_ref=salt_ref,
                    )
                    if rep.get("mode"):
                        new_params.append({**p, "sample_values": new_values})
                        changed = True
                        masked_values += len(new_values)
                        for name in rep.get("fields") or []:
                            fields.setdefault(name, rep["mode"])
                    else:
                        new_params.append(p)
                out.append({**shot, "parameters": new_params} if changed else shot)
            if not fields:
                return items, {}
            record_egress_redaction("prompt_samples", "prompt")
            return out, {
                "channel": "prompt_samples",
                "tool": "prompt",
                "fields": fields,
                "rows": 0,
                "values": masked_values,
            }
        except Exception as e:  # noqa: BLE001 —— fail-closed:样例值全部剥掉
            log.warning(
                "egress redaction failed for prompt samples; "
                "stripping sample values: %s", e,
            )
            stripped = [
                {
                    **shot,
                    "parameters": [
                        {k: v for k, v in p.items() if k != "sample_values"}
                        for p in (shot.get("parameters") or [])
                    ],
                }
                if shot.get("parameters") else shot
                for shot in items
            ]
            return stripped, {
                "channel": "prompt_samples",
                "tool": "prompt",
                "withheld": True,
            }


def redact_tool_result(
    redactor: Any | None,
    tool_name: str,
    args: dict[str, Any] | None,
    value: Any,
) -> tuple[Any, dict[str, Any]]:
    """单次工具回喂的脱敏口(None → 原样,便于调用方无分支调用)。"""
    if redactor is None:
        return value, {}
    return redactor.redact_tool_result(tool_name, args, value)


def egress_entry(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """单次报告 → 可合并的 ``{tool: entry}``(空报告 → ``{}``)。"""
    if not report:
        return {}
    return {str(report.get("tool") or "unknown"): dict(report)}


def merge_egress_reports(
    base: dict[str, dict[str, Any]] | None,
    extra: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """按工具合并两份出境汇总(字段并集 + 计数相加)。

    逐轮合并而非覆盖:修正轮的每个注册表各记一轮的观测,覆盖会丢掉前一轮
    实际发生过的出境;而「这一问里有没有敏感值发给过模型」问的是整段运行。
    """
    out: dict[str, dict[str, Any]] = {}
    for source in (base or {}, extra or {}):
        for tool, entry in source.items():
            target = out.setdefault(
                str(tool), {"fields": {}, "rows": 0, "values": 0},
            )
            for name, mode in (entry.get("fields") or {}).items():
                target["fields"][str(name)] = str(mode)
            target["rows"] += int(entry.get("rows") or 0)
            target["values"] += int(entry.get("values") or 0)
            if entry.get("withheld"):
                target["withheld"] = int(target.get("withheld") or 0) + int(
                    entry["withheld"]
                )
    return out
