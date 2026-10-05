"""历史蒸馏 —— 用户自己的行为记录 → 待审 KB 资产(零 LLM 核心 + 注入式提炼)。

「接入即建模」的冷启动不该是空白:每个真实部署在建模之前就已经积累了被
回答过的问答(episodes / 审计 / lineage)。本模块把这些**用户自己的行为
记录**蒸馏成三类待审资产,全部走既有确认门(自动内容永不绕过管理员确认,
与 ``semantic_layer.candidates`` / memory 子系统同一条纪律):

1. 成功问答 → pending 参考示例(``KbService.draft_example``;同
   question+sql 的 ``exists`` 返回自带幂等);
2. 成功 SQL 的聚合/列引用 → pending 语义候选
   (``SemanticManager.create_draft``)。metric/field 的锚定规则**直接复用**
   ``candidates._metric_spec/_field_spec`` 的保守实现(单聚合、全列限定、
   锚定唯一已声明数据集、名字未被占用;裸列 / CTE 遮蔽 / 未声明数据集
   一律不猜),只在两处历史特化:表名归一为数据集名、reason 换成
   ``history_*``(来路如实:这不是编译 MISS,是使用痕迹);
3. 失败与修正 → 教训材料(``history_lesson_evidence`` 纯映射);LLM 提炼
   (``run_lesson_distill``)的模型网关由**调用方注入**(CLI 注入自建网关,
   管理端任务注入 serve 的共享网关,测试注入 fake)—— 本模块自身不
   import、不构造网关,三个确定性核心的签名里没有 llm 参数(零 LLM 红线
   的同一条落法:构造上不可能凭空发起模型调用)。

红线(与仓库纪律同源):

- **输入路径白名单**(``validate_source_path``):只允许 ``{home}/memory/``、
  ``{home}/app.db``、``{project}/.trove/lineage/``。``eval/`` 评测产物
  刻意不在任何一个根里 —— 蒸馏只许用真实用户行为,不许用评测结果
  (KB 反作弊:``kb init`` 生成不出来的东西不算真实增益);
- **产物永不自动确认**:三个入口的 pending 语义即门位,管理员在收件箱
  逐条确认或拒绝;
- **用户归属不进产物**:蒸馏产物回答「问过什么」,不回答「谁问的」
  (隐私边界);
- ``MAX_DISTILL_PER_RUN`` 显式上限,幂等:同输入重跑第二遍产出 0 条。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 批量蒸馏的显式上限:一次运行最多消费这么多条历史记录(示例 / 语义候选
# 各自再按同一上限截断)。管理端主动触发的一次动作,不该变成无限回灌。
MAX_DISTILL_PER_RUN = 50

# 收集窗口倍数:频次权重可能把「最近窗口之外但被反复执行」的记录顶进前
# limit 名,多读一点再按权重截断。
_FETCH_FACTOR = 4

_SUCCESS_HEADS = ("OK", "EMPTY")

# reason 前缀:``auto:{reason}:{question}`` 的草稿 note 里如实标出来路。
_HISTORY_METRIC_REASON = "history_metric_usage"
_HISTORY_FIELD_REASON = "history_field_usage"

_VERDICT_TOKEN_RE = re.compile(r"^\s*([A-Za-z_]+)")


class HistoryDistillError(Exception):
    """蒸馏输入/路径错误 —— 显式报错,绝不静默成功。"""


@dataclass
class HistoryRecord:
    """一条归一化的历史行为记录(episodes 与审计的并集形状)。

    刻意不带 user_id/username:下游产物不落用户归属,记录层就不携带,
    免得任何一环顺手把「谁问的」漏进示例/草稿。
    """

    question: str
    sql: str
    verdict: str = ""
    corrections: list[str] = field(default_factory=list)
    error: str = ""
    source: str = "episode"  # episode | audit
    last_seen: str = ""
    runs: int = 0  # lineage 频次权重(0 = 未记录)


def _verdict_head(verdict: str) -> str:
    """``OK`` / ``RETRY: …`` / ``NO_SQL`` → 首个词的上档形态(空 = 无判定)。"""
    m = _VERDICT_TOKEN_RE.match(verdict or "")
    return m.group(1).upper() if m else ""


def is_success(record: HistoryRecord) -> bool:
    """成功口径(与运行时 observe 的示例门一致):OK/EMPTY;空 verdict 且
    无 error 无修正史也算交付过;``RETRY: …`` 等其余判定一律排除。"""
    head = _verdict_head(record.verdict)
    if head in _SUCCESS_HEADS:
        return True
    if head:
        return False
    return not record.error and not record.corrections


def is_lesson_material(record: HistoryRecord) -> bool:
    """教训材料:失败轮,或成功但带修正史(反复纠正 = 值得沉淀的模式)。"""
    return (not is_success(record)) or bool(record.corrections)


def history_lesson_evidence(record: HistoryRecord) -> dict[str, Any]:
    """HistoryRecord → ``lesson_distill.build_distill_prompt`` 的 failure 形状。

    纯映射(零 LLM)。历史里没有 gold_sql(不如实造一个);失败上下文 =
    修正史(episodes 侧自带,正是「哪里反复出错」的信号)或审计 error;
    塞进 ``evidence`` 的正是数据原文(该字段按数据扫,见 lesson_distill
    的歧义名处置)。
    """
    evidence = "\n".join(record.corrections) if record.corrections else record.error
    return {
        "question": record.question,
        "evidence": evidence,
        "gold_sql": "",
        "pred_sql": record.sql,
        "error": record.error or record.verdict,
    }


# ── 输入路径白名单(反作弊红线) ──────────────────────────


def validate_source_path(
    path: str | Path, *, home_dir: str | Path, project_root: str | Path,
) -> Path:
    """行为记录白名单:只放行三个根,其余(尤其 ``eval/``)硬拒。

    允许根:
      - ``{home}/memory/``(episodes 等统一记忆库)
      - ``{home}/app.db``(集中审计库)
      - ``{project}/.trove/lineage/``(执行过的查询频次)

    评测产物(``.trove/eval/``)刻意不在任何根里:蒸馏只许用用户自己的
    行为记录,指向它们的输入在任何调用点都会被拒 —— 这是 KB 反作弊在
    数据入口处的一道闸,不是文档里的君子协定。
    """
    p = Path(path).expanduser().resolve()
    home = Path(home_dir).expanduser().resolve()
    root = Path(project_root).expanduser().resolve()
    for base in (home / "memory", home / "app.db", root / ".trove" / "lineage"):
        if p == base or base in p.parents:
            return p
    raise HistoryDistillError(
        f"输入路径不在行为记录白名单内({p});只允许 {home}/memory/、"
        f"{home}/app.db、{root}/.trove/lineage/ —— 评测产物(eval/)绝不进蒸馏"
    )


# ── 收集(episodes ∪ 审计 → 去重 → lineage 权重排序 → 截断) ──


async def collect_history(
    datasource: str,
    *,
    home_dir: str | Path,
    project_root: str | Path,
    auth_store: Any | None = None,
    since: str = "",
    limit: int = MAX_DISTILL_PER_RUN,
) -> list[HistoryRecord]:
    """收集一个数据源的行为记录,按价值排序后截断。

    - episodes(home 统一记忆库)优先:唯一 question+SQL+verdict 三全、
      还带修正史的源;审计(home/app.db 的 ``query.execute``)兜底 ——
      记忆子系统未启用/开启前的历史只在审计里;
    - lineage 只当**排序权重**(它单独不可蒸馏:只有 SQL 与频次,没有
      提问原文);权重高的 SQL 排在前面,截断时先保住它们;
    - 同一 (question, sql) 只留一条(episodes 版本优先,它信息更多),
      (kind 无关的)记录级去重后按 ``(-runs, 时间倒序)`` 排序截断。

    空结果 = **没有可蒸馏的历史** —— 调用方(CLI)必须显式报「无历史」
    而不是报成功(照「查不成的绿是假绿」纪律)。
    """
    from trove.services.lineage.service import LineageService
    from trove.services.lineage.parse import normalization_key
    from trove.services.memory.episode import EpisodeStore

    limit = max(0, min(int(limit or 0), MAX_DISTILL_PER_RUN))
    if not datasource or limit == 0:
        return []

    episodes_path = validate_source_path(
        Path(home_dir).expanduser() / "memory" / "episodes.sqlite",
        home_dir=home_dir, project_root=project_root,
    )
    lineage = LineageService(project_root)

    fetch = max(limit * _FETCH_FACTOR, limit)
    records: list[HistoryRecord] = []
    seen: set[tuple[str, str]] = set()

    def _add(rec: HistoryRecord) -> None:
        key = (rec.question.strip().lower(), rec.sql.strip())
        if key in seen:
            return  # episodes 先入:审计侧的同一问答不再重复
        seen.add(key)
        records.append(rec)

    store: EpisodeStore | None = None
    try:
        if episodes_path.exists():  # 空 home 不造文件:_conn 会建库
            store = EpisodeStore(episodes_path)
            try:
                for row in await store.iter_episodes(
                    datasource, since=since or None, limit=fetch,
                ):
                    _add(HistoryRecord(
                        question=row["question"], sql=row["sql"],
                        verdict=row["verdict"],
                        corrections=[str(c) for c in row["correction_history"]],
                        source="episode", last_seen=row["updated_at"],
                    ))
            except Exception as e:  # 读失败不吞:记录为空会被上层报成「无历史」
                logger.warning("episode history read failed (%s): %s", datasource, e)

        if auth_store is not None:
            try:
                rows = await auth_store.aggregate_query_audit(
                    datasource=datasource, since=since, limit=fetch,
                )
            except Exception as e:
                logger.warning("audit history read failed (%s): %s", datasource, e)
                rows = []
            for row in rows:
                _add(HistoryRecord(
                    question=row["question"], sql=row["sql"],
                    verdict=row["verdict"], error=row["error"],
                    source="audit", last_seen=row["last_seen"],
                    runs=int(row.get("seen") or 0) - 1,  # 审计计数当低频权重
                ))

        # lineage 频次覆盖权重(真实执行次数,比审计窗口内的 seen 更权威)。
        weights: dict[str, int] = {}
        try:
            weights = await lineage.query_weights(datasource, since=since or None)
        except Exception as e:
            logger.warning("lineage weight read failed (%s): %s", datasource, e)
        for rec in records:
            w = weights.get(normalization_key(rec.sql))
            if w is not None:
                rec.runs = int(w)

        # 先按时间倒序(稳定),再按权重倒序稳定排序 → (-runs, -time) 合成序。
        records.sort(key=lambda r: r.last_seen, reverse=True)
        records.sort(key=lambda r: r.runs, reverse=True)
        return records[:limit]
    finally:
        # 这个 store 是本次调用创建的(短生命周期所有者),用完即放:
        # aiosqlite 的 worker 线程不 dispose 会挂住进程退出(CLI 跑完不退出)。
        if store is not None:
            await store.dispose()


# ── 成功 SQL → 语义候选规格(纯函数) ──────────────────────


class _TableContext:
    """SQL 引用解析上下文:表别名 → 基表;CTE/子查询别名 → 遮蔽(不锚)。

    别名绑定出现歧义(同名别名指向不同基表)→ 别名整体作废,按遮蔽处理
    —— 宁缺勿错,孤儿引用绝不硬猜。
    """

    def __init__(self, model: Any, tree: Any):
        from sqlglot import exp

        by_source: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for d in getattr(model, "datasets", []) or []:
            by_name[str(d.name).lower()] = d.name
            src = str(getattr(d, "source", "") or "").strip()
            if src:
                by_source[src.lower()] = d.name
        self._by_source, self._by_name = by_source, by_name

        shadows: set[str] = set()
        aliases: dict[str, str] = {}
        ambiguous: set[str] = set()
        for cte in tree.find_all(exp.CTE):
            a = str(cte.alias or "").lower()
            if a:
                shadows.add(a)
        for sub in tree.find_all(exp.Subquery):
            a = str(sub.alias or "").lower()
            if a:
                shadows.add(a)
        for tbl in tree.find_all(exp.Table):
            a = str(tbl.alias or "").lower()
            base = str(tbl.name or "").lower()
            if not a or not base:
                continue
            if aliases.get(a, base) != base:
                ambiguous.add(a)
            aliases.setdefault(a, base)
        for a in ambiguous:
            aliases.pop(a, None)
            shadows.add(a)
        self._shadows, self._aliases = shadows, aliases

    def dataset_for_table(self, table: str) -> str | None:
        """表引用 → 已声明数据集名;解析不了 → None。"""
        t = (table or "").lower()
        if not t:
            return None
        if t in self._aliases:
            t = self._aliases[t]
        elif t in self._shadows:
            return None
        return self._by_source.get(t) or self._by_name.get(t)

    def normalize(self, expr: Any) -> str | None:
        """表达式 → 数据集限定文本;任一列锚不住/裸列 → None(不猜)。"""
        from sqlglot import exp

        if not list(expr.find_all(exp.Column)):
            return None
        out = expr.copy()
        for col in out.find_all(exp.Column):
            t = str(col.table or "").lower()
            name = str(col.name or "")
            if not t or not name or "*" in name:
                return None
            ds = self.dataset_for_table(t)
            if ds is None:
                return None
            col.set("table", exp.to_identifier(ds))
        return out.sql()


def sql_candidate_specs(sql: str, model: Any) -> list[dict[str, Any]]:
    """成功 SQL → 候选规格(metric 优先;解析不了/锚不住一律不猜)。

    每个 SELECT 的投影逐个过 ``_metric_spec``(单聚合 / 全列限定 / 锚定
    唯一已声明数据集 / 名字未被占用 —— 规则真源在 candidates,这里只做
    表名 → 数据集名的归一);全语句的限定列引用过 ``_field_spec``(未声明
    字段才落)。CTE 遮蔽、表别名歧义、未声明数据集、裸列都按不可锚定跳过。
    """
    from sqlglot import exp, parse_one
    from sqlglot.errors import ErrorLevel

    from trove.services.semantic_layer.candidates import (
        _field_spec,
        _metric_spec,
    )

    if model is None or not (sql or "").strip():
        return []
    try:
        tree = parse_one(sql, error_level=ErrorLevel.RAISE)
    except Exception:
        return []

    ctx = _TableContext(model, tree)
    metrics: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for sel in tree.find_all(exp.Select):
        for proj in sel.expressions:
            expr = proj.this if isinstance(proj, exp.Alias) else proj
            if list(expr.find_all(exp.Window)):
                continue  # 窗口算式不是度量声明(口径归人工)
            component = ctx.normalize(expr)
            if component is None:
                continue
            spec = _metric_spec(component, model)
            if spec is None:
                continue
            spec["reason"] = _HISTORY_METRIC_REASON
            key = (spec["kind"], spec["name"])
            if key not in seen:
                seen.add(key)
                metrics.append(spec)

    for col in tree.find_all(exp.Column):
        t = str(col.table or "").lower()
        name = str(col.name or "")
        if not t or not name or "*" in name:
            continue
        ds = ctx.dataset_for_table(t)
        if ds is None:
            continue
        spec = _field_spec(_HISTORY_FIELD_REASON, f"{ds}.{name}", model)
        if spec is None:
            continue
        key = (spec["kind"], spec["name"])
        if key not in seen:
            seen.add(key)
            fields.append(spec)

    return metrics + fields


def _dedupe_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨记录去重((kind, name),首个胜出)+ metric 优先稳定分区。"""
    seen: set[tuple[str, str]] = set()
    metrics: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    for spec in specs:
        key = (str(spec.get("kind")), str(spec.get("name")))
        if key in seen:
            continue
        seen.add(key)
        (metrics if spec.get("kind") == "metric" else fields).append(spec)
    return metrics + fields


# ── 落库(确定性;示例 + 语义候选) ─────────────────────────


async def run_distill(
    kb: Any,
    datasource: str,
    records: list[HistoryRecord],
    *,
    dry_run: bool = False,
    max_items: int = MAX_DISTILL_PER_RUN,
) -> dict[str, Any]:
    """records → pending 示例 + pending 语义候选(零 LLM;返回计数与明细)。

    - 成功记录 → ``draft_example``(既有 ``exists`` 语义即幂等闸);
    - 成功 SQL → 对当前语义模型抽候选规格;``(kind, name)`` 在**任何**
      草稿状态(pending/applied/rejected)出现过都跳过 —— 管理员拒绝过
      的候选不再回灌,确认过的名字本来就已被声明(taken 规则)。
    - ``max_items`` 对示例与候选**各自**截断(显式上限;批量 ≠ 无限)。
    - ``dry_run=True`` 只算不写;单条失败不连坐整批(照 capture_candidates)。
    """
    out: dict[str, Any] = {
        "examples": 0, "examples_skipped": 0,
        "candidates": 0, "candidates_skipped": 0,
        "example_items": [], "candidate_items": [],
        "dry_run": dry_run,
    }
    if kb is None or not datasource or not records:
        return out
    max_items = max(0, min(int(max_items or 0), MAX_DISTILL_PER_RUN))
    successes = [r for r in records if is_success(r) and r.question and r.sql]

    # 1) 示例(成功问答)
    for rec in successes:
        if out["examples"] >= max_items:
            break
        if dry_run:
            out["examples"] += 1
            out["example_items"].append(
                {"question": rec.question, "sql": rec.sql})
            continue
        try:
            result = await kb.draft_example(
                rec.question, rec.sql, datasource,
                tags=["history"], note="distilled from query history",
                generator="history_distill",
            )
        except Exception as e:  # 单条失败不连坐
            logger.warning("history example draft failed (%s): %s", datasource, e)
            continue
        if result.get("status") == "drafted":
            out["examples"] += 1
            out["example_items"].append(
                {"question": rec.question, "sql": rec.sql})
        else:
            out["examples_skipped"] += 1

    # 2) 语义候选(成功 SQL 的使用痕迹)
    from trove.services.semantic_layer.manage import SemanticManager

    try:
        manager = SemanticManager(kb)
        model = manager.model(datasource)
    except Exception as e:
        logger.warning("history distill model lookup failed (%s): %s", datasource, e)
        return out
    raw_specs: list[dict[str, Any]] = []
    for rec in successes:
        for spec in sql_candidate_specs(rec.sql, model):
            raw_specs.append({**spec, "question": rec.question})
    specs = _dedupe_specs(raw_specs)
    if specs:
        try:
            existing = {
                (str(d.get("kind") or ""), str(d.get("name") or ""))
                for entries in manager.drafts(datasource).values()
                for d in entries
            }
        except Exception:
            existing = set()  # 读队列失败不挡写入:宁可多一条去重由人看
        for spec in specs:
            if out["candidates"] >= max_items:
                break
            if (spec["kind"], spec["name"]) in existing:
                out["candidates_skipped"] += 1
                continue
            item = {
                "kind": spec["kind"], "name": spec["name"],
                "question": spec.get("question", ""),
            }
            if dry_run:
                out["candidates"] += 1
                out["candidate_items"].append(item)
                continue
            try:
                await manager.create_draft(
                    datasource, spec["kind"], "upsert", spec["name"],
                    payload=spec["payload"],
                    note=f"auto:{spec['reason']}:{str(spec.get('question') or '')[:100]}",
                )
            except Exception as e:
                logger.warning(
                    "history candidate draft write failed (%s, %s): %s",
                    datasource, spec["name"], e)
                continue
            out["candidates"] += 1
            out["candidate_items"].append(item)
    return out


# ── 落库(教训;网关由调用方注入) ───────────────────────────


async def run_lesson_distill(
    kb: Any,
    datasource: str,
    records: list[HistoryRecord],
    *,
    llm: Any,
    model: str,
    dry_run: bool = False,
    max_items: int = MAX_DISTILL_PER_RUN,
) -> dict[str, Any]:
    """records → pending 教训(提炼;LLM 网关由调用方注入)。

    CLI 与管理端蒸馏任务**共用**这一条管线(同一段提示词、同一套过滤),
    差别只在注入的网关实例。``llm`` 是调用方给的网关对象(鸭子类型,只用
    ``llm.chat`` 一个方法;见模块 docstring:本模块不 import 也不构造它)。

    - 材料 = ``is_lesson_material`` 且带提问原文的记录,截断到 ``max_items``
      (显式上限;批量 ≠ 无限);
    - 每条材料一次提炼:解析失败 / 管线噪声各自计数跳过;通过的条目再与
      ``lessons.yml`` 既有 pattern 及批内重复去重(``dedupe_by_pattern``
      既有管线)—— 重跑第二遍自然 0 条;
    - ``dry_run=True`` 只算不写;
    - **失败响亮**:任一条提炼抛错即返回 ``error``,此时磁盘**一个字节都
      没改**(写入只在全部提炼完成后发生),调用方如实报失败不报成功;
      单条落库失败不连坐整批(与示例/候选同纪律)。

    返回 ``{material, lessons, duplicates, noise, parse_failed, items,
    dry_run, error}``:``lessons`` = 通过解析与噪声过滤的条数,
    ``duplicates`` = 其中与既有 pattern 重复的条数,实际写入
    ``lessons - duplicates`` 条;``items`` = 写入(或 dry_run 下应写入)的
    ``{pattern, note, question}`` 明细。
    """
    out: dict[str, Any] = {
        "material": 0, "lessons": 0, "duplicates": 0, "noise": 0,
        "parse_failed": 0, "items": [], "dry_run": dry_run, "error": "",
    }
    if kb is None or not datasource or not records:
        return out
    if llm is None:
        out["error"] = "lesson distill requires an injected llm gateway"
        return out

    from trove.prompts import render
    from trove.services.kb.lesson_distill import (
        build_distill_prompt,
        dedupe_by_pattern,
        is_noise_lesson,
        parse_lesson,
    )

    max_items = max(0, min(int(max_items or 0), MAX_DISTILL_PER_RUN))
    material = [r for r in records if is_lesson_material(r) and r.question]
    material = material[:max_items]
    out["material"] = len(material)
    if not material:
        return out

    system = render("lesson_distill/system")
    pairs: list[tuple[HistoryRecord, dict[str, Any]]] = []
    for rec in material:
        failure = history_lesson_evidence(rec)
        try:
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": build_distill_prompt(failure)},
                ],
                max_tokens=16000,
            )
        except Exception as e:
            # 写入只在提炼全部完成后发生 —— 这里返回时磁盘零改动。
            out["error"] = str(e)
            return out
        lesson = parse_lesson(response)
        if lesson is None:
            out["parse_failed"] += 1
            continue
        if is_noise_lesson(rec.question, lesson):
            out["noise"] += 1
            continue
        pairs.append((rec, lesson))

    out["lessons"] = len(pairs)
    try:
        # 读既有 pattern 前先懒同步镜像(与所有 KB 读路径同一纪律:镜像表
        # 由 sync 建立,直接 _rows 会在全新 KB 上撞 "no such table")。
        await kb.ensure_synced(datasource)
        existing = await kb.list_lessons(datasource, confirmed_only=False)
    except Exception as e:
        out["error"] = f"list_lessons failed: {e}"
        return out
    fresh = dedupe_by_pattern([lesson for _, lesson in pairs], existing)
    out["duplicates"] = len(pairs) - len(fresh)
    # dedupe_by_pattern 原地返回入选条目(同一批 dict 对象)——按身份认回
    # 来源记录;若哪天它改成拷贝语义,这里会 KeyError 响亮报错而不是静默
    # 丢归属。
    by_id = {id(lesson): rec for rec, lesson in pairs}
    fresh_pairs = [(by_id[id(lesson)], lesson) for lesson in fresh]
    out["items"] = [
        {"pattern": str(lesson.get("pattern") or ""),
         "note": str(lesson.get("note") or ""),
         "question": rec.question}
        for rec, lesson in fresh_pairs
    ]
    if dry_run:
        return out
    kept: list[dict[str, Any]] = []
    for (rec, lesson), item in zip(fresh_pairs, out["items"]):
        try:
            await kb.append_lesson(
                {**lesson, "confirmed": False}, datasource,
                generator="history_distill",
            )
        except Exception as e:  # 单条失败不连坐
            logger.warning(
                "history lesson write failed (%s, %s): %s",
                datasource, lesson.get("pattern"), e)
            continue
        kept.append(item)
    out["items"] = kept
    return out
