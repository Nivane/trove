"""装前试跑(``trove validate --run``)—— 语料层 + 双态消融,零 LLM/零网络。

实施稿 §03 g / §04 E3 的落点。四条设计契约:

- **语料双源,能力诚实**:``.trove/kb/<ds>/fixtures.yml``(随 KB 树入库,
  带结果行)+ episodes(``iter_episodes``,管理端语义、跨用户;只有 SQL)。
  语料能力映射到「哪档可回放」——episodes 条目对 validator 档(结果域)
  一律计 ``skipped``,绝不假装判过;
- **双态消融**:同一语料跑两遍 —— 「不装候选资产」与「装候选资产」,
  两组判定的差 = 该资产的影响。validator 档直接拿 fixtures 的 rows/columns
  喂 ``run_validators``(纯内存,不执行 SQL);guard 档(SQL 域)走**惰性装载**
  的 ``trove.services.skills.guards``(车道 A/E2) —— 模块不存在时整档计
  skipped,不报错、不假绿。接口契约见 ``load_guard_tier``;
- **三计数诚实**:covered / skipped / errored 逐条判定计数 + 逐条差分表。
  「没有差异」永远不能和「没有验」混为一谈 —— 全跳过(covered == 0)不是绿,
  退出码 2;
- **退出码三分支**:``0`` 无回归 · ``1`` 有拦截变更/断言失败 · ``2`` 无法试跑
  (语料缺失/格式错/路径上需要 LLM/判定执行出错)——绝不静默 0。与
  ``scripts/eval_gate.py`` 同一精神:门必须能红。

隐私(R5):报告默认问题文本哈希短码化(``question_id``),``include_questions``
才带原文;报告行不落 SQL 原文、不落会话/用户标识。episodes 是跨用户行为
记录,只在本机内存中过一遍。

零 LLM 硬门:本模块不 import 任何 LLM 组件,试跑路径全部为纯函数 + 只读
查询;测试用「LLM 被调用即 raise」的 autouse fixture 钉死 —— 不是纪律,是机制。

fixtures.yml 格式(``.trove/kb/<ds>/fixtures.yml``)::

    items:
      - question: "How many loans are there?"   # 必填
        sql: "SELECT COUNT(*) FROM loan"        # 可选(guard 档回放用)
        dialect: mysql                          # 可选
        columns: [loan_count]                   # 与 rows 成对出现
        rows:                                   # 结果行(validator 档回放用)
          - [3]
        verdict: pass                           # 可选断言:pass|violated|unjudged

``verdict`` 是**断言**:该条目在「装」态的判定(``_state_of_hits``)必须与它
一致,不一致 = 断言失败 → 退出码 1;不写 = 只报告不判定。判定按档生效 ——
同一语料在 validator 与 guard 两档各自的判定都要与声明的 verdict 一致。
判不了(unjudged)与判过(pass)是两种结论,写 ``pass`` 而引擎判不了同样算
断言失败 —— 「没验」不许冒充「验过」。

报告 ``to_dict()["metrics"]`` 直接可喂 ``scripts/eval_gate.py``(形状对齐
``trove/eval/gate.py`` 的 scorecard json 消费面:顶层 ``metrics`` 映射到数值,
``score_from_file`` / ``compare_metrics`` 原样可读)。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from trove.core.logging import get_logger
from trove.services.skills.service import SkillService
from trove.services.skills.validators import VALIDATOR_HOST, run_validators

logger = get_logger(__name__)

#: fixtures 文件名(落在 ``.trove/kb/<datasource>/`` 下,随 KB 树入库)。
FIXTURES_NAME = "fixtures.yml"

#: 每个语料源的默认条数上限(§03 h:--limit 默认 200/源)。上限按源生效
#: (episodes 在查询层、fixtures 在装载层)—— 合并后再总限会让后一源被前一源
#: 整段挤掉,「限量」就变成了「只跑 fixtures」。
DEFAULT_LIMIT = 200

#: 两种判定档:validator = 结果域(执行后)、guard = SQL 域(执行前,E2)。
TIERS = ("validator", "guard")

#: 判定三态(语料条目在一档上的结论)与 fixtures ``verdict`` 断言词表同源。
VERDICTS = ("pass", "violated", "unjudged")

#: ``state == "skipped"`` 的机器可读原因码闭集(每个码指得出是哪一步没跑)。
SKIP_REASONS = (
    "no_result_rows",               # validator 档:语料没有结果行(episodes / 只写 SQL 的 fixture)
    "no_sql",                       # guard 档:语料没有 SQL
    "guards_module_absent",         # guard 档:树里没有 guards 模块(降级树),整档不可用
    "guard_runner_missing",         # guards 模块在,但没有 run_guards —— 坏树
    "guards_module_import_failed",  # guards 模块在但导入就炸 —— 坏树(同时进 errors)
)

#: ``state == "errored"`` 的原因码:判定**跑过但炸了** —— 一律退出码 2。
ERROR_REASONS = ("runner_error",)

#: guard 档的接线契约(E2/车道 A 的合流点):
#: ``run_guards(specs, *, sql, dialect, lang) -> list[dict]`` —— 与
#: ``run_validators`` 同形(判定内核共享,§03 c),hit 形状 name/verdict/
#: severity/message,判定值 True 通过 / False 违反 / None 判不了。
GuardRunner = Callable[..., list[dict]]


# ── 语料 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CorpusItem:
    """一条语料 —— 试跑与影响面回放(E4)共享的输入形状(§03 g)。"""

    question: str
    sql: str = ""
    dialect: str = ""
    rows: list[list] | None = None
    columns: list[str] | None = None
    expected: str = ""       # fixtures ``verdict`` 的映射;"" = 不断言
    source: str = ""         # "fixtures:<path>" | "episodes"

    @property
    def has_result(self) -> bool:
        """带结果行 —— validator 档(结果域)可回放。"""
        return self.rows is not None and self.columns is not None

    @property
    def question_id(self) -> str:
        """问题文本的哈希短码 —— R5:报告默认只出短码,不出原文。"""
        return hashlib.sha256(self.question.encode("utf-8")).hexdigest()[:8]

    @property
    def dedupe_key(self) -> tuple[str, str]:
        """跨源去重键:归一化问题 + SQL(episodes 与 fixtures 可能是同一问)。"""
        return (self.question.strip().lower(), self.sql.strip())


@dataclass
class Corpus:
    """装前试跑的语料集(双源合并、去重后的结果)。"""

    items: list[CorpusItem] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # 人类可读的源摘要(报告用)
    errors: list[str] = field(default_factory=list)    # 语料级错误 → 退出码 2
    deduped: int = 0                                   # 去重掉的条数(诚实计数)


def _parse_fixture_item(
    raw: Any, *, index: int, source: str,
) -> tuple[CorpusItem | None, str]:
    """一条 fixture → (语料条目, 错误)。错误非空时条目为 None。"""
    where = f"items[{index}]"
    if not isinstance(raw, dict):
        return None, f"{where} 必须是 mapping"
    question = str(raw.get("question") or "").strip()
    if not question:
        return None, f"{where}.question 必填"
    verdict = str(raw.get("verdict") or "").strip()
    if verdict and verdict not in VERDICTS:
        return None, f"{where}.verdict 必须是 {VERDICTS} 之一,得到 {verdict!r}"
    columns, rows = raw.get("columns"), raw.get("rows")
    if (columns is None) != (rows is None):
        # 半份结果(只写列或只写行)比没有结果更坏:看起来可回放、实际判不了。
        return None, f"{where}.rows 与 columns 必须成对出现"
    if columns is not None:
        if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
            return None, f"{where}.columns 必须是字符串列表"
        if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
            return None, f"{where}.rows 必须是「列表的列表」"
        columns = list(columns)
        rows = [list(r) for r in rows]
    return CorpusItem(
        question=question,
        sql=str(raw.get("sql") or "").strip(),
        dialect=str(raw.get("dialect") or "").strip(),
        rows=rows,
        columns=columns,
        expected=verdict,
        source=source,
    ), ""


def load_fixtures_file(path: str | Path) -> tuple[list[CorpusItem], list[str]]:
    """读一个 fixtures.yml → (语料条目, 错误列表)。

    格式错误是**响亮的**:结构不对一律进 errors(调用方据此退出码 2),绝不
    「跳过这条继续」—— 静默丢条目会让「没有差异」把「没读懂」盖过去,
    正是 eval_gate 精神要拦的那类假绿。
    """
    p = Path(path)
    if not p.exists():
        return [], [f"fixtures 文件不存在: {p}"]
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        return [], [f"fixtures 无法解析: {p} ({exc})"]
    if doc is None:
        return [], [f"fixtures 为空: {p}"]
    if not isinstance(doc, dict):
        return [], [f"fixtures 根必须是 mapping(含 items): {p}"]
    raw_items = doc.get("items")
    if not isinstance(raw_items, list):
        return [], [f"fixtures.items 必须是列表: {p}"]

    source = f"fixtures:{p}"
    items: list[CorpusItem] = []
    errors: list[str] = []
    for i, raw in enumerate(raw_items):
        item, err = _parse_fixture_item(raw, index=i, source=source)
        if err:
            errors.append(err)
        elif item is not None:
            items.append(item)
    return items, errors


def resolve_fixtures_path(
    project_root: Path, datasource: str, fixtures: str,
) -> tuple[Path | None, bool]:
    """``--fixtures`` 参数 → (路径, 是否显式)。

    ``auto``(缺省)= ``.trove/kb/<datasource>/fixtures.yml``;显式路径由调用方
    自己负责存在性 —— 显式给了却不存在是「输入错」(退出码 2),而 auto 缺
    文件只是「这个源没有 fixtures」(报告里如实标注)。
    """
    explicit = bool(fixtures) and fixtures != "auto"
    if explicit:
        return Path(fixtures).expanduser(), True
    if not datasource:
        return None, False
    return project_root / ".trove" / "kb" / datasource / FIXTURES_NAME, False


async def load_episodes_corpus(
    datasource: str,
    *,
    home_dir: str | Path,
    limit: int = DEFAULT_LIMIT,
    store: Any | None = None,
) -> tuple[list[CorpusItem], str, list[str]]:
    """episodes → 语料(管理端语义、跨用户;只有 SQL,没有结果行)。

    返回 ``(条目, 源摘要, 错误)``。读取失败是**错误不是空语料** —— 把读不出
    的库降级成「0 条」会让试跑静默变绿;库不存在(还没产生过行为记录)才是
    如实的「无」。

    ``store`` 供测试注入;自建 store 用完 ``dispose()`` —— aiosqlite 的 worker
    线程是常驻非 daemon,不 dispose 会让进程在跑完后挂住。
    """
    if not datasource:
        return [], "episodes: 未点名数据源", []
    path = Path(home_dir).expanduser() / "memory" / "episodes.sqlite"
    own = store is None
    if own:
        if not path.exists():
            return [], f"episodes: 无（{path} 不存在）", []
        from trove.services.memory.episode import EpisodeStore

        store = EpisodeStore(path)
    try:
        rows = await store.iter_episodes(datasource, limit=limit)
    except Exception as exc:  # noqa: BLE001 — 读取失败 = 无法试跑,绝不静默降级
        logger.warning("dryrun: episodes 读取失败 (%s): %s", datasource, exc)
        return [], f"episodes: 读取失败（{exc}）", [f"episodes 读取失败: {exc}"]
    finally:
        if own:
            await store.dispose()
    items = [
        CorpusItem(
            question=str(r.get("question") or "").strip(),
            sql=str(r.get("sql") or "").strip(),
            dialect=str(r.get("dialect") or "").strip(),
            source="episodes",
        )
        for r in rows
        if str(r.get("question") or "").strip()
    ]
    return items, f"episodes: {len(items)} 条", []


def merge_corpus(
    fixtures: list[CorpusItem], episodes: list[CorpusItem],
) -> tuple[list[CorpusItem], int]:
    """两源合并 + 跨源去重 → ``(条目, 去重条数)``。

    去重键 = 归一化问题 + SQL:episodes 会把 fixtures 里写过的同一问答再记
    一遍(fixtures 的存在意义就是「这条被真实问过」),不去重则同一题在报告
    里判两次,差分计数会被同一资产污染。fixtures 先入(带结果行的那份胜出)。
    """
    out: list[CorpusItem] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for item in [*fixtures, *episodes]:
        if item.dedupe_key in seen:
            dropped += 1
            continue
        seen.add(item.dedupe_key)
        out.append(item)
    return out, dropped


async def gather_corpus(
    *,
    datasource: str,
    project_root: Path,
    home_dir: Path,
    fixtures: str = "auto",
    episodes: bool = False,
    limit: int = DEFAULT_LIMIT,
    episode_store: Any | None = None,
) -> Corpus:
    """两源语料 → 合并去重后的 Corpus(错误全部落 ``Corpus.errors``)。

    与 ``run_validate`` 同一条纪律:坏输入不抛异常,而是变成**报告里的错误**
    (调用方据此退出码 2)——试跑最需要它的时刻,恰是输入坏了的时候。
    """
    corpus = Corpus()
    if not datasource:
        # 语料按数据源组织:fixtures 路径 / episodes 键 / validator 候选集
        # 都按源走 —— 没点名数据源就是**无法试跑**,不是「全部源都跑一遍」。
        corpus.errors.append("--run 需要点名数据源(--datasource):语料按数据源组织")
        return corpus

    # ── fixtures(上限在装载层)──
    path, explicit = resolve_fixtures_path(project_root, datasource, fixtures)
    fixtures_items: list[CorpusItem] = []
    if path is None:
        corpus.sources.append("fixtures: 未指定")
    elif not path.exists():
        # 显式给了路径却不存在 = 输入错(退出码 2);auto 缺文件 = 这个源没有
        # fixtures(如实标注,交给出码的「语料为空」兜底)。
        if explicit:
            corpus.errors.append(f"fixtures 文件不存在: {path}")
        corpus.sources.append(f"fixtures: 无（{path}）")
    else:
        items, errors = load_fixtures_file(path)
        corpus.errors.extend(errors)
        cap = max(0, int(limit))
        fixtures_items = items[:cap]
        if len(items) > cap:
            corpus.sources.append(
                f"fixtures: {cap}/{len(items)} 条（{path};--limit 截断）")
        else:
            corpus.sources.append(f"fixtures: {len(items)} 条（{path}）")

    # ── episodes(跨用户,显式开;上限在查询层)──
    ep_items: list[CorpusItem] = []
    if episodes:
        ep_items, summary, errors = await load_episodes_corpus(
            datasource, home_dir=home_dir, limit=limit, store=episode_store,
        )
        corpus.errors.extend(errors)
        corpus.sources.append(summary)
    else:
        corpus.sources.append("episodes: 未启用（--episodes）")

    corpus.items, corpus.deduped = merge_corpus(fixtures_items, ep_items)
    return corpus


# ── 判定(双态消融)────────────────────────────────────────


def _hit_get(hit: Any, *names: str) -> Any:
    """读 hit 的一个字段 —— **mapping 与对象两种形状都认**。

    判定内核的 validator 侧交的是 dict(E2 的 guard 侧 ``run_guards`` 交的是
    ``GuardVerdict`` dataclass),两者是同一套内核的两种序列化形态。只认
    mapping 的读法会把每一条真实守卫判定读成"判不了"(None)—— 那既不是
    "通过"也不是"违反",而是**每次都没判**,而报告上它看起来只是"没有差异"。
    这里按名字逐个取:mapping 走键,对象走属性;取不到才给 None。
    """
    if isinstance(hit, Mapping):
        for name in names:
            if name in hit:
                return hit[name]
        return None
    for name in names:
        if hasattr(hit, name):
            return getattr(hit, name)
    return None


def _hit_readable(hit: Any) -> bool:
    """这份 hit 是不是我们认识的形状(否则按"认不出"兜底,不假装判过)。"""
    return isinstance(hit, Mapping) or hasattr(hit, "name")


def _hit_verdict(hit: Any) -> bool | None:
    """hit 的判定值 —— 三值,且**两个域各自的极性都要读对**。

    - **validator 内核**的 hit:``verdict`` = True 通过 / False 违反 / None 判不了;
    - **guard 档**的 hit(``GuardVerdict`` / ``as_hit()``):``triggered`` =
      True **命中**(= 违反)· False 合规 · None 判不了 —— 与 ``verdict``
      **极性相反**(守卫作者写的是"合规的 SQL 长什么样",表达式不成立才是
      命中;见 guards.py 的极性说明与 ``execute_sql`` 的 blocking 门)。

    两个键都读、只认布尔值,极性按各自的域翻译 —— 这是试跑与 E2 的接缝。
    都不是布尔 → ``None``(判不了),绝不当成「通过」;形状认不出(既不是
    mapping 也没有名字)同样 → ``None``。
    """
    if not _hit_readable(hit):
        return None
    value = _hit_get(hit, "verdict")
    if isinstance(value, bool):
        return value
    triggered = _hit_get(hit, "triggered")
    if isinstance(triggered, bool):
        return not triggered
    return None


def _state_of_hits(hits: list) -> str:
    """一档判定在一条语料上的三态:violated > unjudged > pass。

    severity 不参与这一层 —— 拦不拦是 ``_violation_names`` 的事;这里回答的
    是「判定的结论是什么」,与 ``run_validators`` 的三值纪律逐字对应。
    """
    if any(_hit_verdict(h) is False for h in hits):
        return "violated"
    if any(_hit_verdict(h) is None for h in hits):
        return "unjudged"
    return "pass"


def _violation_names(hits: Sequence[Any], *, blocking: bool) -> tuple[str, ...]:
    """``hits`` 里明确违反(``verdict is False``)的资产名,按 severity 过滤。"""
    out: list[str] = []
    for h in hits:
        if _hit_verdict(h) is not False:
            continue
        severity = str(_hit_get(h, "severity") or "advisory")
        if (severity == "blocking") == blocking:
            out.append(str(_hit_get(h, "name") or "").strip() or "(未命名)")
    return tuple(out)


def _slim_hit(h: Any) -> dict:
    """一条 hit 的证据(只保留判定面字段;不落 SQL/会话数据)。"""
    if not _hit_readable(h):
        return {"name": "", "verdict": None}
    slim: dict[str, Any] = {
        "name": str(_hit_get(h, "name") or ""),
        "verdict": _hit_verdict(h),
        "severity": str(_hit_get(h, "severity") or "advisory"),
        # 判词:内核侧叫 ``message``(validator),守卫侧叫 ``reason``(GuardVerdict
        # 把作者写的 reason 放这里)—— 同一件事的两个名字。
        "message": str(_hit_get(h, "message", "reason") or ""),
    }
    if slim["verdict"] is None:
        # 判不了才带机器码:判定过的 hit 没有"为什么"。内核侧把码放在
        # ``reason``,守卫侧放在 ``none_reason``(GuardVerdict 里 ``reason``
        # 让给了作者写的判词)—— 两列,同一件事。
        code = _hit_get(h, "none_reason")
        if code is None and isinstance(h, Mapping):
            code = h.get("reason")
        if code:
            slim["reason"] = str(code)
    return slim


def _slim_hits(hits: Sequence[Any]) -> tuple[dict, ...]:
    return tuple(_slim_hit(h) for h in hits)


@dataclass(frozen=True)
class Judgment:
    """一条 (语料 × 档) 的判定 —— 双态消融的原始产物。

    ``pre`` / ``post`` 两列就是两态:默认的 E3 形状是「不装 / 装现状」,
    E4 的影响面回放把它参数化成「现状 / 现状+候选」—— **同一套判定机制**,
    只是 ``pre_specs`` 从空集换成了现状装配(见 ``judge_validator``）。
    """

    state: str                      # covered | skipped | errored
    reason: str = ""
    pre: str = ""                   # 不装候选资产时的判定(pass|violated|unjudged)
    post: str = ""                  # 装候选资产时的判定
    pre_blocked_by: tuple[str, ...] = ()
    post_blocked_by: tuple[str, ...] = ()
    post_flagged_by: tuple[str, ...] = ()
    pre_hits: tuple[dict, ...] = ()     # 装前那遍的逐 hit 证据(E4 的差分取证)
    post_hits: tuple[dict, ...] = ()


def _assemble(pre_hits: list, post_hits: list) -> Judgment:
    """两遍判定结果 → Judgment(装/不装的差就摆在 pre/post 两列上)。"""
    return Judgment(
        state="covered",
        pre=_state_of_hits(pre_hits),
        post=_state_of_hits(post_hits),
        pre_blocked_by=_violation_names(pre_hits, blocking=True),
        post_blocked_by=_violation_names(post_hits, blocking=True),
        post_flagged_by=_violation_names(post_hits, blocking=False),
        pre_hits=_slim_hits(pre_hits),
        post_hits=_slim_hits(post_hits),
    )


def judge_validator(
    specs: Sequence[dict], item: CorpusItem, *,
    pre_specs: Sequence[dict] = (), lang: str = "zh",
) -> Judgment:
    """validator 档的单条判定 —— 不装(pre_specs)与装(specs)两遍,零 LLM。

    判定直接吃 fixtures 的 rows/columns(纯内存,不执行 SQL);没有结果行的
    语料(episodes)如实计 ``skipped``,绝不假装判过。

    ``pre_specs`` 缺省是空集(= E3 的「不装」态);E4 给现状装配,同一行代码
    就变成「现状 → 现状+候选」的差分 —— 判定机制一行不改。
    """
    if not item.has_result:
        return Judgment(state="skipped", reason="no_result_rows")
    columns = list(item.columns or [])
    rows = [list(r) for r in (item.rows or [])]
    try:
        pre_hits = run_validators(
            list(pre_specs), columns=columns, rows=rows, row_count=len(rows),
            lang=lang,
        )
        post_hits = run_validators(
            list(specs), columns=columns, rows=rows, row_count=len(rows), lang=lang,
        )
    except Exception as exc:  # noqa: BLE001 — 判定炸了 = 无法试跑,响亮计 errored
        logger.warning("dryrun: validator 判定失败 (%s): %s", item.question_id, exc)
        return Judgment(state="errored", reason="runner_error")
    return _assemble(pre_hits, post_hits)


def judge_guard(
    runner: GuardRunner, specs: Sequence[dict], item: CorpusItem, *,
    pre_specs: Sequence[dict] = (), lang: str = "zh",
) -> Judgment:
    """guard 档的单条判定(SQL 域,执行前)—— 同一套双态消融,换 scope。

    ``pre_specs`` 同 ``judge_validator``:缺省空集(E3),E4 传现状守卫集。
    """
    if not item.sql:
        return Judgment(state="skipped", reason="no_sql")
    try:
        pre_hits = list(runner(
            list(pre_specs), sql=item.sql, dialect=item.dialect, lang=lang))
        post_hits = list(
            runner(list(specs), sql=item.sql, dialect=item.dialect, lang=lang)
        )
    except Exception as exc:  # noqa: BLE001 — 同上:执行器炸了绝不静默
        logger.warning("dryrun: guard 判定失败 (%s): %s", item.question_id, exc)
        return Judgment(state="errored", reason="runner_error")
    return _assemble(pre_hits, post_hits)


# ── 候选资产集(装配面)────────────────────────────────────


@dataclass
class GuardTier:
    """guard 档的可用性 + 接线 —— E2(车道 A)合流前后的统一形状。"""

    available: bool
    reason: str = ""       # SKIP_REASONS 里的码;available=True 时为空
    detail: str = ""       # 人读的补充(导入失败时的异常文本等)
    specs: list[dict] = field(default_factory=list)
    runner: GuardRunner | None = None


def _probe_guards_module() -> tuple[Any | None, str, str]:
    """惰性探测 E2 的 ``trove.services.skills.guards`` → (模块, 码, 细节)。

    存在性用 ``find_spec`` 先行判定 —— 模块**存在但导入失败**(半合流的树)
    必须与「E2 还没合流」分开:前者是坏了(响亮,退出码 2),后者是还没有
    (整档 skipped,不算错)。两者混作一档会让一次坏合流伪装成「还没装」。
    """
    import importlib
    import importlib.util

    name = "trove.services.skills.guards"
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return None, "guards_module_absent", ""
    try:
        return importlib.import_module(name), "", ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("dryrun: guards 模块导入失败: %s", exc)
        return None, "guards_module_import_failed", str(exc)


def load_guard_tier(
    skills: SkillService,
    *,
    runner: GuardRunner | None = None,
    specs: Sequence[dict] | None = None,
) -> GuardTier:
    """guard 档接线 —— 车道 C(语料层)与车道 A(E2)的**唯一接缝**。

    - ``runner``/``specs`` 显式注入(测试、E4)优先;
    - 否则惰性装载 ``trove.services.skills.guards``,取 ``run_guards``
      (契约见 ``GuardRunner``;E2 若以别的名字导出,合流时在这里对齐);
    - 模块不存在 → ``available=False``(整档 skipped,不算错);导入失败 →
      带 ``guards_module_import_failed``(调用方计退出码 2);
    - 资产集合 = 已确认的 ``tier: guard`` org 资产 —— 与 validator 档同一条
      确认门(``list_org(confirmed_only=True)``),草稿永不进试跑。
    """
    if runner is None:
        module, code, detail = _probe_guards_module()
        if module is None:
            return GuardTier(available=False, reason=code, detail=detail)
        fn = getattr(module, "run_guards", None)
        if not callable(fn):
            return GuardTier(available=False, reason="guard_runner_missing")
        runner = fn
    if specs is None:
        specs = [
            e for e in skills.list_org(confirmed_only=True)
            if e.get("tier") == "guard"
        ]
    return GuardTier(available=True, specs=list(specs), runner=runner)


def validator_specs_for(
    skills: SkillService, datasource: str,
) -> tuple[list[dict], list[str]]:
    """候选 validator 资产集 → ``(specs, 未入选的资产名)``。

    走运行的**同一条**读路径 ``validators_for(VALIDATOR_HOST, **ctx)`` ——
    宿主、触发器收窄、总开关都在它里面(试跑看到的集合就是运行时会跑的集合)。
    未入选名单单独回传:trigger 收窄(lang/role/…)把资产挡在候选之外时,
    报告要把这件事说出来,而不是让「候选为空」看起来像「没有资产」。
    """
    ctx = {"datasource": datasource} if datasource else {}
    specs = list(skills.validators_for(VALIDATOR_HOST, **ctx))
    selected = {str(s.get("name") or "") for s in specs}
    all_validator = {
        str(e.get("name") or "")
        for e in skills.list_org(confirmed_only=True)
        if e.get("tier") == "validator"
    }
    return specs, sorted(all_validator - selected)


@dataclass
class Assembly:
    """一次「装了什么资产」的装配清单 —— 双态消融的**注入点**。

    试跑(E3)与影响面回放(E4)是同一台机器:两边都在问「同一语料在两套装配
    下判定的差是什么」。差别只在两态的取值 ——

    - E3:``before = Assembly()``(空集)· ``after = Assembly.current(...)``;
    - E4:``before = Assembly.current(...)`` · ``after = before + 候选``。

    因此装配被提成一个对象:判定机制不认识"候选包"或"现状"这些概念,它只
    需要两个 ``Assembly``。测试要换 fake 装配(零 LLM/零网络的假资产)时,
    直接构造一个塞给 ``run_dryrun``/``run_impact`` 即可,不必伪造文件树。
    """

    validator: list[dict] = field(default_factory=list)
    guard: list[dict] = field(default_factory=list)
    not_selected: list[str] = field(default_factory=list)   # trigger 收窄挡掉的
    guard_tier: GuardTier | None = None                     # 可用性 + runner

    @property
    def guard_available(self) -> bool:
        return self.guard_tier is not None and self.guard_tier.available

    @property
    def guard_runner(self) -> GuardRunner | None:
        return self.guard_tier.runner if self.guard_tier is not None else None

    @classmethod
    def current(
        cls, skills: SkillService, datasource: str,
        *, guard_runner: GuardRunner | None = None,
        guard_specs: Sequence[dict] | None = None,
    ) -> Assembly:
        """现状装配(已确认资产)—— 与运行时的选人同路(``validators_for`` /
        ``guards`` 接缝),不是另一份读法。"""
        specs, not_selected = validator_specs_for(skills, datasource)
        tier = load_guard_tier(skills, runner=guard_runner, specs=guard_specs)
        return cls(
            validator=list(specs),
            guard=list(tier.specs),
            not_selected=not_selected,
            guard_tier=tier,
        )

    def names(self) -> dict[str, list[str]]:
        """报告用的资产名清单(逐档,按出现顺序;渲染面只读这个)。"""
        return {
            "validator": [str(s.get("name") or "") for s in self.validator],
            "guard": [str(s.get("name") or "") for s in self.guard],
        }


# ── 报告 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class JudgmentRow:
    """逐条差分表的一行 = 一条 (语料 × 档) 判定。"""

    index: int
    question_id: str
    question: str            # 默认 ""(R5);include_questions 才有原文
    source: str
    tier: str
    state: str               # covered | skipped | errored
    reason: str = ""
    pre: str = ""
    post: str = ""
    pre_blocked_by: tuple[str, ...] = ()
    post_blocked_by: tuple[str, ...] = ()
    post_flagged_by: tuple[str, ...] = ()
    expected: str = ""
    assertion: str = ""      # "" | "ok" | "mismatch"
    post_hits: tuple[dict, ...] = ()

    @property
    def newly_blocked(self) -> bool:
        """拦截变更:装后拦下、装前没拦 —— 该资产对这条语料的影响。"""
        return bool(
            self.state == "covered" and self.post_blocked_by and not self.pre_blocked_by
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "question_id": self.question_id,
            "source": self.source,
            "tier": self.tier,
            "state": self.state,
            "pre": self.pre,
            "post": self.post,
            "pre_blocked_by": list(self.pre_blocked_by),
            "post_blocked_by": list(self.post_blocked_by),
            "post_flagged_by": list(self.post_flagged_by),
            "expected": self.expected,
            "assertion": self.assertion,
            "hits": [dict(h) for h in self.post_hits],
        }
        if self.reason:
            data["reason"] = self.reason
        # R5:原文只在显式 include_questions 时才出现(默认连键都没有)
        if self.question:
            data["question"] = self.question
        return data


@dataclass
class DryRunReport:
    """一次装前试跑的完整结果(渲染与 JSON 同源)。"""

    datasource: str = ""
    project_root: str = ""
    home: str = ""
    include_questions: bool = False
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)      # 无法试跑 → 退出码 2
    tiers: dict[str, dict] = field(default_factory=dict)
    rows: list[JudgmentRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ── 计数 ──────────────────────────────────────────────

    @property
    def covered(self) -> int:
        return sum(1 for r in self.rows if r.state == "covered")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.rows if r.state == "skipped")

    @property
    def errored(self) -> int:
        return sum(1 for r in self.rows if r.state == "errored")

    @property
    def corpus_n(self) -> int:
        """语料条数(行是按 语料 × 档 展开的,计数要能对回语料)。"""
        return len({r.index for r in self.rows})

    @property
    def newly_blocked(self) -> list[JudgmentRow]:
        return [r for r in self.rows if r.newly_blocked]

    @property
    def mismatches(self) -> list[JudgmentRow]:
        return [r for r in self.rows if r.assertion == "mismatch"]

    # ── 判定 ──────────────────────────────────────────────

    @property
    def exit_code(self) -> int:
        """0 无回归 · 1 有拦截变更/断言失败 · 2 无法试跑(绝不静默 0)。

        ``2`` 的四种情形:语料/接线层有错误、有判定执行出错、**一条判定都没
        真跑过**(covered == 0)、语料为空(没有行)——「全跳过」不是「全绿」,
        前者是没验,后者是验过且干净,这两个必须用不同退出码分开。
        """
        if self.errors:
            return 2
        if not self.rows:
            return 2
        if self.errored:
            return 2
        if self.covered == 0:
            return 2
        if self.newly_blocked or self.mismatches:
            return 1
        return 0

    # ── gate 消费面 ───────────────────────────────────────

    @property
    def metrics(self) -> dict[str, float]:
        """指标段 —— 形状对齐 ``trove/eval/gate.py`` 的 scorecard json。

        - ``coverage``:判定覆盖率 = covered / 判定总数(高优);
        - ``blocking_fail_rate``:拦截率 = 有拦截的判定 / covered(低优;
          名字带 ``fail`` —— gate 的 ``_direction_of`` 按关键词判方向,改名
          前先看那张表);
        - ``assert_fail_rate``:断言失败率 = mismatch / 已断言条目(低优);
        - ``n`` / ``n_judged``:计数键,正是 gate 的 ``_COUNT_ONLY_KEYS``,
          不进逐指标判定、只作分母元信息。
        """
        total = len(self.rows)
        covered = self.covered
        blocked = sum(
            1 for r in self.rows if r.state == "covered" and r.post_blocked_by
        )
        asserted = [r for r in self.rows if r.assertion]
        mismatched = [r for r in asserted if r.assertion == "mismatch"]
        return {
            "coverage": round(covered / total, 4) if total else 0.0,
            "blocking_fail_rate": round(blocked / covered, 4) if covered else 0.0,
            "assert_fail_rate": (
                round(len(mismatched) / len(asserted), 4) if asserted else 0.0
            ),
            "n": float(total),
            "n_judged": float(covered),
        }

    # ── 输出 ──────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "dryrun",
            "ok": self.exit_code == 0,
            "exit_code": self.exit_code,
            "datasource": self.datasource,
            "project_root": self.project_root,
            "include_questions": self.include_questions,
            "sources": list(self.sources),
            "errors": list(self.errors),
            "tiers": {k: dict(v) for k, v in self.tiers.items()},
            "counts": {
                "covered": self.covered,
                "skipped": self.skipped,
                "errored": self.errored,
                "judgments": len(self.rows),
                "corpus": self.corpus_n,
            },
            "items": [r.to_dict() for r in self.rows],
            "metrics": self.metrics,
            "notes": list(self.notes),
        }

    def render(self) -> str:
        lines = [
            "trove validate --run — 装前试跑（零 LLM；validator 结果域 + guard SQL 域双档消融）",
            f"数据源: {self.datasource or '（未点名）'}"
            f" | 项目根: {self.project_root}"
            f" | home: {self.home}",
        ]
        if self.sources:
            lines.append("语料源: " + " · ".join(self.sources))
        tier_bits = []
        for tier in TIERS:
            info = self.tiers.get(tier) or {}
            if info.get("available"):
                names = ", ".join(info.get("specs") or []) or "（无）"
                tier_bits.append(f"{tier} 可用 → [{names}]")
            else:
                tier_bits.append(f"{tier} 不可用（{info.get('reason') or 'n/a'}）")
        if tier_bits:
            lines.append("候选资产: " + " · ".join(tier_bits))
        lines.append(
            f"判定: covered {self.covered} · skipped {self.skipped}"
            f" · errored {self.errored}"
            f"（共 {len(self.rows)} 条判定 / 语料 {self.corpus_n} 条）"
        )

        if self.rows:
            lines.append("")
            lines.append("逐条差分:")
            for r in self.rows:
                head = f"  [{r.question_id or '--------'}] {r.tier:<9} {r.state}"
                if r.state == "covered":
                    detail = f"  装前 {r.pre} → 装后 {r.post}"
                    if r.post_blocked_by:
                        detail += f"  拦截: {', '.join(r.post_blocked_by)}"
                    if r.post_flagged_by:
                        detail += f"  附注: {', '.join(r.post_flagged_by)}"
                    if r.assertion:
                        detail += f"  （断言 {r.expected}: {r.assertion}）"
                else:
                    detail = f"  {r.reason}"
                lines.append(head + detail + (f"  ({r.source})" if r.source else ""))
                if r.question:
                    # R5:只有显式 include_questions 才渲染原文
                    lines.append(f"      问题: {r.question}")

        if self.notes:
            lines.append("")
            lines.append("备注:")
            for n in self.notes:
                lines.append(f"  - {n}")
        if self.errors:
            lines.append("")
            lines.append(f"错误 ({len(self.errors)}):")
            for e in self.errors:
                lines.append(f"  - {e}")

        lines.append("")
        bits = []
        if self.newly_blocked:
            bits.append(f"拦截变更 {len(self.newly_blocked)} 条")
        if self.mismatches:
            bits.append(f"断言失败 {len(self.mismatches)} 条")
        if self.exit_code == 2:
            why = []
            if self.errors:
                why.append(f"错误 {len(self.errors)}")
            if self.errored:
                why.append(f"判定出错 {self.errored}")
            if not self.rows:
                why.append("无判定行(语料为空)")
            elif self.covered == 0 and not self.errored:
                why.append("covered 0(没有任何判定真的跑过)")
            bits.append("无法试跑: " + "、".join(why or ["未知"]))
        summary = "、".join(bits) if bits else "无差异"
        lines.append(f"结论: {summary} → 退出码 {self.exit_code}")
        return "\n".join(lines)


# ── 入口 ─────────────────────────────────────────────────


def resolve_home() -> Path:
    """``~/.trove`` 的解析 —— 与 ``AgentConfig.home`` 同一来源(conf/agent.yml)。

    配置读不到(无文件/损坏)时退回默认 ``~/.trove``:试跑只是读语料,不该
    因为配置文件坏了而崩;这条降级进日志。测试/嵌入方一律显式传 ``home_dir``。
    """
    try:
        from trove.core.config import ConfigLoader

        return Path(ConfigLoader.load_agent_config().home)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dryrun: agent 配置读取失败,home 退回默认: %s", exc)
        return Path("~/.trove").expanduser()


async def run_dryrun(
    *,
    datasource: str,
    project_root: str | Path | None = None,
    home_dir: str | Path | None = None,
    fixtures: str = "auto",
    episodes: bool = False,
    limit: int = DEFAULT_LIMIT,
    include_questions: bool = False,
    skills: SkillService | None = None,
    guard_runner: GuardRunner | None = None,
    guard_specs: Sequence[dict] | None = None,
    episode_store: Any | None = None,
    assembly: Assembly | None = None,
    lang: str = "zh",
) -> DryRunReport:
    """装前试跑:语料层(fixtures + episodes)+ 双态消融 + 三分支退出码。

    ``run_validate`` 的姿态在这里同样成立 —— **never raises for a bad
    configuration**:语料缺失/格式错/资产读不出都落进 ``errors``(退出码 2),
    不让一次坏输入把命令炸成 traceback(试跑正是最需要它的时刻)。

    ``assembly`` 是"装什么"(缺省 = 现状装配):E3 的 after 态就是它;注入
    另一个 ``Assembly`` 不会改变判定机制 —— E4 的影响面回放正是拿两个装配
    跑同一台机器。
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    home = Path(home_dir) if home_dir is not None else resolve_home()
    report = DryRunReport(
        datasource=datasource,
        project_root=str(root),
        home=str(home),
        include_questions=include_questions,
    )

    corpus = await gather_corpus(
        datasource=datasource, project_root=root, home_dir=home,
        fixtures=fixtures, episodes=episodes, limit=limit,
        episode_store=episode_store,
    )
    report.errors.extend(corpus.errors)
    report.sources.extend(corpus.sources)
    if corpus.deduped:
        report.notes.append(
            f"跨源去重 {corpus.deduped} 条(同一问答同时出现在 fixtures 与 episodes)"
        )

    if not datasource:
        report.tiers = _tier_summary()
        return report

    # 只读路径:不建 git 版本化对象(那是写路径的事)
    skills = skills or SkillService(
        root / ".trove" / "skills", git_enabled=False)

    # ── 候选资产集:validator(运行时同路)+ guard(E2 接缝)──
    assembly = assembly or Assembly.current(
        skills, datasource, guard_runner=guard_runner, guard_specs=guard_specs)
    specs, not_selected = assembly.validator, assembly.not_selected
    if not specs:
        report.notes.append(
            "候选 validator 资产为空(无已确认的 validator 档;总开关关闭或"
            "全部被 trigger 收窄时同样如此)"
        )
    if not_selected:
        report.notes.append(
            "未入选的 validator 档资产(trigger 收窄:lang/role/complexity/…): "
            + ", ".join(not_selected)
        )
    guard = assembly.guard_tier or GuardTier(available=False, reason="not_resolved")
    if guard.reason == "guards_module_import_failed":
        # 模块在但导入失败 = 坏合流,不是「还没到」——响亮进错误(退出码 2)
        report.errors.append(f"guards 模块导入失败（{guard.detail}）")

    # ── 逐条判定(语料 × 档,双态)──
    guard_fn = guard.runner if guard.available else None
    for i, item in enumerate(corpus.items):
        question = item.question if include_questions else ""
        for tier in TIERS:
            if tier == "validator":
                judgment = judge_validator(assembly.validator, item, lang=lang)
            elif guard_fn is not None:
                judgment = judge_guard(guard_fn, assembly.guard, item, lang=lang)
            else:
                judgment = Judgment(
                    state="skipped",
                    reason=guard.reason or "guards_module_absent",
                )
            report.rows.append(_row_of(
                i, item, tier, judgment,
                question=question, include_questions=include_questions,
            ))

    report.tiers = _tier_summary(
        validator_available=True,
        validator_specs=[str(s.get("name") or "") for s in specs],
        guard=guard,
    )
    return report


def _row_of(
    index: int,
    item: CorpusItem,
    tier: str,
    judgment: Judgment,
    *,
    question: str,
    include_questions: bool,
) -> JudgmentRow:
    """Judgment → 报告行(含断言核对)。

    断言按档生效:同一语料在 validator 与 guard 两档各自的判定都要与声明的
    ``verdict`` 一致 —— 一档说 violated、另一档说 pass,而语料只声明了一种
    期望,那就是有人没按契约做事。未判定(covered 之外)不产生断言判决:
    判不了不是「违背期望」,它由 skipped/errored 计数如实承载。
    """
    assertion = ""
    if item.expected and judgment.state == "covered":
        assertion = "ok" if judgment.post == item.expected else "mismatch"
    return JudgmentRow(
        index=index,
        question_id=item.question_id,
        question=question if include_questions else "",
        source=item.source,
        tier=tier,
        state=judgment.state,
        reason=judgment.reason,
        pre=judgment.pre,
        post=judgment.post,
        pre_blocked_by=judgment.pre_blocked_by,
        post_blocked_by=judgment.post_blocked_by,
        post_flagged_by=judgment.post_flagged_by,
        expected=item.expected,
        assertion=assertion,
        post_hits=judgment.post_hits,
    )


def _tier_summary(
    *,
    validator_available: bool = False,
    validator_specs: list[str] | None = None,
    guard: GuardTier | None = None,
) -> dict[str, dict]:
    """两档的可用性摘要(报告 tiers 段)。"""
    summary: dict[str, dict] = {
        "validator": {
            "available": validator_available,
            "specs": list(validator_specs or []),
            "reason": "" if validator_available else "not_resolved",
        },
    }
    if guard is None:
        summary["guard"] = {"available": False, "specs": [], "reason": "not_resolved"}
    else:
        summary["guard"] = {
            "available": guard.available,
            "specs": [str(s.get("name") or "") for s in guard.specs],
            "reason": guard.reason,
        }
    return summary
