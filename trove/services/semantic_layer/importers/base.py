"""外部语义层导入的中立中间表示(IR)。

解析器(dbt / Cube)只产出这里的 dataclass,**不碰** ``semantics.yml``;
落点(``landing.py``)再把 IR 翻成两种目标形状(bootstrap 的完整 OSSIE
文档 / stage 的有序草稿 payload)。这样两种来源共用一条写入门,映射差异
只在各自的解析器里,不扩散到落点。

纯解析:本模块与兄弟模块都不 import LLM、连接器或数据源适配器
(``test_importers_pure.py`` 用 AST 钉住)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp, parse_one

_ANSI = "ANSI_SQL"


class SemanticImportError(Exception):
    """用户可修正的导入错误(路径/内容/模式前置条件/阻断 lint)。

    CLI 把它映射为退出码 2;服务层突发错误(IO、审批流抛错)不包装成它。
    """

    def __init__(self, message: str, *, blocking: list[str] | None = None):
        super().__init__(message)
        self.blocking = list(blocking or [])


@dataclass
class ImportWarning:
    code: str
    target: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "target": self.target, "message": self.message}


@dataclass
class ImportedField:
    name: str
    expression: str
    datatype: str | None = None
    description: str = ""
    is_time: bool = False


@dataclass
class ImportedDataset:
    name: str
    source: str
    primary_key: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    description: str = ""
    fields: list[ImportedField] = field(default_factory=list)


@dataclass
class ImportedMetric:
    name: str
    expression: str
    datasets: list[str] = field(default_factory=list)
    description: str = ""
    metric_type: str = ""  # "" | "ratio" | "derived"


@dataclass
class ImportedRelationship:
    name: str
    from_dataset: str
    to_dataset: str
    from_columns: list[str]
    to_columns: list[str]
    cardinality: str = "1:N"


@dataclass
class ImportedSemantic:
    source: str
    datasets: list[ImportedDataset] = field(default_factory=list)
    metrics: list[ImportedMetric] = field(default_factory=list)
    relationships: list[ImportedRelationship] = field(default_factory=list)
    warnings: list[ImportWarning] = field(default_factory=list)
    unsupported: list[ImportWarning] = field(default_factory=list)
    input_path: str = ""

    # ── 报告 ────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        return {
            "datasets": len(self.datasets),
            "fields": sum(len(d.fields) for d in self.datasets),
            "metrics": len(self.metrics),
            "relationships": len(self.relationships),
        }

    def warn(self, code: str, target: str, message: str) -> None:
        self.warnings.append(ImportWarning(code, target, message))

    def skip(self, code: str, target: str, message: str) -> None:
        self.unsupported.append(ImportWarning(code, target, message))

    def to_report(self, **extra: Any) -> dict[str, Any]:
        report: dict[str, Any] = {
            "source": self.source,
            "path": self.input_path,
            "counts": self.counts(),
            "datasets": [
                {"name": d.name, "source": d.source, "fields": len(d.fields)}
                for d in self.datasets
            ],
            "metrics": [m.name for m in self.metrics],
            "relationships": [r.name for r in self.relationships],
            "warnings": [w.to_dict() for w in self.warnings],
            "unsupported": [u.to_dict() for u in self.unsupported],
        }
        report.update(extra)
        return report


# ── 表达式工具(解析器共用) ─────────────────────────────────


def validate_expression(expr: str) -> bool:
    """SQLGlot 能解析的纯表达式才算可落盘(字段 / 指标同一档)。"""
    text = (expr or "").strip()
    if not text:
        return False
    try:
        parse_one(text)
    except Exception:
        return False
    return True


def qualify_expression(expr: str, dataset: str) -> str | None:
    """给**未限定**的列补上数据集名(SQLGlot AST 级,不做字符串拼接)。

    只给 ``table == ""`` 的列补名:已经限定过的列原样保留,避免
    ``orders.amount`` 变成 ``orders.orders.amount``。解析失败 / 纯 ``*``
    → ``None``(调用方决定回退或跳过)。
    """
    text = (expr or "").strip()
    if not text:
        return None
    try:
        tree = parse_one(text)
    except Exception:
        return None
    if isinstance(tree, exp.Star):
        return None
    for col in tree.find_all(exp.Column):
        if not col.table:
            col.set("table", exp.to_identifier(dataset))
    return tree.sql()


def aggregate_expression(agg: str, expr: str) -> str | None:
    """聚合函数名 + 已限定表达式 → 聚合 SQL;不认识的聚合 → ``None``。"""
    fn = {
        "sum": "SUM",
        "avg": "AVG",
        "min": "MIN",
        "max": "MAX",
        "count": "COUNT",
    }.get(agg)
    if fn is not None:
        return f"{fn}({expr})"
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {expr})"
    return None


def dataset_refs(expression: str, declared: set[str]) -> list[str]:
    """表达式引用到的已声明数据集名(与 ``ossie._dataset_refs`` 同语义)。"""
    found: set[str] = set()
    try:
        for col in parse_one(expression).find_all(exp.Column):
            if col.table in declared:
                found.add(col.table)
    except Exception:
        return []
    return sorted(found)


def unique_name(base: str, used: set[str]) -> str:
    """确定性去重:冲突时追加 ``_2`` / ``_3``(不引入随机数)。"""
    if base not in used:
        used.add(base)
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    name = f"{base}_{n}"
    used.add(name)
    return name


__all__ = [
    "ImportWarning",
    "ImportedDataset",
    "ImportedField",
    "ImportedMetric",
    "ImportedRelationship",
    "ImportedSemantic",
    "SemanticImportError",
    "aggregate_expression",
    "dataset_refs",
    "qualify_expression",
    "unique_name",
    "validate_expression",
]
