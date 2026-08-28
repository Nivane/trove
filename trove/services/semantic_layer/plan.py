"""Typed plan AST for the semantic layer compile boundary.

Planner 的原始输出仍是松散 JSON dict(生成侧无 structured output),这里
在**解析/编译边界**把 plan 强类型化:`PlanQuery` 是编译器与 planner 共用的
查询 IR。解析语义:

- 顶层形态错误(非 dict、conditions/having 非列表、item 非 dict、ordering
  形态非法、having 的 field/metric 不是恰好一个、未知 time grain)→ 整体
  ``None`` —— 调用方回退现有 raw-dict 流,行为字节级不变。**不允许静默
  丢弃单个语义组件**(丢过滤条件比拒绝更糟:严格 MISS 会触发拒绝+扩展,
  静默丢弃会产出错误 SQL)。
- 标量强制转换容忍(op/direction/grain/聚合字段),缺失可选字段走默认值,
  未知多余 key 忽略。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 时间粒度白名单(编译器与 dict 流共用同一常量)。
GRAINS = frozenset({"year", "quarter", "month", "week", "day"})

# 分析类型(plan.analysis.type 白名单):编译器窗口函数分析。
ANALYSIS_TYPES = frozenset({
    "share",        # 占比:metric / SUM(metric) OVER (PARTITION BY ...)
    "running_total",  # 累计:SUM(metric) OVER (... ORDER BY time ROWS UNBOUNDED PRECEDING)
    "mom",          # 环比增量:metric - LAG(metric,1) OVER (... ORDER BY time)
    "yoy",          # 同比增量:metric - LAG(metric,n) OVER (... ORDER BY time),n 由 grain 推
    "pct_change",   # 环比增长率:(metric - LAG)/LAG
    "rank",         # 排名:RANK() OVER (... ORDER BY metric DESC)
})


def _norm_direction(value: Any) -> str:
    """方向归一:desc/descending → "desc",其余一律 "asc"(容错)。"""
    d = str(value).strip().lower()
    return "desc" if d in ("desc", "descending") else "asc"


def _parse_ordering_entries(parts: list[str]) -> list[tuple[str, str]] | None:
    """解析逗号切分后的排序段:"col" / "col asc" / "col desc"。

    列名可为多词(metric 名带空格,如 "number of loan records desc"):
    末 token 是 asc/desc 时作方向,其余整体作列名。空段与占位文本跳过。
    非法段(列名缺失)→ None。
    """
    out: list[tuple[str, str]] = []
    for part in parts:
        part = part.strip()
        if not part or part.lower() in ("none", "empty", "(empty if none)"):
            continue
        tokens = part.split()
        if tokens[-1].lower() in ("asc", "desc"):
            col = " ".join(tokens[:-1]).strip()
            direction = tokens[-1].lower()
        else:
            col = part
            direction = "asc"
        if not col:
            return None
        out.append((col, direction))
    return out


def parse_ordering(value: Any) -> list[tuple[str, str]] | None:
    """解析排序三种形态:字符串 / 列表[str] / 列表[dict]。

    返回 (column, direction) 列表;非法形态 → None(PlanQuery 视为整体
    失败回退 dict 流;编译器 dict 流调用方自行决定丢弃)。
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return _parse_ordering_entries(value.split(","))
    if isinstance(value, list):
        out: list[tuple[str, str]] = []
        for item in value:
            if isinstance(item, str):
                parsed = _parse_ordering_entries([item])
                if parsed is None:
                    return None
                out.extend(parsed)
            elif isinstance(item, dict):
                col = str(item.get("column") or "").strip()
                if not col:
                    return None
                out.append((col, _norm_direction(item.get("direction"))))
            else:
                return None
        return out
    return None


def _coerce_str(value: Any) -> str:
    return "" if value is None else str(value)


class PlanCondition(BaseModel):
    """行级过滤条件(field/op/value/note)。"""

    model_config = ConfigDict(extra="ignore")

    field: str
    op: str = "="
    value: Any = None
    note: str = ""

    @field_validator("field", "op", "note", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> str:
        return _coerce_str(v)


class PlanTimeGrain(BaseModel):
    """时间分桶:field 必须是声明的时间字段,grain ∈ 白名单。"""

    model_config = ConfigDict(extra="ignore")

    field: str
    grain: str

    @field_validator("field", mode="before")
    @classmethod
    def _field_str(cls, v: Any) -> str:
        return _coerce_str(v)

    @field_validator("grain", mode="before")
    @classmethod
    def _grain(cls, v: Any) -> str:
        g = str(v).strip().lower()
        if g not in GRAINS:
            raise ValueError(f"unknown time grain: {g!r}")
        return g


class PlanHaving(BaseModel):
    """聚合后过滤:field(折进 WHERE)与 metric(进 HAVING)恰好一个。"""

    model_config = ConfigDict(extra="ignore")

    field: str | None = None
    metric: str | None = None
    op: str = "="
    value: Any = None

    @field_validator("field", "metric", "op", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> str | None:
        return _coerce_str(v) if v is not None else None

    @model_validator(mode="after")
    def _exactly_one(self) -> "PlanHaving":
        if bool(self.field) == bool(self.metric):
            raise ValueError("PlanHaving requires exactly one of field/metric")
        return self


class PlanOrdering(BaseModel):
    model_config = ConfigDict(extra="ignore")

    column: str
    direction: str = "asc"

    @field_validator("column", mode="before")
    @classmethod
    def _col(cls, v: Any) -> str:
        return _coerce_str(v)

    @field_validator("direction", mode="before")
    @classmethod
    def _dir(cls, v: Any) -> str:
        return _norm_direction(v)


class PlanAnalysis(BaseModel):
    """窗口函数分析:编译器把聚合结果再套一层窗口计算。

    type: share / running_total / mom / yoy / pct_change / rank。
    metric: 目标度量名(缺省 = plan 的唯一聚合度量)。
    partition_by: 窗口分区字段(占比/排名/环比按组内算时用;缺省空)。
    order_by: 窗口排序字段——时间类分析(running_total/mom/yoy/pct_change)
        必需时间维度;share/rank 可选(rank 默认按度量降序)。
    direction: 窗口 ORDER BY 方向(asc/desc,默认 asc)。
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    metric: str = ""
    partition_by: list[str] = Field(default_factory=list)
    order_by: str = ""
    direction: str = "asc"

    @field_validator("type", mode="before")
    @classmethod
    def _type(cls, v: Any) -> str:
        t = _coerce_str(v).strip().lower()
        if t not in ANALYSIS_TYPES:
            raise ValueError(f"unknown analysis type: {t!r}")
        return t

    @field_validator("metric", "order_by", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> str:
        return _coerce_str(v).strip()

    @field_validator("partition_by", mode="before")
    @classmethod
    def _parts(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("partition_by must be a list")
        return [str(x).strip() for x in v if str(x).strip()]

    @field_validator("direction", mode="before")
    @classmethod
    def _dir(cls, v: Any) -> str:
        return _norm_direction(v)


class PlanQuery(BaseModel):
    """planner 查询计划的强类型 IR(编译器唯一输入)。"""

    model_config = ConfigDict(extra="ignore")

    tables: list[str] = Field(default_factory=list)
    joins: str = ""
    conditions: list[PlanCondition] = Field(default_factory=list)
    aggregation: str = ""
    extreme: dict[str, Any] | None = None
    ordering: list[PlanOrdering] = Field(default_factory=list)
    answer_columns: list[str] = Field(default_factory=list)
    time_grain: PlanTimeGrain | None = None
    having: list[PlanHaving] = Field(default_factory=list)
    analysis: PlanAnalysis | None = None
    limit: int | None = None
    plan_field: str = ""

    @field_validator("tables", mode="before")
    @classmethod
    def _tables(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError(f"tables must be a list, got {type(v).__name__}")
        return [str(t) for t in v]

    @field_validator("joins", "plan_field", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> str:
        return _coerce_str(v)

    @field_validator("aggregation", mode="before")
    @classmethod
    def _agg(cls, v: Any) -> str:
        # 与 dict 流 str(plan.get("aggregation") or "") 语义对齐
        return "" if v is None else str(v)

    @field_validator("extreme", mode="before")
    @classmethod
    def _extreme(cls, v: Any) -> dict[str, Any] | None:
        # extreme 编译器不消费,非 dict 宽松置空
        return v if isinstance(v, dict) else None

    @field_validator("conditions", "having", mode="before")
    @classmethod
    def _list_of_dict(cls, v: Any) -> list[Any]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("expected a list")
        return v  # item 形态由子模型校验把关(非 dict item → 整体失败)

    @field_validator("ordering", mode="before")
    @classmethod
    def _ordering(cls, v: Any) -> list[PlanOrdering]:
        parsed = parse_ordering(v)
        if parsed is None:
            raise ValueError(f"invalid ordering shape: {v!r}")
        return [PlanOrdering(column=c, direction=d) for c, d in parsed]

    @field_validator("answer_columns", mode="before")
    @classmethod
    def _answer_cols(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError(f"answer_columns must be a list, got {type(v).__name__}")
        return [str(x) for x in v]

    @field_validator("analysis", mode="before")
    @classmethod
    def _analysis(cls, v: Any) -> Any:
        # 缺省 → None;present 但形态非法 → 整体失败(回退 dict 流,编译器
        # 在 dict 流同样严格 MISS,不静默丢弃分析意图)。
        if v is None or v == "":
            return None
        return v

    @field_validator("limit", mode="before")
    @classmethod
    def _limit(cls, v: Any) -> int | None:
        if v is None or v == "":
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"invalid limit: {v!r}")
        return n if n > 0 else None

    def to_dict(self) -> dict[str, Any]:
        """回投 raw-dict 形态(供编译器内部 dict 流复用)。"""
        return self.model_dump(exclude_none=True)


def parse_plan_query(data: Any) -> PlanQuery | None:
    """dict/JSON → PlanQuery;形态错误 → None(永不抛)。

    None 语义 = 回退现有 raw-dict 流,行为与改造前字节级一致。
    """
    try:
        return PlanQuery.model_validate(data)
    except Exception:
        return None
