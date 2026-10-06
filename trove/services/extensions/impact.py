"""影响面回放(``trove validate --impact``)—— 候选资产「装上会发生什么」。

实施稿 §03 h / §04 E4 的落点。它与装前试跑(E3,``dryrun.py``)是**同一台
机器**:同一份语料、同一套判定机制、同一条零 LLM 纪律。差别只在两态取值 ——

- E3(试跑):``before = 空集`` · ``after = 现状装配`` ——「现在装着的资产会不会
  拦下这条语料」;
- E4(回放):``before = 现状装配`` · ``after = 现状 + 候选`` ——「**换上新包之后**
  判定会怎么变」。候选集 = 一个待导入的包目录,或一个直接的资产目录。

四条设计契约:

- **差分四桶,一桶都不许含糊**:新增拦截(装后新出现的拦截)· 新放行(装前拦、
  装后不拦)· 无变化 · **无法判定**。第四桶是诚实边界:双态任一侧 ``skipped``
  / ``errored``,或结论是 ``unjudged``(判不了)—— 一律进"无法判定",绝不
  默默算成"无变化"。判不了与没变化是两种结论,把它们合成一个数字等于用
  「没有差异」冒充「验过且干净」;
- **逐题可指回原始行**:每条判定行带证据 —— 触发资产名、**表达式**、**变量
  实际取值**(只列表达式真正读到的变量,值与判定内核用的是同一份作用域:
  validator 走 ``build_scope``,guard 走 ``extract_sql_features``)。报告里
  的每个数字都能顺着证据回到那一行;
- **确定性证明**:输出里没有时间戳、没有集合迭代序、没有不确定内容 —— 所有
  排序显式给定。``PYTHONHASHSEED=0/1/2`` 三连跑必须逐字节一致(测试断言):
  回放结果可复现是企业采纳的前提,而"这次跑出来不一样"的回放没人敢挂 CI;
- **可挂门**:``--json`` 顶层带 ``metrics`` 段,形状直接喂 ``scripts/eval_gate.py``
  (``trove/eval/gate.py`` 的 scorecard 消费面)。指标**名里带方向关键词** ——
  门的 ``_direction_of`` 靠关键词判方向(``fail`` → 更低更好),改名之前先读
  那张表:一个方向判反的指标会让门替回归放行。

隐私(R5,全批红线):报告默认问题文本**哈希短码化**(与 E3 同一个 ``question_id``
——两份报告的问题可以互相对上),``include_questions`` 才出原文;逐题证据里
只有配置侧的表达式与数值,不落 SQL 原文、不落会话/用户标识。

退出码三分支(与 E3 同款,``0`` 无拦截变更 · ``1`` 有拦截变更 · ``2`` 无法回放):
候选集为空/读不出、语料缺失、判定执行出错、一条判定都没真跑过 —— 全是 2。
**"回放不出东西"绝不静默返回 0**:一个永远绿的门不是门。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.extensions import dryrun as _dryrun
from trove.services.extensions.dryrun import (
    DEFAULT_LIMIT,
    TIERS,
    Assembly,
    CorpusItem,
    Judgment,
    gather_corpus,
    judge_guard,
    judge_validator,
)
from trove.services.skills.service import SkillService

logger = get_logger(__name__)

#: 差分四桶(闭集,报告与 metrics 同源)。
BUCKETS = ("newly_blocked", "newly_released", "unchanged", "undecided")

#: 桶的中文渲染(CLI 一屏文本)。
BUCKET_LABELS = {
    "newly_blocked": "新增拦截",
    "newly_released": "新放行",
    "unchanged": "无变化",
    "undecided": "无法判定",
}

#: 可回放的档位 —— 只有这两档有判定内核(rest 是提示词侧的 methodology,
#: 回放不了,报告如实列出而非静默丢弃)。
REPLAY_TIERS = ("validator", "guard")


# ── 候选资产集(输入面)────────────────────────────────────


@dataclass(frozen=True)
class CandidateAsset:
    """一个候选资产 —— 从包/资产目录读出的**待生效**条目。

    ``entry`` 是 ``SkillService.read_skill`` 的同一投影(判定内核消费的形状):
    包内资产与已确认资产走**同一条读路径**,不存在"回放看到的形状"与"装上
    后的形状"两套。``status`` 如实带上但**不参与筛选** —— 回放回答的是
    「确认之后会发生什么」,这正是导入前要看的那一眼。
    """

    name: str
    tier: str
    entry: dict
    source: str = ""
    status: str = ""

    @property
    def replayable(self) -> bool:
        return self.tier in REPLAY_TIERS

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "status": self.status,
            "source": self.source,
            "replayable": self.replayable,
        }


def _skill_root_of(target: Path) -> tuple[Path | None, list[str], list[str]]:
    """目标目录 → ``(skills 根, 错误, 备注)``:包(有 manifest.yml)先过校验链。

    包走 ``read_pack``(**版本门 + 逐文件 sha256**)—— 影响面回放的第一条
    纪律是"先确认这是它说的那个包":一份被改过的包回放出的影响面,回放的是
    别人改过的东西。校验失败一律响亮(调用方退出码 2),不做"能读多少读多少"。
    """
    errors: list[str] = []
    notes: list[str] = []
    manifest = target / "manifest.yml"
    if not manifest.is_file():
        # 直接的资产目录:根下就是 ``<name>/SKILL.md``
        return target, errors, notes

    from trove.services.extensions.pack import PackError, read_pack

    try:
        pack = read_pack(target)
    except PackError as exc:
        return None, [f"候选包校验失败: {exc}"], notes
    kinds = "、".join(pack.manifest.kinds) or "（无）"
    notes.append(
        f"候选包 {pack.manifest.name!r}（pack_schema={pack.manifest.pack_schema}"
        f"·origin={pack.manifest.origin}·kinds={kinds}）已过逐文件 sha256 校验"
    )
    if pack.extra:
        notes.append(
            f"包内未列入清单的文件 {len(pack.extra)} 个（不是包的一部分,未参与回放）: "
            + ", ".join(pack.extra)
        )
    non_replay = [k for k in pack.manifest.kinds if k != "skills"]
    if non_replay:
        notes.append(
            "包的 kinds 含 " + "、".join(non_replay) + " —— 本次回放只覆盖 "
            "skills（validator/guard 判定内核）;decisions/presets 的影响面不在本机制内"
        )
    if not (target / "files" / "skills").is_dir():
        return None, [], notes
    return target / "files" / "skills", errors, notes


def load_candidates(target: str | Path) -> tuple[list[CandidateAsset], list[str], list[str]]:
    """候选集路径 → ``(候选资产, 错误, 备注)``。

    两种输入形态(§04 E4):一个**待导入的包**(含 ``manifest.yml``)或一个
    **资产目录**。读条目走 ``SkillService.list_org`` —— 与运行时同一条读
    路径,绝不复写 frontmatter 解析。

    读不出的目录/坏包 → 错误(退出码 2);档位不可回放的资产(如 ``available``
    档的方法论)如实列进备注而不是静默丢掉。
    """
    errors: list[str] = []
    notes: list[str] = []
    root = Path(target).expanduser()
    if not str(target).strip():
        return [], ["--impact 需要候选集路径（包目录或资产目录）"], notes
    if not root.exists():
        return [], [f"候选集路径不存在: {root}"], notes
    if not root.is_dir():
        return [], [f"候选集必须是目录（包目录或资产目录）: {root}"], notes

    skills_root, errors, notes = _skill_root_of(root)
    if skills_root is None:
        return [], errors, notes

    if not skills_root.is_dir():
        notes.append(f"候选集不含 skills 目录（{skills_root}）: 没有可回放的资产")
        return [], errors, notes

    dirs = sorted(d for d in skills_root.iterdir() if d.is_dir())
    orphan = [d.name for d in dirs if not (d / "SKILL.md").exists()]
    if orphan:
        # 有目录没 SKILL.md(只有 SKILL.<lang>.md / 空目录):读取路径只认
        # SKILL.md,这份文件在运行时也不会被消费 —— 如实说出来,别静默少一条。
        notes.append("候选目录没有 SKILL.md（读取路径只认它,运行时同样不消费）: "
                     + ", ".join(orphan))
    if not dirs:
        notes.append(f"候选目录下没有任何资产目录（{skills_root}）")

    svc = SkillService(root=skills_root, git_enabled=False)
    out: list[CandidateAsset] = []
    for d in dirs:
        if not (d / "SKILL.md").exists():
            continue
        entry = svc.read_skill(d.name)
        if entry is None:
            continue
        if entry.get("error"):
            # frontmatter 读不出来 = 连它属于哪一档都不知道 —— 无法回放(2)。
            errors.append(f"候选资产 {d.name} 的 SKILL.md 无法解析: {entry['error']}")
            continue
        name = str(entry.get("name") or d.name)
        out.append(CandidateAsset(
            name=name,
            tier=str(entry.get("tier") or ""),
            entry=entry,
            source=f"{skills_root}/{d.name}/SKILL.md",
            status=str(entry.get("status") or ""),
        ))

    # 输出顺序完全确定:按名字排序(不是目录、不是集合迭代序)。
    out.sort(key=lambda c: (c.name, c.tier, c.source))
    stray = [c for c in out if not c.replayable]
    if stray:
        notes.append(
            "不可回放的候选资产（档位不在 " + "/".join(REPLAY_TIERS) + "，本次跳过）: "
            + ", ".join(f"{c.name}({c.tier or '未声明'})" for c in stray)
        )
    return out, errors, notes


# ── 装配(现状 → 现状+候选)────────────────────────────────


def _merge_by_name(base: Sequence[dict], extra: Sequence[dict]) -> list[dict]:
    """同名**替换**(候选胜出,位置不动),其余追加 —— 输出顺序完全确定。

    替换而不是"两份都留着":装上候选之后同名资产只有一份(包内版本),把
    两份都喂给判定内核会让同一条资产判两遍,差分计数被自己污染。
    """
    out = list(base)
    index = {str(e.get("name") or ""): i for i, e in enumerate(out)}
    for e in extra:
        name = str(e.get("name") or "")
        if name and name in index:
            out[index[name]] = e
        else:
            index[name] = len(out)
            out.append(e)
    return out


def merge_assembly(base: Assembly, candidates: Sequence[CandidateAsset]) -> Assembly:
    """现状装配 + 候选集 → **after** 装配(双态的那一态)。

    只有 validator / guard 两档进集合(其余档位没有判定内核);guard 的可用性
    与 runner 原样继承现状 —— 候选集改变的是**装了什么**,不是**引擎在不在**。
    """
    return Assembly(
        validator=_merge_by_name(
            base.validator, [c.entry for c in candidates if c.tier == "validator"]),
        guard=_merge_by_name(
            base.guard, [c.entry for c in candidates if c.tier == "guard"]),
        not_selected=list(base.not_selected),
        guard_tier=base.guard_tier,
    )


#: 回放时**可知**的触发维度。运行时的 ``skill_ctx()`` 有 query/lang/role/
#: complexity/intent……回放只知道数据源 —— 拿一个不知道的维度去筛,筛掉的
#: 是"信息缺失"而不是"不匹配"(候选被静默丢掉),所以只筛可知的那个。
_KNOWN_TRIGGER_DIMS = ("datasource",)


def filter_candidates(
    candidates: Sequence[CandidateAsset], datasource: str,
) -> tuple[list[CandidateAsset], list[str]]:
    """候选集 → ``(本数据源上会生效的, 备注)``。

    候选声明的 ``triggers.datasource`` 指向别的数据源 → 它在**这台**数据源上
    永远不会跑,放进 after 装配就会伪造出一批"新增拦截"。筛选走
    ``SkillService._trigger_mismatch`` —— 触发语义的**唯一**实现,不另写一份
    (另写的必然漂移,而漂移的表现是"配了却不生效"这类最难查的事故)。

    其余维度(lang/role/complexity/intent)回放时不可知:按「信息缺失不收窄」
    处置并**如实备注** —— 静默丢掉一条候选,与"这条候选没有影响"从报告上
    看一模一样。
    """
    if not datasource:
        return list(candidates), []
    kept: list[CandidateAsset] = []
    notes: list[str] = []
    unknown_dims: list[str] = []
    for c in candidates:
        triggers = c.entry.get("triggers") or {}
        if not isinstance(triggers, Mapping):
            triggers = {}
        known = {k: v for k, v in triggers.items() if k in _KNOWN_TRIGGER_DIMS}
        others = sorted(k for k in triggers if k not in _KNOWN_TRIGGER_DIMS)
        if others and c.name not in unknown_dims:
            unknown_dims.append(c.name)
            notes.append(
                f"候选 {c.name} 声明了回放时不可知的触发维度"
                f"（{'/'.join(others)}）—— 按「信息缺失不收窄」处置:它在正式"
                "运行时是否命中该维度,以运行时 ctx 为准"
            )
        if known and SkillService._trigger_mismatch(
            known, "", {"datasource": datasource}, skip_node=True,
        ) is not None:
            notes.append(
                f"候选 {c.name} 不适用于数据源 {datasource}"
                f"（triggers.datasource={known.get('datasource')!r}）—— 本数据源上"
                "不会生效,未进 after 装配"
            )
            continue
        kept.append(c)
    return kept, notes


# ── 差分分类(四桶)────────────────────────────────────────


def classify(judgment: Judgment) -> tuple[str, str]:
    """一条 (语料 × 档) 的双态判定 → ``(桶, 理由)``。

    优先级与理由码:

    - 任一侧 ``skipped`` / ``errored`` → ``undecided``(理由 = 那一侧的成因码);
    - 任一侧结论 ``unjudged`` → ``undecided``(判不了不是"没变化";三值内核
      的 None 在这里有了它的第三个去处 —— 不是通过、不是违反,而是**没判**);
    - 装后出现装前没有的拦截 → ``newly_blocked``;
    - 装前拦、装后不拦 → ``newly_released``(卸掉/放宽守卫同样是"拦截变更",
      而且方向更危险 —— 门必须能红);
    - 其余(两侧都判过且拦截集合相同)→ ``unchanged``。
    """
    if judgment.state != "covered":
        return "undecided", judgment.reason or judgment.state
    if judgment.pre == "unjudged" or judgment.post == "unjudged":
        return "undecided", "unjudged"
    if newly_blocked_by(judgment):
        return "newly_blocked", ""
    if newly_released_by(judgment):
        return "newly_released", ""
    return "unchanged", ""


def newly_blocked_by(judgment: Judgment) -> tuple[str, ...]:
    """装后新出现的拦截资产名(保序、去重 —— 直接来自判定内核的两列)。"""
    pre = set(judgment.pre_blocked_by)
    return _uniq(n for n in judgment.post_blocked_by if n not in pre)


def newly_released_by(judgment: Judgment) -> tuple[str, ...]:
    """装前拦、装后不拦的资产名。"""
    post = set(judgment.post_blocked_by)
    return _uniq(n for n in judgment.pre_blocked_by if n not in post)


def _uniq(names: Any) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return tuple(out)


# ── 逐题证据(触发资产名 / 表达式 / 变量实际取值)──────────


def _json_value(value: Any) -> Any:
    """作用域里的值 → JSON 安全 —— ``UNKNOWN``(判不了)显式成 ``"unknown"``。

    把 UNKNOWN 写成 null 或 0 都会让报告读者以为"算出来是空/是零",而它的
    含义恰恰是**没算出来**(缺列/解析失败/被截断)。
    """
    from trove.services.decision.expr import UNKNOWN

    if value is UNKNOWN:
        return "unknown"
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return str(value)


def _checks_of(entries: Sequence[dict], tier: str) -> list[tuple[str, str, str, dict]]:
    """装配清单 → ``[(匹配名, 资产名, check 名, check 原始 dict)]``。

    匹配名 = 判定内核给这条 check 的 hit 名字:validator 是**资产名**(内核
    按 spec 逐条判,一条资产一个 hit);guard 是**check 自己的名字**(E2 把
    每条 authored check 展开成一个独立守卫单元,名字缺省回落到资产名)。
    两边都按内核的命名来索引,证据才挂得准。
    """
    out: list[tuple[str, str, str, dict]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        asset = str(entry.get("name") or "")
        if tier == "validator":
            for check in entry.get("checks") or []:
                if isinstance(check, Mapping):
                    out.append((asset, asset, str(check.get("name") or asset), check))
        else:
            guard = entry.get("guard")
            checks = guard.get("checks") if isinstance(guard, Mapping) else None
            for check in checks or []:
                if isinstance(check, Mapping):
                    out.append((
                        str(check.get("name") or "") or asset,
                        asset, str(check.get("name") or asset), check,
                    ))
    return out


def _scope_of(
    tier: str, check: Mapping, item: CorpusItem, cache: dict[Any, dict] | None = None,
) -> dict[str, Any]:
    """该 check 的**实际取值域** —— 与判定内核同源,不是另一套近似。

    validator 走 ``build_scope``(结果域,fixtures 的 rows/columns 直喂);
    guard 走 ``extract_sql_features``(SQL 域,一次 parse 九个特征)。

    ``cache`` 只为省重复计算(同一行的多条 hit 只需一次 parse),不改语义 ——
    缓存键含 item 身份,证据永远是**这条语料**的取值。
    """
    key: Any = ("guard",)
    if tier == "validator":
        key = ("validator", tuple(str(c) for c in (check.get("columns") or [])))
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit

    if tier == "validator":
        from trove.services.skills.validators import build_scope

        rows = [list(r) for r in (item.rows or [])]
        scope = dict(build_scope(
            dict(check), list(item.columns or []), rows, row_count=len(rows)))
    else:
        from trove.services.skills.guards import extract_sql_features

        scope = dict(extract_sql_features(item.sql, item.dialect))
    if cache is not None:
        cache[key] = scope
    return scope


def _evidence_for(
    hits: Sequence[Any], entries: Sequence[dict], tier: str,
    item: CorpusItem, *, side: str, lang: str = "zh",
) -> list[dict]:
    """一个态的 hits → 逐条证据(只取"触发"与"判不了",通过的不算证据)。

    表达式来自**装配清单**(作者写的东西);取值来自**判定内核同一份作用域**;
    只列表达式真正读到的变量(``condition_variables``)—— 一份把九个变量全
    倒出来的"证据"读起来和没有证据一样。
    """
    del lang  # 证据里只有配置与数值,不带判词(判词在 hits 里,按档语言渲染)
    out: list[dict] = []
    index = _checks_of(entries, tier)
    cache: dict[Any, dict] = {}
    for hit in hits:
        if not isinstance(hit, Mapping):
            # ``GuardVerdict`` 形状的 hit:由调用方(E2 接缝)归一后再进来;
            # 这里认不出就跳过,不编造证据。
            continue
        verdict = hit.get("verdict")
        if verdict is True:
            continue
        name = str(hit.get("name") or "")
        for match, asset, check_name, check in index:
            if match != name:
                continue
            expr = str(check.get("expr") or "")
            out.append({
                "side": side,
                "tier": tier,
                "name": name,
                "asset": asset,
                "check": check_name,
                "expr": expr,
                "verdict": verdict,
                "values": _referenced_values(tier, expr, check, item, cache),
            })
    out.sort(key=lambda e: (e["side"], e["tier"], e["name"], e["check"], e["expr"]))
    return out


def _referenced_values(
    tier: str, expr: str, check: Mapping, item: CorpusItem,
    cache: dict[Any, dict] | None = None,
) -> dict[str, Any]:
    """表达式读到的变量 → 实际取值(读不出来 → 空 dict + 表达式错误标记)。"""
    from trove.services.decision.expr import (
        VALIDATOR_VARIABLES,
        DecisionExprError,
        condition_variables,
    )
    from trove.services.skills.guards import GUARD_VARIABLES

    domain = VALIDATOR_VARIABLES if tier == "validator" else GUARD_VARIABLES
    try:
        names = condition_variables(expr, domain)
    except DecisionExprError:
        # 写坏的表达式:判定内核那边已经落一条 bad_expression 的"判不了",
        # 证据侧如实说"这个表达式读不出来",不猜它想读什么。
        return {}
    scope = _scope_of(tier, check, item, cache)
    return {n: _json_value(scope.get(n)) for n in sorted(names)}


# ── 逐题差分行 ───────────────────────────────────────────


@dataclass(frozen=True)
class ImpactRow:
    """逐题差分表的一行 = 一条 (语料 × 档) 的双态判定。"""

    index: int
    question_id: str
    question: str = ""            # 默认 ""(R5);include_questions 才有原文
    source: str = ""
    tier: str = ""
    state: str = ""               # covered | skipped | errored
    reason: str = ""
    pre: str = ""
    post: str = ""
    pre_blocked_by: tuple[str, ...] = ()
    post_blocked_by: tuple[str, ...] = ()
    post_flagged_by: tuple[str, ...] = ()
    bucket: str = "unchanged"
    evidence: tuple[dict, ...] = ()

    @property
    def newly_blocked_by(self) -> tuple[str, ...]:
        if self.state != "covered" or self.pre == "unjudged" or self.post == "unjudged":
            return ()
        pre = set(self.pre_blocked_by)
        return _uniq(n for n in self.post_blocked_by if n not in pre)

    @property
    def newly_released_by(self) -> tuple[str, ...]:
        if self.state != "covered" or self.pre == "unjudged" or self.post == "unjudged":
            return ()
        post = set(self.post_blocked_by)
        return _uniq(n for n in self.pre_blocked_by if n not in post)

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
            "newly_blocked_by": list(self.newly_blocked_by),
            "newly_released_by": list(self.newly_released_by),
            "bucket": self.bucket,
            "evidence": [dict(e) for e in self.evidence],
        }
        if self.reason:
            data["reason"] = self.reason
        # R5:原文只在显式 include_questions 时才出现(默认连键都没有)
        if self.question:
            data["question"] = self.question
        return data


def _row_of(
    index: int, item: CorpusItem, tier: str, judgment: Judgment,
    *, before: Assembly, after: Assembly, include_questions: bool,
) -> ImpactRow:
    bucket, why = classify(judgment)
    evidence: list[dict] = []
    if judgment.state == "covered":
        evidence += _evidence_for(
            judgment.pre_hits, before.validator if tier == "validator" else before.guard,
            tier, item, side="pre")
        evidence += _evidence_for(
            judgment.post_hits, after.validator if tier == "validator" else after.guard,
            tier, item, side="post")
    return ImpactRow(
        index=index,
        question_id=item.question_id,
        question=item.question if include_questions else "",
        source=item.source,
        tier=tier,
        state=judgment.state,
        reason=judgment.reason or why,
        pre=judgment.pre,
        post=judgment.post,
        pre_blocked_by=judgment.pre_blocked_by,
        post_blocked_by=judgment.post_blocked_by,
        post_flagged_by=judgment.post_flagged_by,
        bucket=bucket,
        evidence=tuple(evidence),
    )


def diff_assembly(
    items: Sequence[CorpusItem], *, before: Assembly, after: Assembly,
    include_questions: bool = False, lang: str = "zh",
) -> list[ImpactRow]:
    """语料 × 两套装配 → 逐题差分行(**纯函数**,不碰文件系统/网络)。

    这是整个回放的判定核:装什么由两个 ``Assembly`` 说了算,零 LLM、零网络、
    零文件 —— 测试构造差分直接调它,不必伪造目录树。
    """
    rows: list[ImpactRow] = []
    guard_runner = after.guard_runner if after.guard_available else None
    guard_reason = (
        after.guard_tier.reason if after.guard_tier is not None else "guards_module_absent"
    )
    for i, item in enumerate(items):
        for tier in TIERS:
            if tier == "validator":
                judgment = judge_validator(
                    after.validator, item, pre_specs=before.validator, lang=lang)
            elif guard_runner is not None:
                judgment = judge_guard(
                    guard_runner, after.guard, item, pre_specs=before.guard, lang=lang)
            else:
                judgment = Judgment(
                    state="skipped", reason=guard_reason or "guards_module_absent")
            rows.append(_row_of(
                i, item, tier, judgment,
                before=before, after=after, include_questions=include_questions,
            ))
    return rows


# ── 报告 ─────────────────────────────────────────────────


@dataclass
class ImpactReport:
    """一次影响面回放的完整结果(渲染与 JSON 同源)。"""

    target: str = ""
    datasource: str = ""
    project_root: str = ""
    home: str = ""
    include_questions: bool = False
    sources: list[str] = field(default_factory=list)        # 语料源摘要
    errors: list[str] = field(default_factory=list)         # 无法回放 → 退出码 2
    candidates: list[CandidateAsset] = field(default_factory=list)
    before: dict[str, list[str]] = field(default_factory=dict)
    after: dict[str, list[str]] = field(default_factory=dict)
    rows: list[ImpactRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ── 计数 ──────────────────────────────────────────────

    @property
    def corpus_n(self) -> int:
        return len({r.index for r in self.rows})

    @property
    def covered(self) -> int:
        return sum(1 for r in self.rows if r.state == "covered")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.rows if r.state == "skipped")

    @property
    def errored(self) -> int:
        return sum(1 for r in self.rows if r.state == "errored")

    def _bucket(self, name: str) -> list[ImpactRow]:
        return [r for r in self.rows if r.bucket == name]

    @property
    def newly_blocked(self) -> list[ImpactRow]:
        return self._bucket("newly_blocked")

    @property
    def newly_released(self) -> list[ImpactRow]:
        return self._bucket("newly_released")

    @property
    def unchanged(self) -> list[ImpactRow]:
        return self._bucket("unchanged")

    @property
    def undecided(self) -> list[ImpactRow]:
        return self._bucket("undecided")

    @property
    def decided(self) -> int:
        """真判过的判定行数(双态都给出了确定结论)。"""
        return len(self.rows) - len(self.undecided)

    # ── 判定 ──────────────────────────────────────────────

    @property
    def exit_code(self) -> int:
        """0 无拦截变更 · 1 有拦截变更 · 2 无法回放(绝不静默 0)。

        ``2`` 的五种情形:输入/语料层有错误、有判定执行出错、没有判定行、
        **没有可回放的候选资产**(「没有差异」不许等于「没有验」)、一条判定
        也没真判过(全部 skipped 或 unjudged)。``1`` 覆盖两个方向的拦截变更
        —— 新增拦截与新放行都改变了拦截行为,而新放行是更危险的那一侧。
        """
        if self.errors:
            return 2
        if not self.candidates:
            return 2
        if not self.rows:
            return 2
        if self.errored:
            return 2
        if self.decided == 0:
            return 2
        if self.newly_blocked or self.newly_released:
            return 1
        return 0

    # ── gate 消费面 ───────────────────────────────────────

    @property
    def metrics(self) -> dict[str, float]:
        """指标段 —— 形状对齐 ``trove/eval/gate.py`` 的 scorecard json。

        **名字即方向**:``gate._direction_of`` 先查内置表、再按关键词判 ——
        带 ``fail`` 的一律"越低越好"。所以每个"变坏"的比率都在名字里带
        ``fail``(改名之前先读那张表,方向判反的门会替回归放行)。计数键
        (``n`` / ``n_judged``)是 gate 的 ``_COUNT_ONLY_KEYS``,不进逐指标判定。
        """
        total = len(self.rows)
        decided = self.decided
        blocked = sum(1 for r in self.rows if r.state == "covered" and r.post_blocked_by)
        nb = len(self.newly_blocked)
        nr = len(self.newly_released)
        return {
            "coverage": round(decided / total, 4) if total else 0.0,
            "blocking_fail_rate": round(blocked / decided, 4) if decided else 0.0,
            "newly_blocked_fail_rate": round(nb / decided, 4) if decided else 0.0,
            "newly_released_fail_rate": round(nr / decided, 4) if decided else 0.0,
            "unjudged_fail_rate": (
                round(len(self.undecided) / total, 4) if total else 0.0
            ),
            "n": float(total),
            "n_judged": float(decided),
        }

    def counts(self) -> dict[str, int]:
        return {
            "newly_blocked": len(self.newly_blocked),
            "newly_released": len(self.newly_released),
            "unchanged": len(self.unchanged),
            "undecided": len(self.undecided),
            "judgments": len(self.rows),
            "corpus": self.corpus_n,
            "decided": self.decided,
            "covered": self.covered,
            "skipped": self.skipped,
            "errored": self.errored,
            "candidates": len(self.candidates),
        }

    # ── 输出 ──────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "impact",
            "ok": self.exit_code == 0,
            "exit_code": self.exit_code,
            "target": self.target,
            "datasource": self.datasource,
            "project_root": self.project_root,
            "include_questions": self.include_questions,
            "sources": list(self.sources),
            "errors": list(self.errors),
            "candidates": [c.to_dict() for c in self.candidates],
            "assembly": {"before": _names_dict(self.before),
                         "after": _names_dict(self.after)},
            "counts": self.counts(),
            "items": [r.to_dict() for r in self.rows],
            "metrics": self.metrics,
            "notes": list(self.notes),
        }

    def render(self) -> str:
        lines = [
            "trove validate --impact — 影响面回放（零 LLM；候选装配前后的判定差分）",
            f"候选集: {self.target} | 数据源: {self.datasource or '（未点名）'}",
            f"项目根: {self.project_root} | home: {self.home}",
        ]
        for c in self.candidates:
            mark = "" if c.replayable else "（不可回放）"
            lines.append(
                f"  候选 {c.name} [{c.tier or '未声明'}]"
                f"{mark} status={c.status or '—'}")
        before = self.before or {}
        after = self.after or {}
        lines.append(
            "装配: 现状 validator [{}] · guard [{}] → 候选 validator [{}] · guard [{}]".format(
                ", ".join(before.get("validator") or []) or "（无）",
                ", ".join(before.get("guard") or []) or "（无）",
                ", ".join(after.get("validator") or []) or "（无）",
                ", ".join(after.get("guard") or []) or "（无）",
            )
        )
        if self.sources:
            lines.append("语料源: " + " · ".join(self.sources))
        c = self.counts()
        lines.append(
            "汇总: " + " · ".join(
                f"{BUCKET_LABELS[b]} {c[b]}" for b in BUCKETS)
            + f"（共 {c['judgments']} 条判定 / 语料 {c['corpus']} 条"
              f" / 已判 {c['decided']} / 候选 {c['candidates']}）"
        )

        if self.rows:
            lines.append("")
            lines.append("逐条差分:")
            for r in self.rows:
                head = (f"  [{r.question_id or '--------'}] {r.tier:<9} "
                        f"{BUCKET_LABELS.get(r.bucket, r.bucket)}")
                if r.state == "covered":
                    detail = f"  装前 {r.pre} → 装后 {r.post}"
                    if r.newly_blocked_by:
                        detail += f"  新增拦截: {', '.join(r.newly_blocked_by)}"
                    if r.newly_released_by:
                        detail += f"  新放行: {', '.join(r.newly_released_by)}"
                    if r.post_flagged_by:
                        detail += f"  附注: {', '.join(r.post_flagged_by)}"
                else:
                    detail = f"  {r.reason}"
                lines.append(head + detail + (f"  ({r.source})" if r.source else ""))
                if r.question:
                    lines.append(f"      问题: {r.question}")
                for e in r.evidence:
                    values = ", ".join(f"{k}={v}" for k, v in e["values"].items())
                    lines.append(
                        f"      证据[{e['side']}] {e['asset']}/{e['check']}: "
                        f"{e['expr']}" + (f"  ← {values}" if values else ""))

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
            bits.append(f"新增拦截 {len(self.newly_blocked)} 条")
        if self.newly_released:
            bits.append(f"新放行 {len(self.newly_released)} 条")
        if self.exit_code == 2:
            why = []
            if self.errors:
                why.append(f"错误 {len(self.errors)}")
            if not self.candidates:
                why.append("候选集为空(没有可回放的东西)")
            if not self.rows:
                why.append("无判定行(语料为空)")
            if self.errored:
                why.append(f"判定出错 {self.errored}")
            elif self.rows and self.decided == 0:
                why.append("一条判定都没真判过(全部跳过/判不了)")
            bits.append("无法回放: " + "、".join(why or ["未知"]))
        summary = "、".join(bits) if bits else "无拦截变更"
        lines.append(f"结论: {summary} → 退出码 {self.exit_code}")
        return "\n".join(lines)


def _names_dict(names: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """装配摘要 → 纯 list(报告可序列化;顺序即装配顺序)。"""
    return {k: [str(n) for n in v] for k, v in names.items()}


# ── 入口 ─────────────────────────────────────────────────


async def run_impact(
    *,
    target: str | Path,
    datasource: str,
    project_root: str | Path | None = None,
    home_dir: str | Path | None = None,
    fixtures: str = "auto",
    episodes: bool = False,
    limit: int = DEFAULT_LIMIT,
    include_questions: bool = False,
    skills: SkillService | None = None,
    assembly: Assembly | None = None,
    guard_runner: Any | None = None,
    guard_specs: Sequence[dict] | None = None,
    episode_store: Any | None = None,
    lang: str = "zh",
) -> ImpactReport:
    """影响面回放:候选集 × 语料 × 两套装配 → 差分报告(never raises)。

    与 ``run_dryrun`` 同一条姿态:坏输入不抛异常,落进报告的 ``errors``
    (调用方据此出退出码 2)——回放最需要它的时刻,恰是输入坏了的时候。

    ``assembly`` 是**现状装配**的注入点(缺省 = ``Assembly.current``,即
    运行时同路的已确认资产);测试要跑 fake 装配时直接传一个,不必伪造
    ``.trove/skills`` 目录树。
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    # ``_dryrun.resolve_home`` 而不是 from-import:注入点必须能被替换
    # (测试钉 home,试跑/回放两条路要钉的是同一个符号)。
    home = Path(home_dir) if home_dir is not None else _dryrun.resolve_home()
    report = ImpactReport(
        target=str(target), datasource=datasource,
        project_root=str(root), home=str(home),
        include_questions=include_questions,
    )

    candidates, errors, notes = load_candidates(target)
    report.errors.extend(errors)
    report.notes.extend(notes)
    report.candidates = candidates

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
        return report

    # 候选先过触发筛(只知道数据源,见 filter_candidates);筛掉的都在备注里点名。
    candidates, scope_notes = filter_candidates(candidates, datasource)
    report.notes.extend(scope_notes)
    report.candidates = candidates

    skills = skills or SkillService(root / ".trove" / "skills", git_enabled=False)
    before = assembly or Assembly.current(
        skills, datasource, guard_runner=guard_runner, guard_specs=guard_specs)
    after = merge_assembly(before, candidates)
    report.before = before.names()
    report.after = after.names()

    if before.guard_tier is not None and before.guard_tier.reason == "guards_module_import_failed":
        report.errors.append(
            f"guards 模块导入失败（{before.guard_tier.detail}）")
    if not before.validator and not any(c.tier == "validator" for c in candidates):
        report.notes.append(
            "装配里没有 validator 档资产（现状无已确认的,候选也没有）—— "
            "该档只会有 guard 档的结论")
    if before.not_selected:
        report.notes.append(
            "未入选的现状 validator 档资产（trigger 收窄:lang/role/complexity/…）: "
            + ", ".join(before.not_selected)
        )
    if not after.guard_available:
        report.notes.append(
            "guard 档不可用（"
            + (after.guard_tier.reason if after.guard_tier else "guards_module_absent")
            + "）—— SQL 域判定整档计入无法判定")

    report.rows = diff_assembly(
        corpus.items, before=before, after=after,
        include_questions=include_questions, lang=lang,
    )
    return report
