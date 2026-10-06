"""ExtensionEnvelope — 每个扩展资产的**编译产物**:把「声明存在」变成「推导出它真正读什么、挂在哪」。

设计稿《一切接缝皆契约》§3f(拍板点 D6):信封的全部价值建立在
**「信封说的 = 实际有的」**。所以 ``capabilities`` 是**推导物,不是作者声明**
(对照 Claude Code Mods 的声明制)—— 推导规则零 LLM、纯函数:

- 对每条 check 取 ``condition_variables(expr, 合法域)`` 的 identifiers 闭包,
  与合法域求交,并集即 ``variables``;
- ``effects`` 从 severity 推(blocking → takeover,advisory → watch),
  无 checks 的资产从**挂点目录静态表**推(required → inject,available → advertise);
- 推导不出的引用进 ``unresolved`` —— **响亮,不是静默省略**:静默省略会让
  「声明了但不生效」和「声明了且生效」从任何外部面看都一模一样,
  而那正是本模块存在的理由。

缓存键 = ``(资产名, 源文件 sha256, 变量域版本)``(§3f;名进键是施工时
发现的必要修正 —— 同源文件多资产会互相顶掉缓存)。域版本在词表变更时
必须 bump —— 否则旧缓存会把用旧词表推出来的能力集当成有效结果交付。

零 LLM、零网络、零 IO 副作用(只读文件算摘要)。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from trove.services.decision.expr import (
    VARIABLES as DECISION_VARIABLES,
)
from trove.services.decision.expr import (
    DecisionExprError,
    condition_variables,
)
from trove.services.skills.validators import (
    SEVERITIES,
    VALIDATOR_HOST,
    VALIDATOR_VARIABLES,
)

# ── 闭集词表 ─────────────────────────────────────────────

#: 资产种类。term-set / template 是预留位(KB 侧的聚合面尚未接入时,
#: 词表先闭死 —— 开放词表会让「拼错了」与「还没做」无法区分)。
KINDS = ("skill", "decision", "preset", "term-set", "template")

#: 生效状态。与 skills 服务的 status 词表同源(pending/confirmed/rejected),
#: ``disabled`` 是 E6 的停用位(写文件、进 git —— 状态必须有痕)。
STATES = ("confirmed", "pending", "rejected", "disabled")

#: 挂点的**效果**闭集 —— 一个资产在挂点上能做什么:
#:   inject    整篇注入节点提示(required 档 skill)
#:   advertise 只广告描述、按需加载(available 档 skill)
#:   watch     只报告,不改流程(advisory 断言 / 通知型规则)
#:   takeover  能改变流程或对外出动作(blocking 断言 / 带行动模板的规则)
EFFECTS = ("inject", "advertise", "watch", "takeover")

#: 挂点的**作用域**闭集:
#:   prompt  节点提示文本(required/available)
#:   result  结果域断言(validator;E2 的 guard 用 sql)
#:   sql     SQL 文本域断言(guard;E2 接入,词表先声明)
TARGETS = ("prompt", "result", "sql")

#: Mount 的 tier 闭集。前三档与 ``skills.service._TIERS`` 同源;``guard``
#: 由 E2 接入;``scheduled`` = 决策规则的调度判定面;``draft`` = preset
#: 交付的草稿队列(确认门之后才由各面各自的挂点接管)。
TIERS = ("required", "available", "validator", "guard", "scheduled", "draft")

#: 变量域版本 —— 参与缓存键。词表(expr.VARIABLES / VALIDATOR_VARIABLES /
#: E2 的 GUARD_VARIABLES)每改一次必须 bump,否则旧缓存会越过变更继续生效。
DOMAIN_VERSIONS: dict[str, str] = {
    "decision": "decision-v1",
    "validator": "validator-v1",
}

#: severity → 效果。闭集:词表外的 severity 进 unresolved(该条永远不拦,
#: 从外面看却像配好了 —— 正是要响亮报出的那类漂移)。
_SEVERITY_EFFECTS: dict[str, str] = {
    "advisory": "watch",
    "blocking": "takeover",
}


# ── 数据形状(纯数据,to_dict 即 --json 的字节) ────────────


@dataclass(frozen=True)
class Mount:
    """一个节点级挂点:资产在 ``node`` 上以 ``tier`` 档投递,效果为 ``effect``。"""

    node: str
    tier: str
    effect: str

    def to_dict(self) -> dict[str, str]:
        return {"node": self.node, "tier": self.tier, "effect": self.effect}


@dataclass(frozen=True)
class Provenance:
    """来源链:文件清单 + 合并摘要 + git 修订(有就给)。

    ``sha256`` 是**逐文件 (路径, 内容摘要) 行的合并摘要** —— 确定性、
    对任何文件的内容变化敏感;路径参与摘要,因为「谁提供的」本身就是
    来源的一部分(同内容换路径 = 换了来源,重算)。
    """

    files: tuple[str, ...] = ()
    sha256: str = ""
    git_rev: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": list(self.files),
            "sha256": self.sha256,
            "git_rev": self.git_rev,
        }


@dataclass(frozen=True)
class Capabilities:
    """推导出的能力集 —— **不是作者声明**。

    ``variables``:check 表达式实际读到的变量(identifiers 闭包 ∩ 合法域);
    ``effects``:  该资产在运行时能做什么(EFFECTS 闭集);
    ``targets``:  作用域(prompt/result/sql)。
    """

    variables: frozenset[str] = frozenset()
    effects: frozenset[str] = frozenset()
    targets: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "variables": sorted(self.variables),
            "effects": sorted(self.effects),
            "targets": sorted(self.targets),
        }


@dataclass(frozen=True)
class ExtensionEnvelope:
    """一个扩展资产的完整信封(§3f 结构,字段一一对应)。"""

    kind: str
    name: str
    source: str                 # code | builtin | org | kb:<ds> | preset:<name>
    state: str                  # STATES 闭集
    mounts: tuple[Mount, ...] = ()
    capabilities: Capabilities = field(default_factory=Capabilities)
    provenance: Provenance = field(default_factory=Provenance)
    unresolved: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "source": self.source,
            "state": self.state,
            "mounts": [m.to_dict() for m in self.mounts],
            "capabilities": self.capabilities.to_dict(),
            "provenance": self.provenance.to_dict(),
            "unresolved": list(self.unresolved),
        }


@dataclass(frozen=True)
class MountCatalog:
    """挂点目录的**静态表** —— 无 checks 的资产的能力从这里推。

    常量本体住在 validate(``SKILL_NODES`` 是对账母版的家);这里做的是
    **函数内延迟导入**,避免「validate 导入 extensions、extensions 导入
    validate」的模块级环。运行时注入点若变化,信封随 SKILL_NODES 一起动 ——
    不产生第三份名单。
    """

    inject_nodes: tuple[str, ...] = ()
    ad_nodes: tuple[str, ...] = ()
    validator_host: str = VALIDATOR_HOST

    @classmethod
    def from_validate(cls) -> "MountCatalog":
        from trove.services.validate.service import (  # 延迟:见类 docstring
            AVAILABLE_AD_NODES,
            SKILL_NODES,
        )

        return cls(
            inject_nodes=tuple(SKILL_NODES),
            ad_nodes=tuple(AVAILABLE_AD_NODES),
            validator_host=VALIDATOR_HOST,
        )


# ── 摘要与缓存 ───────────────────────────────────────────


def digest_files(files: Sequence[Path | str]) -> str:
    """逐文件 (路径, 内容 sha256) 行的合并摘要;空输入 → 空串。

    路径用调用方给的形态(相对项目根的相对路径或绝对路径都行)——
    只要同一次运行内一致,缓存键就是确定性的。
    """
    if not files:
        return ""
    h = hashlib.sha256()
    for f in files:
        p = Path(f)
        try:
            content = p.read_bytes()
        except OSError:
            content = b""
        h.update(f"{p}\0".encode())
        h.update(hashlib.sha256(content).hexdigest().encode())
        h.update(b"\n")
    return h.hexdigest()


#: 进程级缓存。读取面是只读的、推导是纯函数,所以缓存永远不会 stale ——
#: 键里的文件摘要变了就 miss。测试用 ``clear_cache()`` 归零。
_CACHE: dict[tuple[str, str], ExtensionEnvelope] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _cached(name: str, files: Sequence[Path | str], domain_version: str,
            build: Any) -> ExtensionEnvelope:
    """缓存键 =(资产名, 源文件 sha256, 变量域版本)。

    **名必须进键**:同一种资产可以共享一个源文件(三个 code skill 同住
    一份 manifest),只按文件摘要缓存会让它们互相顶掉 —— 撞出的结果每个
    字段都"看起来对",只是属于另一个资产,是最安静的那种坏法。
    """
    key = (name, digest_files(files), domain_version)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    env = build()
    _CACHE[key] = env
    return env


def _provenance(files: Sequence[Path | str], git_rev: str) -> Provenance:
    return Provenance(
        files=tuple(str(f) for f in files),
        sha256=digest_files(files),
        git_rev=git_rev,
    )


# ── 推导 ─────────────────────────────────────────────────


def derive_check_capabilities(
    checks: Sequence[Any], domain: frozenset[str],
) -> tuple[Capabilities, tuple[str, ...]]:
    """check 列表 → (能力集, unresolved)。

    ``domain`` 是合法变量域(validator → VALIDATOR_VARIABLES;guard 由 E2
    接入时传 GUARD_VARIABLES)。表达式在域外 → ``DecisionExprError`` →
    进 unresolved(手写文件绕过了写入面校验;读面必须响亮,不能静默丢)。
    """
    variables: set[str] = set()
    effects: set[str] = set()
    unresolved: list[str] = []
    for i, c in enumerate(checks):
        if not isinstance(c, Mapping):
            unresolved.append(f"checks[{i}] 不是 mapping")
            continue
        expr = str(c.get("expr") or "").strip()
        if not expr:
            unresolved.append(f"checks[{i}].expr 为空")
            continue
        try:
            variables |= condition_variables(expr, domain)
        except DecisionExprError as exc:
            unresolved.append(f"checks[{i}].expr: {exc}")
            continue
        severity = str(c.get("severity") or "advisory")
        effect = _SEVERITY_EFFECTS.get(severity)
        if effect is None:
            unresolved.append(
                f"checks[{i}].severity {severity!r} 不在 {SEVERITIES} —— "
                "该条永远不拦"
            )
            continue
        effects.add(effect)
    return (
        Capabilities(variables=frozenset(variables), effects=frozenset(effects)),
        tuple(unresolved),
    )


def _skill_targets(tier: str) -> frozenset[str]:
    """档位 → 作用域静态表(空 checks 的资产从这里推)。"""
    if tier in ("required", "available"):
        return frozenset({"prompt"})
    if tier == "validator":
        return frozenset({"result"})
    if tier == "guard":          # E2 接入;词表先声明,免得届时改语义
        return frozenset({"sql"})
    return frozenset()


# ── 构建器(资产种类各一个;文件=该资产的源文件) ───────────


def build_skill_envelope(
    entry: Mapping[str, Any], *,
    files: Sequence[Path | str],
    source: str,
    catalog: MountCatalog,
    git_rev: str = "",
) -> ExtensionEnvelope:
    """org/code skill → 信封。读的是 ``read_skill`` 已解析的 entry。"""
    name = str(entry.get("name") or "")
    tier = str(entry.get("tier") or "available")
    status = str(entry.get("status") or "pending")
    triggers = entry.get("triggers") or {}
    declared = triggers.get("node") if isinstance(triggers, Mapping) else None
    if declared is not None:
        declared = str(declared).strip() or None

    unresolved: list[str] = []
    if entry.get("error"):
        unresolved.append(str(entry["error"]))

    mounts: list[Mount] = []
    if tier == "required":
        nodes = [declared] if declared else list(catalog.inject_nodes)
        for node in nodes:
            if node in catalog.inject_nodes:
                mounts.append(Mount(node, "required", "inject"))
            else:
                unresolved.append(
                    f"triggers.node {node!r} 不是注入节点 —— "
                    "没有任何投递面会取用它"
                )
        caps = Capabilities(
            effects=frozenset({"inject"}), targets=_skill_targets(tier))
    elif tier == "available":
        nodes = [declared] if declared else list(catalog.ad_nodes)
        for node in nodes:
            if node in catalog.ad_nodes:
                mounts.append(Mount(node, "available", "advertise"))
            else:
                unresolved.append(
                    f"triggers.node {node!r} 不是广告节点 —— "
                    f"available 档只在 {'/'.join(catalog.ad_nodes)} 广告"
                )
        caps = Capabilities(
            effects=frozenset({"advertise"}), targets=_skill_targets(tier))
    elif tier == "validator":
        caps, check_unresolved = derive_check_capabilities(
            entry.get("checks") or [], VALIDATOR_VARIABLES)
        unresolved += list(check_unresolved)
        # 宿主是 VALIDATOR_HOST(常量,同运行期);声明了别的节点 = 一条
        # 永远不运行的配置 —— 写入面会拒,手写文件这里响亮。
        if declared is not None and declared != catalog.validator_host:
            unresolved.append(
                f"triggers.node {declared!r} ≠ {catalog.validator_host!r} —— "
                "结果断言只在宿主节点运行"
            )
        effect = next(iter(caps.effects)) if caps.effects else "watch"
        mounts.append(Mount(catalog.validator_host, "validator", effect))
        caps = Capabilities(
            variables=caps.variables, effects=caps.effects,
            targets=_skill_targets(tier))
    else:
        unresolved.append(f"tier {tier!r} 不在 {TIERS} —— 没有任何投递面")
        caps = Capabilities()

    def _build() -> ExtensionEnvelope:
        return ExtensionEnvelope(
            kind="skill", name=name, source=source, state=status,
            mounts=tuple(mounts), capabilities=caps,
            provenance=_provenance(files, git_rev),
            unresolved=tuple(unresolved),
        )

    return _cached(name, files, DOMAIN_VERSIONS["validator"], _build)


def build_decision_envelope(
    rule: Any, *,
    files: Sequence[Path | str],
    datasource: str,
    git_rev: str = "",
) -> ExtensionEnvelope:
    """一条决策规则 → 信封。``rule`` 是 ``decision.rules.DecisionRule``。"""
    variables: set[str] = set()
    unresolved: list[str] = []
    for i, cond in enumerate(getattr(rule, "conditions", ()) or ()):
        try:
            variables |= condition_variables(str(cond), DECISION_VARIABLES)
        except DecisionExprError as exc:
            unresolved.append(f"conditions[{i}]: {exc}")

    effect = "takeover" if getattr(rule, "action", None) is not None else "watch"
    caps = Capabilities(
        variables=frozenset(variables),
        effects=frozenset({effect}),
        targets=frozenset({"result"}),
    )

    rule_id = str(getattr(rule, "id", "") or "")

    def _build() -> ExtensionEnvelope:
        return ExtensionEnvelope(
            kind="decision",
            name=rule_id,
            source=f"kb:{datasource}",
            state="confirmed" if getattr(rule, "enabled", True) else "disabled",
            mounts=(Mount("decisions", "scheduled", effect),),
            capabilities=caps,
            provenance=_provenance(files, git_rev),
            unresolved=tuple(unresolved),
        )

    return _cached(rule_id, files, DOMAIN_VERSIONS["decision"], _build)


def _preset_item_caps(
    items: Sequence[Any], domain: frozenset[str],
) -> tuple[Capabilities, tuple[str, ...]]:
    """preset 的模板条目 → 能力并集。

    条目可能是**引用**(``{name: X}``,指向既有资产 —— 能力归被引资产,
    这里不重复计)或**模板**(就地声明的骨架)。引用跳过,模板推导。
    """
    variables: set[str] = set()
    effects: set[str] = set()
    targets: set[str] = set()
    unresolved: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        if "checks" in item:                      # 技能骨架
            caps, errs = derive_check_capabilities(item.get("checks") or [], domain)
            variables |= caps.variables
            effects |= caps.effects
            targets.add("result")
            unresolved += [f"items[{i}].{e}" for e in errs]
        elif "conditions" in item:                # 规则模板
            conds = item.get("conditions") or []
            if isinstance(conds, Mapping):        # {all|any: [...]}
                conds = next(iter(conds.values()), [])
            for j, cond in enumerate(conds or []):
                try:
                    variables |= condition_variables(str(cond), DECISION_VARIABLES)
                except DecisionExprError as exc:
                    unresolved.append(f"items[{i}].conditions[{j}]: {exc}")
            effects.add("takeover" if item.get("action") else "watch")
            targets.add("result")
        elif "tier" in item:                      # 技能引用的显式形态
            tier = str(item.get("tier"))
            if tier == "required":
                effects.add("inject")
                targets.add("prompt")
            elif tier == "available":
                effects.add("advertise")
                targets.add("prompt")
    return (
        Capabilities(
            variables=frozenset(variables), effects=frozenset(effects),
            targets=frozenset(targets)),
        tuple(unresolved),
    )


def build_preset_envelope(
    preset: Any, *, git_rev: str = "",
) -> ExtensionEnvelope:
    """一份 preset → 信封。挂点 = 各面的草稿队列(确认门之后由各自接管)。"""
    files = [preset.path] if getattr(preset, "path", "") else []
    mounts: list[Mount] = []
    unresolved: list[str] = []
    variables: set[str] = set()
    effects: set[str] = set()
    targets: set[str] = set()

    skills_caps, skills_errs = _preset_item_caps(
        preset.skills, VALIDATOR_VARIABLES)
    decisions_caps, decisions_errs = _preset_item_caps(
        preset.decisions, DECISION_VARIABLES)
    for caps in (skills_caps, decisions_caps):
        variables |= caps.variables
        effects |= caps.effects
        targets |= caps.targets
    unresolved += list(skills_errs) + list(decisions_errs)

    if preset.skills:
        mounts.append(Mount("skills", "draft", "inject"))
    if preset.decisions:
        mounts.append(Mount("decisions", "draft", "watch"))
    if preset.domains:
        mounts.append(Mount("semantics", "draft", "inject"))

    def _build() -> ExtensionEnvelope:
        return ExtensionEnvelope(
            kind="preset",
            name=str(preset.name),
            source=str(getattr(preset, "source", "builtin")),
            state="confirmed",   # preset 本体无确认门;apply 才是门
            mounts=tuple(mounts),
            capabilities=Capabilities(
                variables=frozenset(variables), effects=frozenset(effects),
                targets=frozenset(targets)),
            provenance=_provenance(files, git_rev),
            unresolved=tuple(unresolved),
        )

    # preset 横跨两个域(技能骨架 + 规则模板),缓存键必须含两者的版本。
    _preset_domain = (
        f"preset:{DOMAIN_VERSIONS['validator']}+{DOMAIN_VERSIONS['decision']}")
    return _cached(str(preset.name), files, _preset_domain, _build)
