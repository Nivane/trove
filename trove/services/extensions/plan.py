"""运行时装配清单(``trove extensions plan``)—— 信封 × 运行时对账(E6)。

两张视图,一个问题:「现在到底有哪些资产会动?」

- **信封静态视图**:``collect_assets`` 的每封资产的 ``state`` 列
  (confirmed / pending / rejected / disabled)。它是**声明面**:全量列出,
  含未生效的 —— 治理要看得见「存在但停了」。
- **运行时装配清单**(本模块的 ``entries``):信封 × state 的投影 ——
  只有 ``state == "confirmed"`` 的资产按其挂点展开。``disabled``(E6 的
  颗粒停用)被摘除,pending/rejected 同理:它们本来就不投递。

  「影响面」的运行时镜像:``trove validate --impact``(E4)回答「装上会改变
  什么」,``--plan`` 回答「现在装着什么」。

**对账(本模块的另一半)**:``drift`` 把两份**同一真理的副本**摆在一起比 ——
信封的 ``state`` 列(静态)与运行时读路径实际过滤的结果(动态):

- 运行时侧 = 各服务**自己的读函数**(与运行时同一份,绝不复写):
  ``SkillService.list_org(confirmed_only=True)`` 与
  ``KbService.load_decisions(...)`` 里 ``enabled`` 的规则 ——
  停用/确认的过滤就发生在那里;
- 静态侧 = 信封的 ``state == "confirmed"``。

两份今天由构造保证相等(同一份文件读出两遍),所以 ``drift`` 的价值是
**漂移时响亮**:改了一边忘了另一边(信封的 state 映射变了、或某条读路径
的过滤没了)时,差集非空、CLI 退出码 1。这与 ``trove validate`` 的检查器
族(声明集 ↔ 实现集,静态)是同一纪律的两半 —— R7 的双保险:
**静态对账在检查器族,运行时对账在这里**。

只对**有状态门**的资产对账(org skills 与决策规则):code skills 与
preset 没有停用位,两侧恒等,进了只会是噪音。四个消费面(注入 / 广告 /
validator+guard 执行 / 试跑枚举)全部经过同一个过滤点 ——
``skills/service.py`` 的 ``list_org(confirmed_only=True)``;面本身逐面
由 ``tests/services/skills/test_skills_disable.py`` 钉死。

零 LLM、零网络;读的是现成资产与现成读函数,不新增任何状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from trove.services.extensions.envelope import (
    ExtensionEnvelope,
    MountCatalog,
)

#: 对账只覆盖**有状态门**的资产种类 —— 名字即信封的 ``kind``。
#: skill 只取 ``source == "org"``(code skills 随包走,没有停用位)。
GATED_KINDS = ("skill", "decision")

#: 信封枚举里视为「活」的状态。运行时不投递的状态一律在清单外 ——
#: ``disabled`` 是 E6 的新值,``pending`` / ``rejected`` 同一条规则。
LIVE_STATE = "confirmed"


def _identity(kind: str, source: str, name: str) -> str:
    """对账用的资产身份:``kind:source:name``(同名跨源不会互相顶掉)。"""
    return f"{kind}:{source}:{name}"


# ── 运行时装配清单(信封 × state)──────────────────────────


@dataclass(frozen=True)
class PlanEntry:
    """清单的一行 = 一个挂点(资产 × 节点 × 档位 × 效果)。"""

    kind: str
    name: str
    source: str
    node: str
    tier: str
    effect: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind, "name": self.name, "source": self.source,
            "node": self.node, "tier": self.tier, "effect": self.effect,
        }


def plan_entries(envelopes: Sequence[ExtensionEnvelope]) -> list[PlanEntry]:
    """信封 × state → 运行时装配清单(**只有活资产**,按挂点逐行展开)。

    「disabled 摘除」由这一处决定:``state != "confirmed"`` 的资产整封不进
    —— 清单是运行时事实,不是资产目录(目录在 ``extensions list``)。
    """
    out = [
        PlanEntry(
            kind=env.kind, name=env.name, source=env.source,
            node=m.node, tier=m.tier, effect=m.effect,
        )
        for env in envelopes
        if env.state == LIVE_STATE
        for m in env.mounts
    ]
    return sorted(out, key=lambda e: (e.kind, e.name, e.source, e.node, e.tier))


# ── 对账:静态状态列 ↔ 运行时读路径 ───────────────────────


def expected_liveness(
    envelopes: Sequence[ExtensionEnvelope],
) -> dict[str, set[str]]:
    """静态侧:有状态门的资产里,信封说「活」的那些(按 kind 分组)。"""
    out: dict[str, set[str]] = {kind: set() for kind in GATED_KINDS}
    for env in envelopes:
        if env.kind not in GATED_KINDS:
            continue
        if env.kind == "skill" and env.source != "org":
            continue
        if env.state == LIVE_STATE:
            out[env.kind].add(_identity(env.kind, env.source, env.name))
    return out


def runtime_liveness(
    project_root: Path,
    *,
    skills: Any | None = None,
    kb: Any | None = None,
) -> dict[str, set[str]]:
    """动态侧:运行时读路径实际取得到的活资产(与运行时同一份读函数)。

    - org skills → ``SkillService.list_org(confirmed_only=True)`` ——
      四个消费面共用的那个单点过滤(N8),同时是管理台之外的唯一读口;
    - 决策规则 → ``KbService.load_decisions(ds)`` 里 ``enabled`` 的规则 ——
      ``DecisionService`` / 调度 runner 判「跑不跑」用的就是这一个标志。

    刻意**不经过信封**:两边都从信封推的话,对账就成了同义反复 ——
    这里要的正是「声明面 vs 运行面」两份独立副本的对照。
    """
    if skills is None:
        from trove.services.skills.service import SkillService

        skills = SkillService(project_root / ".trove" / "skills",
                              git_enabled=False)

    out: dict[str, set[str]] = {kind: set() for kind in GATED_KINDS}
    for entry in skills.list_org(confirmed_only=True):
        name = str(entry.get("name") or "")
        if name:
            out["skill"].add(_identity("skill", "org", name))

    kb_dir = project_root / ".trove" / "kb"
    if kb_dir.is_dir():
        from trove.services.kb.service import KbService

        if kb is None:
            kb = KbService(project_root, git_kb=False)
        for ds_dir in sorted(kb_dir.iterdir()):
            if not ds_dir.is_dir() or not (ds_dir / "decisions.yml").exists():
                continue
            try:
                doc = kb.load_decisions(ds_dir.name)
            except Exception:  # noqa: BLE001 — 解析失败由 validate 的检查器点名
                continue
            for rule in doc.rules:
                if getattr(rule, "enabled", True):
                    out["decision"].add(
                        _identity("decision", f"kb:{ds_dir.name}",
                                  str(getattr(rule, "id", "") or "")))
    return out


def reconcile_plan(
    expected: dict[str, set[str]], actual: dict[str, set[str]],
) -> list[str]:
    """两份副本的对称差 → 漂移消息(空 = 对账通过)。纯函数,可单测。

    两个方向各有名字:运行时有、静态说没有 = **消费面漏了过滤**
    (停用的资产还在被投递);静态说有、运行时没有 = **两个副本漂移**
    (信封的能力声明与运行时行为对不上)。
    """
    drift: list[str] = []
    for kind in sorted(set(expected) | set(actual)):
        exp = expected.get(kind, set())
        act = actual.get(kind, set())
        for ident in sorted(act - exp):
            drift.append(
                f"{ident}: 运行时读路径取得到,但信封状态不是 {LIVE_STATE}"
                " —— 消费面可能漏了过滤(停用的资产仍在投递?)"
            )
        for ident in sorted(exp - act):
            drift.append(
                f"{ident}: 信封状态是 {LIVE_STATE},但运行时读路径取不到 —— "
                "两个副本已漂移(信封说的 ≠ 实际有的)"
            )
    return drift


# ── 报告 ─────────────────────────────────────────────────


@dataclass
class AssemblyPlan:
    """一次 ``--plan`` 的完整结果(text 渲染与 --json 同源)。"""

    entries: list[PlanEntry] = field(default_factory=list)
    #: 信封静态视图(每封的 kind/name/source/state)—— 对账的**原料**原样附上,
    #: 「与信封 diff 空」因此是一条可以自己跑的断言,不只是测试里的一句话。
    envelopes: list[dict[str, str]] = field(default_factory=list)
    #: 对账的两份副本:「活」资产的静态侧(信封 state)与动态侧(运行时读路径),
    #: 各自按 kind 分组、排序 —— 差集就是 ``drift``(JSON 里可直接 diff)。
    static_live: dict[str, list[str]] = field(default_factory=dict)
    runtime_live: dict[str, list[str]] = field(default_factory=dict)
    drift: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """对账通过(漂移为空)= 退出码 0;有漂移 = 1。绝不静默。"""
        return not self.drift

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "plan",
            "ok": self.ok,
            "exit_code": self.exit_code,
            "entries": [e.to_dict() for e in self.entries],
            "envelopes": list(self.envelopes),
            "static_live": {k: list(v) for k, v in self.static_live.items()},
            "runtime_live": {k: list(v) for k, v in self.runtime_live.items()},
            "drift": list(self.drift),
            "counts": dict(self.counts),
        }

    def render(self) -> str:
        lines = [
            "trove extensions plan —— 运行时实际装配清单"
            "(信封 × state;disabled/pending/rejected 摘除)",
            f"挂点行: {len(self.entries)}"
            f" | 信封: {self.counts.get('envelopes', 0)} 封"
            f"(其中活 {self.counts.get('live', 0)}"
            f" / 停用 {self.counts.get('disabled', 0)})",
        ]
        if self.entries:
            lines.append("")
            for e in self.entries:
                lines.append(
                    f"  {e.kind:9s} {e.name:28s} {e.source:10s} "
                    f"→ {e.node}({e.tier}): {e.effect}"
                )
        else:
            lines.append("（运行时装配清单为空：没有已确认的资产）")
        if self.drift:
            lines.append("")
            lines.append(f"对账漂移 ({len(self.drift)}):")
            for d in self.drift:
                lines.append(f"  ! {d}")
        lines.append("")
        n_static = sum(len(v) for v in self.static_live.values())
        n_runtime = sum(len(v) for v in self.runtime_live.values())
        lines.append(
            f"对账(信封静态视图 {n_static} 项 ↔ 运行时读路径 {n_runtime} 项): "
            + ("一致(diff 空)" if self.ok else f"漂移 {len(self.drift)} 条")
            + f" → 退出码 {self.exit_code}"
        )
        return "\n".join(lines)


def build_plan(
    project_root: str | Path = ".",
    *,
    catalog: MountCatalog | None = None,
    skills: Any | None = None,
    kb: Any | None = None,
) -> AssemblyPlan:
    """信封 → 运行时装配清单 + 对账。只读、零 LLM;坏文件降级不抛。"""
    from trove.services.extensions.sources import collect_assets

    root = Path(project_root)
    envelopes = collect_assets(root, catalog=catalog)
    entries = plan_entries(envelopes)
    static = expected_liveness(envelopes)
    runtime = runtime_liveness(root, skills=skills, kb=kb)
    drift = reconcile_plan(static, runtime)
    states: dict[str, int] = {}
    for env in envelopes:
        states[env.state] = states.get(env.state, 0) + 1
    return AssemblyPlan(
        entries=entries,
        envelopes=[
            {"kind": e.kind, "name": e.name, "source": e.source, "state": e.state}
            for e in envelopes
        ],
        static_live={k: sorted(v) for k, v in static.items()},
        runtime_live={k: sorted(v) for k, v in runtime.items()},
        drift=drift,
        counts={
            "envelopes": len(envelopes),
            "live": states.get(LIVE_STATE, 0),
            "disabled": states.get("disabled", 0),
            "mounts": len(entries),
        },
    )
