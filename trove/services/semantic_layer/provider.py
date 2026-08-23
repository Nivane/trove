"""SemanticLayerProvider: live, validated semantic layer for a datasource.

Reads OSSIE YAML files from a per-datasource directory (default
<trove_root>/.trove/semantic/<datasource>/) at query time. Files are
re-parsed only when mtime/size changes — lazy "real-time": the next
question sees the change, no polling or sync. Validation drops bad
metrics, and a broken file falls back to the last known good model;
the provider never raises into the question flow.
"""
import logging
import re
from pathlib import Path
from typing import Callable

from sqlglot import Dialect, ErrorLevel, parse_one

from trove.services.kb.service import TermHit
from trove.services.semantic_layer.models import SemanticMetric, SemanticModel
from trove.services.semantic_layer.ossie import parse_ossie

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "with", "to", "and", "or",
    "per", "by", "is", "are", "what", "how", "many", "much", "does", "do",
}
_WORD_RE = re.compile(r"[A-Za-z]+")


def _tokens(text: str) -> set[str]:
    """小写词元,去停用词,去复数 s(与 KB term 匹配同款朴素处理)。"""
    words = _WORD_RE.findall(text.lower())
    return {
        w[:-1] if w.endswith("s") and not w.endswith("ss") else w
        for w in words if w not in _STOPWORDS
    }


def _merge_models(base: SemanticModel, override: SemanticModel) -> SemanticModel:
    """合并两个语义模型:override(KB, 真源)按名字覆盖 base(配置目录)。

    单一真源原则:同名 dataset/relationship/metric 以 KB 为准,避免
    配置目录里的演示资产遮蔽权威定义。
    """
    if base is None:
        return override
    datasets = {d.name: d for d in base.datasets}
    for d in override.datasets:
        datasets[d.name] = d
    relationships = {r.name: r for r in base.relationships}
    for r in override.relationships:
        relationships[r.name] = r
    metric_names = {m.name for m in base.metrics}
    metrics = list(base.metrics)
    for m in override.metrics:
        metrics = [x for x in metrics if x.name != m.name] if m.name in metric_names else metrics
        metrics.append(m)
        metric_names.add(m.name)
    return SemanticModel(
        name=override.name or base.name,
        description=override.description or base.description,
        instructions=override.instructions or base.instructions,
        metrics=metrics,
        datasets=list(datasets.values()),
        relationships=list(relationships.values()),
    )


class SemanticLayerProvider:
    """Per-datasource live semantic layer reader.

    Args:
        directory: Directory holding OSSIE YAML files (one or more
            files, merged). Absent → provider disabled.
        datasource: Datasource name (logging/scope).
        parser: Optional parser override (default: parse_ossie bound to
            the adapter dialect). Injectable for tests/other formats.
        table_exists: Optional callback verifying a dataset maps to a
            real table; metrics referencing unknown datasets are dropped.
        dialect: Adapter dialect for expression validation and
            dialect-aware parsing.
    """

    def __init__(
        self,
        directory: str | Path,
        datasource: str,
        parser: Callable[[str], SemanticModel] | None = None,
        table_exists: Callable[[str], bool] | None = None,
        dialect: str = "sqlite",
        kb_semantics_path: str | Path | None = None,
    ) -> None:
        self.directory = Path(directory)
        self.datasource = datasource
        self._kb_path = (
            Path(kb_semantics_path) if kb_semantics_path is not None else None)
        self._parser = parser or (
            lambda text: parse_ossie(text, preferred_dialect=dialect))
        self._table_exists = table_exists
        try:
            Dialect.get_or_raise(dialect)
            self._read = dialect
        except Exception:
            self._read = None  # sqlglot 不认识的方言 → 默认解析器
        self._key: tuple | None = None  # [(path, mtime_ns, size)] 缓存键
        self._parsed: SemanticModel | None = None  # last known good
        self._validated: list[SemanticMetric] | None = None

    @property
    def enabled(self) -> bool:
        """配置目录有文件,或该数据源的 KB semantics.yml 存在即启用。

        单一真源 = KB semantics.yml(kb init 生成 + 人审);.trove/semantic
        目录作为补充源(演示资产等)。
        """
        if self._kb_path is not None and self._kb_path.exists():
            return True
        return self.directory.is_dir() and bool(list(self.directory.glob("*.y*ml")))

    # ── Load / validate ────────────────────────────────────

    def _reload(self) -> None:
        files = sorted([*self.directory.glob("*.yaml"), *self.directory.glob("*.yml")])
        if self._kb_path is not None and self._kb_path.exists():
            files.append(self._kb_path)
        key = tuple((f, f.stat().st_mtime_ns, f.stat().st_size) for f in files)
        if key == self._key and self._parsed is not None:
            return  # 文件未变 → 命中缓存
        self._key = key
        try:
            merged: SemanticModel | None = None
            for f in files:
                model = self._parser(f.read_text(encoding="utf-8"))
                merged = _merge_models(merged, model) if merged is not None else model
            self._parsed = merged
        except Exception as e:
            # 解析失败 → 保留 last-known-good,只警告;文件下次再变才重试
            logger.warning(
                "Semantic layer parse failed (%s): %s — keeping last known good",
                self.datasource, e,
            )
            return
        self._validated = self._validate(self._parsed.metrics)

    def _validate(self, metrics: list[SemanticMetric]) -> list[SemanticMetric]:
        """逐条校验:括号配平 + SQLGlot 解析 + 数据集存在性。坏条目丢弃。"""
        out: list[SemanticMetric] = []
        for m in metrics:
            if m.expression.count("(") != m.expression.count(")"):
                logger.warning(
                    "Dropping semantic metric %s: unbalanced parentheses in %r",
                    m.name, m.expression,
                )
                continue
            try:
                parse_one(m.expression, read=self._read, error_level=ErrorLevel.RAISE)
            except Exception as e:
                logger.warning("Dropping semantic metric %s: %s", m.name, e)
                continue
            if self._table_exists is not None:
                unknown = [d for d in m.datasets if not self._table_exists(d)]
                if unknown:
                    logger.warning(
                        "Dropping semantic metric %s: unknown datasets %s",
                        m.name, unknown,
                    )
                    continue
            out.append(m)
        return out

    # ── Consumption ────────────────────────────────────────

    def metrics(self) -> list[SemanticMetric]:
        """当前所有通过校验的 metric(表锚定过滤由渲染方做)。"""
        if not self.enabled:
            return []
        self._reload()
        return list(self._validated or [])

    def model(self) -> SemanticModel | None:
        """当前解析出的完整 SemanticModel(datasets/relationships/metrics)。

        不可用 → None;解析失败保留 last-known-good(与 metrics() 同一
        缓存路径)。
        """
        if not self.enabled:
            return None
        self._reload()
        return self._parsed

    def terms_for(
        self,
        question: str,
        tables: list[str] | None = None,
        all_tables: list[str] | None = None,
    ) -> list[TermHit]:
        """Live metrics as terms: name/synonym substring + word overlap,
        数据集锚定(与 KB search_terms 同签名同语义)。"""
        hits: list[TermHit] = []
        for m in self.metrics():
            if not (
                m.name in question
                or any(a and a in question for a in m.synonyms)
                or self._word_overlap_ok(m.name, question)
            ):
                continue
            if (
                tables is not None
                and m.datasets
                and not any(t in tables for t in m.datasets)
            ):
                continue  # 绑定到未匹配的数据集 → 与当前问题无关
            hits.append(TermHit(
                term=m.name,
                aliases=list(m.synonyms),
                mapping=m.expression,
                tables=list(m.datasets),
                definition=m.definition,
            ))
        return hits

    def field_hits(self, question: str, tables: list[str] | None = None) -> list[str]:
        """问题词元 × 字段 synonyms 词重叠 → 确定性 Field hints(零 LLM)。

        命中即输出 ``'region' → district.A3``:planner 靠它把问题词链接到
        不透明列(如 BIRD 的 A3),与 value_hints 同机制。top≤5,保序。
        """
        if not self.enabled:
            return []
        model = self.model()
        if model is None:
            return []
        q = _tokens(question)
        if not q:
            return []
        matched = {str(t) for t in (tables or [])}
        out: list[str] = []
        for d in model.datasets:
            if tables is not None and d.name not in matched:
                continue
            for f in d.fields:
                for syn in [s for s in f.synonyms if s]:
                    if _tokens(syn) <= q:
                        out.append(f"'{syn}' → {d.name}.{f.name}")
                        break
                if len(out) >= 5:
                    return out
        return out

    @staticmethod
    def _word_overlap_ok(term: str, question: str) -> bool:
        """镜像 KB search_terms 的词重叠语义:term 有效词(去停用词)
        ≥2 词时,term 词在问题中的占比 ≥0.5 视为同一语义。"""
        t = _tokens(term)
        if len(t) < 2:
            return False
        q = _tokens(question)
        if not q:
            return False
        return len(t & q) / len(t) >= 0.5
