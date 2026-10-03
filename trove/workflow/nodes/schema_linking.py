"""Schema Linking node (语义优先 Phase B) — 唯一通道是语义模型.

语义模型是唯一可答边界:dataset/metric/field 检索(synonym/词重叠,复用
search_terms 语义)→ dataset 锚定 → 产出 semantic_context(仅渲染模型声明,
不含物理 DDL/统计/数值样本)。零命中 = 未覆盖 = 拒绝;无语义模型 = 整体拒绝 +
提示 /kb init。旧裸表路径(catalog 检索/value/FK/对齐/fallback)已从查询图
物理移除(决策 1/2/3)。

Node shape: `async def schema_linking(state: WorkflowState) -> dict`
returns a partial state update.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService, TermHit
from trove.services.semantic_layer.topics import (
    TopicResolution,
    in_scope,
    render_topic_line,
    resolve_topic,
    topic_names,
)
from trove.core.logging import get_logger
from trove.llm.observability import record_span
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

# 零匹配兜底的全量表数量上限(金融 dev 集 8 张表;防超长 context)
FALLBACK_TABLES_LIMIT = 8
# 反思轮放大上限(避免多轮重跑后 context 无限膨胀)
MAX_PROGRESSIVE_LIMIT = 16
# 基础匹配阈值(score ≥ 2.0 才锚定);反思轮按档放宽
BASE_MATCH_THRESHOLD = 2.0
# 反思轮 → 阈值放宽档位(第 N 轮起降到 1.5;第二轮再降 1.0)
_PROGRESSIVE_THRESHOLD = (
    (BASE_MATCH_THRESHOLD, BASE_MATCH_THRESHOLD),   # 第 0 轮(首查)
    (1.5, 2.0),                                     # 第 1 轮:中置信扩容 + 字段词仍从严
    (1.0, 2.0),                                     # 第 2+ 轮:再扩容
)

_QUOTED_RE = re.compile(r"['\"]([^'\"]{2,30})['\"]")
_CAPITALIZED_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_ALL_CAPS_RE = re.compile(r"\b[A-Z]{2,}\b")
# 中文字符(供 CJK 检索分支:问题含中文 → 指标召回可作为零锚定的补充证据)
_CJK_RE = re.compile(r"[一-鿿]")


def _dedup_tables(hits: list[TermHit]) -> list[str]:
    """Flatten term table lists, preserving order and dropping duplicates."""
    names: list[str] = []
    for hit in hits:
        for table in hit.tables:
            if table not in names:
                names.append(table)
    return names


# ── 语义优先通道(Phase B,见 semantic-first 架构文档 §3.1 / §4.1) ──────
#
# 语义模型是唯一可答边界:dataset/metric/field 检索(synonym/词重叠,复用
# search_terms 语义)→ dataset 锚定 → 产出 semantic_context(仅渲染模型声明
# 内容,不含物理 DDL/统计/数值样本)。零命中 = 未覆盖 = 拒绝;无语义模型 =
# 整体拒绝 + 提示 /kb init。旧裸表路径(决策 1)已物理移除。

_SEMANTIC_STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "with", "to", "and", "or",
    "per", "by", "is", "are", "what", "how", "many", "much", "does", "do",
}


def _word_tokens(text: str) -> set[str]:
    """小写词元,去停用词(与 provider/KB term 同款朴素处理)。"""
    words = re.findall(r"[a-z0-9_]+", (text or "").lower())
    return {w for w in words if w not in _SEMANTIC_STOPWORDS}


def _fold_plural(word: str) -> str:
    """朴素屈折归一:尾 s 去尾(≥4 字母、非 ss 结尾)。"""
    if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _semantic_dataset_score(d: Any, query: str, q_tokens: set[str]) -> float:
    """dataset 名/synonym/description 的确定性匹配分(零 LLM)。

    3.0 = 名称/synonym 子串命中;2.5 = synonym 词重叠 ≥0.5 或
    enum_display 展示值子串命中;2.0 = description 词重叠 ≥0.5;1.5 =
    description 词重叠 ≥0.25;1.0 = 名称/synonym/description 单 token
    命中(弱信号,仅反思轮放大时用)。
    基础阈值为 2.0 在调用侧判定,重跑轮按档放宽(见 _progressive_threshold)。
    """
    q = (query or "").lower()
    if d.name and d.name.lower() in q:
        return 3.0
    for s in d.synonyms:
        if s and str(s).lower() in q:
            return 3.0
    # enum_display 展示值命中("female" → gender 枚举列):问题点名枚举值 →
    # 锚定其数据集(0483 型问题里值词是唯一线索)。长度 ≥3 过滤 F/M 这类
    # 单字母码值——它们在英文问句里几乎必然误撞,是噪声。
    q_fold = {_fold_plural(w) for w in q_tokens}
    for f in getattr(d, "fields", None) or []:
        for _code, disp in (getattr(f, "enum_display", None) or {}).items():
            disp_s = str(disp or "").strip().lower()
            if len(disp_s) >= 3 and disp_s in q:
                return 2.5
            # 官方文档形态的长标签("running contract, OK so far")几乎不可能
            # 整串出现在问句里(实测 0.0 分,而短标签 "running contract" 靠
            # 子串命中 2.5)→ 补 token 级命中:标签词元(尾 s 屈折归一)与
            # 问句词元 ≥2 命中、且至少一个命中词元长 ≥5 才认——单词语义值
            # 与常见短词不单独构成锚定信号(噪声控制)。
            lf = {_fold_plural(w) for w in _word_tokens(disp_s)}
            hit = lf & q_fold
            if len(hit) >= 2 and any(len(w) >= 5 for w in hit):
                return 2.5
    for s in d.synonyms:
        if s:
            st = _word_tokens(s)
            if len(st) >= 2 and len(st & q_tokens) / len(st) >= 0.5:
                return 2.5
    dt = _word_tokens(d.description)
    if len(dt) >= 2:
        ov = len(dt & q_tokens) / len(dt)
        if ov >= 0.5:
            return 2.0
        if ov >= 0.25:
            return 1.5
    # 弱信号:dataset 名/synonym 任一 token 或 description 任一 token 命中
    pool = set(_word_tokens(d.name))
    for s in d.synonyms:
        pool |= _word_tokens(s)
    if pool & q_tokens:
        return 1.0
    if dt and dt & q_tokens:
        return 1.0
    return 0.0


def _progressive_threshold(retry_round: int) -> float:
    """反思轮 → 匹配阈值放宽(档位封顶)。首轮 = 旧阈值 2.0(行为不变)。"""
    idx = min(max(retry_round, 0), len(_PROGRESSIVE_THRESHOLD) - 1)
    return _PROGRESSIVE_THRESHOLD[idx][0]


def _progressive_tables_limit(retry_round: int) -> int:
    """反思轮 → 候选表上限(8 → 16,封顶)。"""
    return min(FALLBACK_TABLES_LIMIT + retry_round * 4, MAX_PROGRESSIVE_LIMIT)


def _semantic_match_datasets(
    model: Any, query: str, term_hits: list[TermHit],
    semantic_layer: Any | None,
    retry_round: int = 0,
    scope: set[str] | None = None,
) -> list[str]:
    """模型视角的 dataset 锚定:KB/live term 表 + 词法匹配,排序去重。

    词法匹配覆盖 dataset 名/synonym/description 词重叠,以及**声明字段名/
    synonym 命中**(问题提到某字段 → 锚定其数据集,等价旧列名检索)。

    ``retry_round``(反思/纠错重跑轮数)渐进放大候选:阈值档位放宽
    (2.0 → 1.5 → 1.0) + 上限提升(8 → 16)。首轮(0)与旧行为字节级一致;
    重跑轮多拉候选,给回滚到 schema_linking 的修正提供更多可锚表。

    ``scope``(主题域作用域)非空时,**三条通道一律收敛**:词法锚定、字段名
    锚定、KB/live term 表。收敛在入口而不是出口 —— 出口过滤会让"域外表先
    进候选、再被删掉",而候选顺序/上限是按全量算的,域内表可能已经被上限
    挤掉。``None`` = 未选主题域,行为与不启用主题域字节级一致。
    """
    q_tokens = _word_tokens(query)
    q_lower = (query or "").lower()
    declared = {d.name for d in model.datasets}
    matched: list[str] = []
    threshold = _progressive_threshold(retry_round)
    scored = [
        (name, _semantic_dataset_score(d, query, q_tokens))
        for d in model.datasets for name in [d.name]
        if in_scope(scope, name)
    ]
    for name, score in sorted(scored, key=lambda x: -x[1]):
        if score >= threshold and name not in matched:
            matched.append(name)

    # 声明字段命中 → 数据集锚定(问题词 = 字段名/synonym 子串)。
    # 原始字段名子串只对业务列(dimension/enum/measure)生效:identifier/time
    # 列是结构列(主键、日期),其原始名常与问题里的普通词撞车(如 "issued"
    # 命中 card.issued 时间列,把无关数据集拉进作用域,query_sketch 进而误锚列)。
    # synonym 命中不受此限(人工写的业务词表)。
    for d in model.datasets:
        if d.name in matched or not in_scope(scope, d.name):
            continue
        for f in d.fields:
            role = str(getattr(f, "semantic_role", "") or "").strip().lower()
            name_hit = (
                bool(f.name)
                and f.name.lower() in q_lower
                and role not in ("identifier", "time")
            )
            syn_hit = any(
                s and str(s).lower() in q_lower for s in f.synonyms
            )
            if name_hit or syn_hit:
                matched.append(d.name)
                break

    term_tables: list[str] = []
    try:
        term_tables = _dedup_tables(term_hits)
    except Exception:
        pass
    live_tables: list[str] = []
    if semantic_layer is not None:
        try:
            live_tables = _dedup_tables(semantic_layer.terms_for(query))
        except Exception:
            live_tables = []
    for t in term_tables + live_tables:
        if t in declared and in_scope(scope, t) and t not in matched:
            matched.append(t)
    return matched[:_progressive_tables_limit(retry_round)]


def _render_semantic_context(
    model: Any, matched: list[str], semantic_layer: Any | None,
    question: str, metric_hits: list[Any] | None = None,
    topic: TopicResolution | None = None,
) -> str:
    """仅渲染模型声明内容(dataset/metric/field+synonym/关系/instructions)。

    不出现物理 schema 启发(统计/数值样本/FK 命名边/value hints)。
    字段名是已声明语义词表(§4.1),表达式隐藏、由 metric/field_hints 承载。

    ``metric_hits``(typed 检索选出的相关指标)非空时,以"Relevant metrics"
    块渲染相关性选择的指标(带口径),替换逐 dataset 全量渲染的 Metrics 块
    ——口径注入 + 相关度优先,避免无关指标挤占。
    """
    from trove.services.semantic_layer.compiler import JoinResolver

    resolution = JoinResolver(model).resolve(list(matched))
    datasets_by_name = {d.name: d for d in model.datasets}
    render_tables = list(matched)
    for extra in resolution.extra_tables:
        if extra not in render_tables:
            render_tables.append(extra)

    parts: list[str] = []
    # 主题域声明行(仅命中时渲染):作用域事实先行,生成侧据此不往域外找表。
    topic_line = render_topic_line(topic) if topic is not None else ""
    if topic_line:
        parts.append(topic_line)
    use_selected = bool(metric_hits)
    if use_selected:
        parts.append("Relevant metrics:\n" + "\n".join(
            f"- {m.name} = {m.expression}"
            + (f" — {m.definition}" if m.definition else "")
            for m in metric_hits))
    for name in render_tables:
        d = datasets_by_name.get(name)
        if d is None:
            continue
        lines = [f"Dataset: {name}"]
        if d.description:
            lines.append(f"Description: {d.description}")
        if d.fields:
            lines.append("Fields:")
            for f in d.fields:
                bits = [f.name]
                _desc = " ".join(str(getattr(f, "description", "") or "").split())
                _role = str(getattr(f, "semantic_role", "") or "").strip().lower()
                if _desc and _role in ("dimension", "enum", "measure"):
                    # 业务列(维度/枚举/度量)的描述是值语义线索(官方文档
                    # 导入的列义,如 "average salary");identifier/time 是
                    # 结构列,描述多为样板句,渲染只挤占预算。单行、截 80。
                    if len(_desc) > 80:
                        _desc = _desc[:80].rstrip() + "…"
                    bits.append(f"desc={_desc}")
                if f.synonyms:
                    bits.append("synonyms: " + ", ".join(f.synonyms))
                if f.semantic_role:
                    bits.append(f"role={f.semantic_role}")
                if f.is_time:
                    bits.append("time")
                if f.enum_display:
                    # 渲染值映射(不是只报个数):query_sketch 据此把人类值(male/男性)
                    # 落到规范 code(M),编译期再确定性归一——值留在字段层。
                    # 分隔符用 "; " 而非 ", ":官方文档导入的标签自身带逗号
                    # ("running contract, OK so far"),逗号分隔会让映射边界歧义。
                    mapping = "; ".join(
                        f"{k}={v}" for k, v in f.enum_display.items())
                    bits.append(f"enum {{{mapping}}}")
                if getattr(f, "value_aliases", None):
                    # 值语义字典(多标签):evidence/建模期沉淀的别名,扩展示例问法
                    # 词到存储值的桥(weekly issuance → POPLATEK TYDNE)。同样渲染
                    # 给生成通道,避免 agent 瞎猜值。
                    alias_txt = "; ".join(
                        f"{k} ≈ {', '.join(labels)}"
                        for k, labels in f.value_aliases.items())
                    bits.append(f"value_aliases {{{alias_txt}}}")
                lines.append("  - " + " | ".join(bits))
        if not use_selected:
            anchored = [m for m in model.metrics if m.datasets and name in m.datasets]
            if anchored:
                lines.append("Metrics:")
                for m in anchored:
                    line = f"  - {m.name} = {m.expression}"
                    if m.definition:
                        line += f" — {m.definition}"
                    lines.append(line)
        parts.append("\n".join(lines))

    resolved_block = "" if resolution.fan_out else JoinResolver.render(resolution)
    if resolved_block:
        parts.append(resolved_block)

    model_lines: list[str] = []
    agnostic = [m for m in model.metrics if not m.datasets]
    if agnostic:
        model_lines.append(
            "Metrics:\n" + "\n".join(
                f"- {m.name} = {m.expression}" for m in agnostic))
    if model.instructions:
        model_lines.append(f"Semantic note: {model.instructions}")
    if model_lines:
        parts.append("\n\n".join(model_lines))

    if semantic_layer is not None:
        try:
            hits = semantic_layer.field_hits(question, matched)
            if hits:
                parts.append("Field hints: " + "; ".join(hits))
        except Exception:
            pass

    return "\n\n".join(parts) if parts else "No semantic model matched this question."


async def _semantic_linking(
    state: WorkflowState, kb, connectors, semantic_layer,
    term_hits: list[TermHit], search_query: str, datasource: str,
    retry_round: int = 0,
) -> dict[str, Any]:
    """语义优先主通道(§3.1):dataset 锚定 + semantic_context,唯一路径。"""
    model = None
    if semantic_layer is not None and datasource and getattr(
            semantic_layer, "enabled", False):
        try:
            model = semantic_layer.model()
        except Exception as e:
            logger.warning("Semantic model lookup failed (%s): %s", datasource, e)
            model = None

    if model is None:
        # 决策 2/3:无语义模型 → 整体拒绝 + 提示 /kb init(不静默降级裸表)
        return {
            "matched_tables": [],
            "schema_context": "",
            "semantic_context": "",
            "no_model": True,
            "link_detail": {"semantic_first": True, "no_model": True},
        }

    # ── 主题域(可选):选择 → 作用域 → 收敛锚定 ──────────────
    # 用户/前端按主题域进入;名字解析不到 = **显式失败**(topic_not_found),
    # 不静默退回全量 —— 用户选的是范围,范围失效却按更大的数据回答,是
    # "答非所选"。未选主题域 → status="none",下面每条路径与旧行为一致。
    topic_res = resolve_topic(model, str(getattr(state, "topic", "") or ""))
    if topic_res.status in ("not_found", "empty_scope"):
        return {
            "matched_tables": [],
            "schema_context": "",
            "semantic_context": "",
            "refusal": {
                "reason": {
                    "not_found": "topic_not_found",
                    "empty_scope": "topic_empty_scope",
                }[topic_res.status],
                "question": state.question,
                "topic": topic_res.requested,
                "available_topics": topic_names(model),
            },
            "link_detail": {
                "semantic_first": True,
                "topic": topic_res.requested,
                "topic_status": topic_res.status,
            },
        }
    scope = topic_res.scope_set

    matched = _semantic_match_datasets(
        model, search_query, term_hits, semantic_layer,
        retry_round=retry_round, scope=scope)

    # 指标相关性选择 + 图链接(P4):metric 命中沿 metric.datasets 扩展表锚,
    # 相关性选择的指标(带口径)替换"全量渲染锚定 metrics"。只在已有锚定时
    # 生效——零锚定仍走 no_semantic_match 拒绝,不被指标复活(语义优先边界)。
    # 例外:中文问题(词元切分不到)在 dataset 零锚定时允许指标召回拉回锚点
    # ——指标别名/定义经字符重叠命中(见 _score_metric CJK 分支),此时指标
    # 本身就是"已声明语义"的证据,不是无关召回。无关问题仍零命中 → 拒绝。
    metric_hits: list[Any] = []
    if kb is not None and datasource and (
        matched or _CJK_RE.search(state.question)
    ):
        try:
            family = await kb.metric_family(
                state.question, datasource, matched_tables=matched or None)
            metric_hits = family.get("metrics") or []
            expanded = family.get("tables") or []
            # 指标沿 metric.datasets 扩展表锚 —— 这是唯一一处"引擎自己"往
            # matched 里加表的地方,主题域作用域必须在这里再收敛一次:
            # 只靠入口过滤挡不住它(域外指标一律会把域外数据集拉回来)。
            if scope is not None:
                expanded = [t for t in expanded if in_scope(scope, t)]
            if expanded and set(expanded) != set(matched):
                matched = expanded
        except Exception as e:
            logger.warning("metric_family failed (%s): %s", datasource, e)

    semantic_context = _render_semantic_context(
        model, matched, semantic_layer, state.question, metric_hits=metric_hits,
        topic=topic_res)

    base: dict[str, Any] = {
        "matched_tables": matched,
        "schema_context": semantic_context,
        "semantic_context": semantic_context,
        "link_detail": {
            "semantic_first": True,
            "matched_datasets": list(matched),
            "retry_round": retry_round,
            "tables_limit": _progressive_tables_limit(retry_round),
            "threshold": _progressive_threshold(retry_round),
            # 主题域只作事实记录(status="none" 时两键都不落,存量运行面板
            # 形状不变):评估归因要能分清"全模型零命中"与"域内零命中"。
            **({
                "topic": topic_res.topic.name,
                "topic_scope": list(topic_res.scope or []),
            } if topic_res.active else {}),
        },
    }

    # 查询时回灌已索引的物理 schema 元数据(schema_doc:表/列描述 + 枚举值),
    # 辅助 query_sketch 锚定列/枚举值。仅 pg_hybrid 后端(统一 PG 检索库)有数据;
    # 其他后端或库空 → 返回空,不注入,维持语义优先边界。只在已锚定数据集上
    # 注入(按 doc_id 的 schema:<table> 过滤),避免无关物理表污染作用域。
    if kb is not None and datasource and matched:
        try:
            schema_docs = await kb.search_schema_docs(state.question, datasource, limit=5)
            if schema_docs:
                matched_set = set(matched)
                lines = []
                for h in schema_docs:
                    tbl = h.doc_id.split(":", 1)[1] if h.doc_id.startswith("schema:") else ""
                    if tbl and tbl not in matched_set:
                        continue
                    lines.append(f"- {h.content}")
                if lines:
                    semantic_context = (
                        semantic_context
                        + "\n\nRetrieved schema notes:\n" + "\n".join(lines)
                    )
                    base["semantic_context"] = semantic_context
                    base["schema_context"] = semantic_context
                    base["link_detail"]["schema_doc_hits"] = len(lines)
        except Exception as e:
            logger.warning("schema_doc injection failed: %s", e)
    if not matched:
        # 零命中 = 未覆盖 = 拒绝(决策 4),不 fallback 全量表。
        # 带主题域时把域信息一并带上:拒绝对拒绝的补救方式不同 ——
        # 「模型缺声明」是找管理员补建模,「问题不在本主题域内」是换个域
        # 或把数据集并进域。refuse 节点据此给不同的用户文案。
        base["refusal"] = {
            "reason": "no_semantic_match",
            "question": state.question,
            "semantic_context": semantic_context,
            **({
                "topic": topic_res.topic.name,
                "topic_scope": list(topic_res.scope or []),
                "available_topics": topic_names(model),
            } if topic_res.active else {}),
        }
    return base










def make_schema_linking(
    kb: KbService | None = None,
    connectors: ConnectorRegistry | None = None,
    semantic_layer: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the schema_linking node — 语义优先(Phase B)唯一通道。

    Args:
        kb: Optional knowledge base for term matching (metric 锚定数据集)。
        connectors: Registry providing the active datasource name (KB scope).
        semantic_layer: Live semantic provider — 唯一可答边界;无语义模型
            → no_model 拒绝(决策 2/3)。

    Returns:
        Async node function taking WorkflowState and returning a partial update.
    """

    async def schema_linking(state: WorkflowState) -> dict[str, Any]:
        # Upstream node failed — pass through without running
        if state.error:
            return {}

        # Knowledge base is scoped to the active datasource
        datasource = state.datasource or (
            connectors.default_name if connectors is not None else ""
        )

        # 带上下文重跑：诊断文本参与检索，诊断中提到的表/术语可重新进入匹配
        search_query = (
            (state.question + "\n" + state.error_analysis).strip()
            if state.error_analysis else state.question
        )

        # 反思/纠错重跑轮:回滚到 schema_linking 时渐进放大候选(决策:首轮
        # 阈值/上限与旧行为一致,重跑轮放宽——匹配率自适应扩大的 trove 版)。
        retry_round = max(state.retry_count, 0)

        # 1. Knowledge base term matching (substring, works for Chinese)
        term_hits: list[TermHit] = []
        if kb is not None and datasource:
            await kb.ensure_synced(default_datasource=datasource)
            term_hits = await kb.search_terms(search_query, datasource)

        update = await _semantic_linking(
            state, kb, connectors, semantic_layer, term_hits, search_query, datasource,
            retry_round=retry_round,
        )

        # 检索来源标识:内置 SQLite FTS5 镜像 vs pg_hybrid 统一检索库
        # (仅作分析面板展示,不参与语义边界判定)。
        if kb is not None and datasource:
            try:
                rb = kb.retrieval_backend_label(datasource)
            except Exception:
                rb = "builtin"
            update["retrieval_backend"] = rb
            if isinstance(update.get("link_detail"), dict):
                update["link_detail"]["retrieval_backend"] = rb

        if term_hits:
            hits = [
                {
                    "kind": "term",
                    "term": h.term,
                    "mapping": h.mapping,
                    "definition": h.definition,
                    "tables": h.tables,
                }
                for h in term_hits
            ]
            update["kb_hits"] = hits
            # KB 术语命中进 langfuse(无 Langfuse 时 no-op)
            with record_span(
                "kb.hits", input={"question": state.question},
            ) as span:
                if span is not None:
                    span.update(output={
                        "hits": [{"term": h["term"], "mapping": h["mapping"]}
                                 for h in hits],
                    })
        return update

    return schema_linking
