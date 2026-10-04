"""SkillService — org-level methodology skills as admin-managed assets.

Design boundary (mirrors ``trove/prompts/skills/``): skills carry
*cross-datasource* methodology (how to plan / how to diagnose / org analysis
conventions). Facts about a datasource belong in the KB, not here.

Two sources are merged at render time:

- **code skills** (``trove/prompts/skills/``): read-only methodology shipped
  with the product. Always matched by the manifest trigger conditions and
  injected in full — they are de-facto ``required``.
- **org skills** (``.trove/skills/<name>/SKILL.md``): admin-managed assets with
  a `pending → confirmed` gate mirroring the KB lessons/examples flow. Drafts
  never enter prompts or the load_skill tool until an admin confirms. Two
  tiers control injection:

  - ``tier: required`` — full body is injected into the system prompt of the
    node(s) its triggers match (single-shot nodes: query_sketch / analyze_error,
    and the agentic gen_sql system block).
  - ``tier: available`` — only the description is advertised (``<available_skills>``
    block); the body is loaded on demand via the ``load_skill`` tool in the
    agentic gen_sql loop, so many skills cost only a short description in the
    prompt.

Skills without a ``triggers.node`` are global — they apply to every node.
A blank node (``node: ""``) reads as undeclared too: the field is left unfilled
far more often than it is meant literally, and treating it as a declaration
would make the skill never match — indistinguishable, from the outside, from
not existing.

A third tier, ``validator``, is not an injection tier: its criteria run
post-hoc against the result (``trove/services/skills/validators.py``) and its
body is never delivered to the model. It is set by hand in ``SKILL.md`` —
``set_tier`` only moves between ``required`` and ``available``.

All four delivery paths — required injection, available advertisement,
validator execution and on-demand ``load_skill`` — share **one** trigger
predicate (``_trigger_mismatch``). A declared ``role`` / ``lang`` / … narrows
every path, so naming a skill directly cannot bypass it.

Governance (P2 — effect / rollback / disable):

- **Effect**: every read path re-reads the files; every gate reads the admin
  switch from the live ``AgentConfig`` on each call. Flipping a switch or
  editing a file takes effect on the next question — no restart, no cache.
- **Rollback**: every write path (create / confirm / reject / set_tier / body
  / rollback) auto-commits through the same ``GitVersioning`` the KB uses
  (scoped staging, degrade-to-no-op — versioning never blocks the write it
  records). ``version`` is a **revision counter**: any content change
  (confirm / body rewrite / tier change / rollback) increments it, so the
  audit history reads as a monotonically growing series.
- **Disable**: ``agent.extensions.org_extensions_enabled`` (default true) is
  a single master switch for *this org surface only* — injection, the
  ``load_skill`` advertisement, on-demand loading and validator assertions
  all short-circuit when it is off. Code skills (``trove/prompts/skills/``),
  the KB and few-shots are untouched; the admin write/list surface stays
  usable (the switch stops **consumption**, not management).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from trove.llm.injection import scan_injection
from trove.prompts.skills import fence_org_skill, match_trigger
from trove.prompts.skills import render_skills as _code_render
from trove.services.kb.git_versioning import GitVersioning
from trove.services.skills.validators import SEVERITIES, VALIDATOR_HOST

# name = lowercase letters/digits + hyphens; also a safe directory name.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TIERS = ("required", "available", "validator")
#: 本期**只**驱动 deterministic。``llm`` 档(Datus 式散文检查)是 P5 ——
#: 本期在**写入面**就挡掉:允许配一份没人跑的配置,等于制造静默失效。
#: runner 侧仍留一条兜底(手写 SKILL.md 能绕过 create),报"判不了"而不是跳过。
_VALIDATOR_MODES = ("deterministic",)
# ``severity`` 的闭集**不在这里定义** —— 与运行期(``run_validators``)共用
# 上面 import 的 ``SEVERITIES``。两处各写一份的话,"写入放行、执行侧当未知"
# 这条漂移会重新打开"配了却静默失效"那类事故。

#: 本期**只**驱动 result。另两个都无处受理:
#: - ``sql``:run_validators 吃的是结果集,没有"SQL 文本"这个可断言对象;
#: - ``answer``:output 是图的终点、没有回退边,答案级检查只能 advisory(P5)。
#: 同 mode —— 声明一个没人管的 target 等于静默失效,当场拒比静默保留好。
_TARGETS = ("result",)

#: validator 档专属字段 —— 出现在别的档位上就是配置错误(不是宽容地忽略:
#: 写下去也永远不会生效,当场拒比静默保留一份死配置好)。
VALIDATOR_FIELDS = ("mode", "severity", "targets", "checks")
_STATUSES = ("pending", "confirmed", "rejected")

FRONTMATTER_FIELDS = (
    "name", "description", "triggers", "tier", "status",
    "source", "lang", "version", "created_at", "updated_at",
)

#: ``create`` 接受的输入键 = frontmatter 词表 + validator 四字段 + 正文。
#: 校验**只在写入时**做:手写的存量文件(可能带未知键)必须照常可读可确认
#: ——读路径宽容是这一层的另一半(见 ``read_skill``)。
#: 此前 FRONTMATTER_FIELDS 只是个死常量、没有任何校验:未知键经 YAML 往返
#: 被静默保留进 SKILL.md,写错了没人告诉你。
_CREATE_INPUT_FIELDS = (
    frozenset(FRONTMATTER_FIELDS) | frozenset(VALIDATOR_FIELDS) | {"body"}
)


def _declared_node(triggers: dict) -> Any:
    """``triggers.node`` 的**声明值** —— 空串 / 纯空白等同**未声明**(返回 None)。

    ``node:``(YAML 留空)解析成 ``None``,``node: ""`` 是同一个意思的另一种
    写法,而 ``create`` 只在 API 边界上挡后者 —— 手写 SKILL.md 可以带进来,
    而手写正是本期 P1/P2 唯一的授权路径。当成"已声明"的后果是这条技能
    **永远不命中**:required 不注入、available 不广告、``load_skill`` 按名也
    取不到,从任何外部面看都与"没写 node"一样。同一个意思的两种写法不该有
    相反的行为。

    **非字符串的畸形声明不在此列**:收窄判据在信息不明时按"不命中"处理是
    保守的一侧,而把它读成未声明会让一条本该收窄的技能变成**全局**。那条
    留给写入面(或另开一条可观测的降级路),不是这里能顺手决定的。
    """
    node = triggers.get("node")
    if isinstance(node, str) and not node.strip():
        return None
    return node


class SkillService:
    """Manage + render org skills, merged with the built-in code skills."""

    def __init__(self, root: Path | None = None, llm: Any = None, *,
                 git_enabled: bool = True, config: Any = None):
        self.root = Path(root) if root is not None else Path.cwd() / ".trove" / "skills"
        self.llm = llm
        # 扩展面治理开关的宿主(``AgentConfig``)。管理端改的是**同一个活对象**
        # (``apply_overrides`` 就地改),所以这里是"每问现读"的读书处,不是快照。
        # 不传(旧构造点/纯单测)→ ``_org_enabled()`` 视为全开。
        self.config = config
        # 写路径的 git 自动版本化 —— 与 KB 同一个 ``GitVersioning``,守卫
        # (只暂存点名文件 / 失败降级 no-op)因此只有一份实现。enabled=False
        # 或树不在 git worktree 里时,后续 commit 全部降级、写入照常。
        self.git = GitVersioning(self.root, enabled=True) if git_enabled else None

    # ── Governance switch (``agent.extensions.*``) ─────────

    def _org_enabled(self) -> bool:
        """组织扩展总开关 —— **每次调用现读**(热生效,无缓存)。

        ``agent.extensions.org_extensions_enabled``(默认 true)只停「组织
        扩展」这一层:org skills 的注入与 ``load_skill`` 广告、按名取正文、
        validator 断言。code skills / KB / few-shots 一律不受影响。

        "现读"而不是"构造时快照":管理端 PUT 改的是共享的 ``AgentConfig``
        实例(``apply_overrides`` 就地改),构造期快照会让开关在重启前失效
        ——而"改了没生效"正是这一层最要防的静默结局。
        """
        ext = getattr(self.config, "extensions", None)
        if ext is None:
            return True
        return bool(getattr(ext, "org_extensions_enabled", True))

    # ── Paths / IO ────────────────────────────────────────

    def skill_dir(self, name: str) -> Path:
        return self.root / name

    def skill_path(self, name: str) -> Path:
        return self.skill_dir(name) / "SKILL.md"

    @staticmethod
    def _parse_skill(text: str) -> dict:
        """Parse ``SKILL.md`` frontmatter + body."""
        lines = text.split("\n")
        if not lines or lines[0].strip() != "---":
            return {"error": "missing frontmatter (must start with '---')"}
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            return {"error": "missing closing '---'"}
        try:
            meta = yaml.safe_load("\n".join(lines[1:end])) or {}
        except yaml.YAMLError as exc:
            return {"error": f"invalid frontmatter: {exc}"}
        body = "\n".join(lines[end + 1:]).strip()
        return {"meta": meta, "body": body}

    @staticmethod
    def _normalized_version(meta: dict) -> int:
        """从 frontmatter 读修订号;非整数(含 bool)/缺失 → 1 —— 读路径不抛。"""
        raw = meta.get("version", 1)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 1

    def read_skill(self, name: str) -> dict | None:
        """Load one org skill (meta + body). None when absent/invalid."""
        path = self.skill_path(name)
        if not path.exists():
            return None
        parsed = self._parse_skill(path.read_text(encoding="utf-8"))
        if "meta" not in parsed:
            return {"name": name, "error": parsed.get("error", "parse failed")}
        meta = parsed["meta"]
        entry = {
            "name": meta.get("name", name),
            "description": meta.get("description", ""),
            "triggers": meta.get("triggers") or {},
            "tier": meta.get("tier", "available"),
            "status": meta.get("status", "pending"),
            "source": meta.get("source", "admin"),
            "lang": meta.get("lang", "en"),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
        }
        # version:org skill 的**修订计数**(仅 .trove/skills/ 下的文件;code
        # skills 不走这里)。create 初始 1;此后每次内容变更(confirm / 正文
        # 重写 / tier 变更 / rollback)由 ``_rewrite_field`` 递增 —— 版本号在
        # 审计史里单调前进,回滚因此是"新的一版"而不是时间倒流。
        # 读路径保持宽容:遗留文件没有该字段 → 读出 1;手写进非整数的同样
        # 按 1 解释,绝不抛。
        entry["version"] = self._normalized_version(meta)
        # validator 专属字段**条件带上**:非 validator 档的返回形状保持不变
        # (既有调用方按 exact dict 断言的话,无条件加键会打碎它们)。
        if entry["tier"] == "validator":
            entry["mode"] = meta.get("mode", "deterministic")
            entry["severity"] = meta.get("severity", "advisory")
            entry["targets"] = meta.get("targets") or []
            entry["checks"] = meta.get("checks") or []
        entry["body"] = parsed["body"]
        return entry

    def list_org(self, confirmed_only: bool = False) -> list[dict]:
        """List org skills (sorted by name); invalid files surface with error."""
        if not self.root.exists():
            return []
        out: list[dict] = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not (d / "SKILL.md").exists():
                continue
            entry = self.read_skill(d.name)
            if entry is None:
                continue
            if confirmed_only and entry.get("status") != "confirmed":
                continue
            if entry.get("error"):
                entry.pop("body", None)
            out.append(entry)
        return out

    def list_code_skills(self) -> list[dict]:
        """Metadata of the shipped code skills (for the admin list)."""
        out = []
        manifest_path = Path(__file__).parent.parent.parent / "prompts" / "skills" / "manifest.yml"
        if not manifest_path.exists():
            return out
        for entry in (yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or []):
            out.append({
                "name": entry.get("name", ""),
                "description": (entry.get("description") or "").strip(),
                "triggers": entry.get("triggers") or {},
                "tier": "required",
                "status": "confirmed",
                "source": "code",
                "body": None,
            })
        return out

    def list_all(self, confirmed_only: bool = False) -> list[dict]:
        """Merged code + org skill list (admin console view)."""
        return self.list_code_skills() + self.list_org(confirmed_only=confirmed_only)

    # ── CRUD (draft → admin confirm/reject) ──────────────

    @staticmethod
    def _validate_validator_spec(entry: dict) -> dict:
        """validator 档的写入时校验 —— **全部在落盘前**做。

        运行期才发现配置写错 = validator 静默失效,而静默失效从外面看和
        "检查通过"一模一样。表达式预解析是这里最重要的一条:它把 `mn >= 0`
        (拼错)从"永远判不了"变成一条带位置的 400。
        """
        from trove.services.decision.expr import (
            VALIDATOR_VARIABLES,
            DecisionExprError,
            parse_condition,
        )

        mode = entry.get("mode") or "deterministic"
        if mode not in _VALIDATOR_MODES:
            raise ValueError(f"mode must be one of {_VALIDATOR_MODES}")
        severity = entry.get("severity") or "advisory"
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        # mode 现在只可能是 deterministic,所以 checks 的必填与预解析
        # 是无条件执行的 —— 不留一个"将来 llm 档再说"的空分支。
        # (mode=llm 的正文本身就是检查指令,那条通用的 body 非空校验覆盖它。)
        #
        # checks 排在 targets 之前:**一条 validator 没有 checks 就什么都判不了**,
        # 而 targets 有默认语义(本期只有 result);set_tier 这条旁路上的 entry
        # 两个键都没有(非 validator 档 read_skill 不带 validator 字段),
        # 先报 targets 会把"这份 skill 根本没有检查"这条最要命的信息盖过去。
        checks = entry.get("checks") or []
        if not checks:
            raise ValueError("checks is required")
        if not isinstance(checks, list):
            # 手写 YAML 的标量/mapping 笔误:或者进 enumerate 抛 TypeError
            # (500),或者按 string key 迭代后死在 ``.get`` 上。契约是
            # ValueError(400),所以形状在这里就要挡住。
            raise ValueError("checks must be a list")
        for i, c in enumerate(checks):
            if not isinstance(c, dict):
                raise ValueError(f"checks[{i}] must be a mapping")
            expr = str(c.get("expr") or "").strip()
            if not expr:
                raise ValueError(f"checks[{i}].expr is required")
            try:
                parse_condition(expr, VALIDATOR_VARIABLES)
            except DecisionExprError as exc:
                raise ValueError(f"checks[{i}].expr: {exc}") from exc
        targets = entry.get("targets") or []
        if not isinstance(targets, list) or not targets:
            raise ValueError("targets must be a non-empty list")
        for t in targets:
            if t not in _TARGETS:
                raise ValueError(f"target must be one of {_TARGETS}")
        # 宿主节点:本期 targets 只有 result,而 result 断言只在 VALIDATOR_HOST
        # 运行 —— 写别的 node 是一条**永远不运行**的配置。写入时拒掉它,读取时
        # (validators_for + run_validators)对绕过写入的手写文件降级为"判不了":
        # 一条不变量,两个入口都不留静默结局。
        # P5 的 targets: answer 会让宿主不再是唯一一个节点,那时这条守卫要
        # 跟着放宽(按 target 映射宿主),而不是删掉。
        # 空串由下面 create 的通用非空校验报(它的话更准:问题是**没填**,
        # 不是"填了别的节点")——``_declared_node`` 让这里读到 None 就够了。
        declared = _declared_node(entry.get("triggers") or {})
        if declared is not None and declared != VALIDATOR_HOST:
            raise ValueError(
                f"triggers.node must be {VALIDATOR_HOST!r} (or omitted) for tier=validator: "
                f"result assertions only run at the {VALIDATOR_HOST} node"
            )
        return {"mode": mode, "severity": severity, "targets": targets,
                "checks": checks}

    def create(self, entry: dict, *, actor: str = "") -> dict:
        """Create an org skill as a *pending* draft. Returns the saved entry.

        写入面先把未知键拦掉(报错并列出合法字段):未知键经 YAML 往返会被
        静默保留进 SKILL.md —— 写错了没人告诉你,而这份配置从任何外部面看
        都与"写对了"一样。**只查写入面**:``read_skill``/``confirm`` 对存量
        文件(含手工未知键)照常宽容,否则一条校验会把盘上已有的文件锁死。

        ``status``/``created_at``/``updated_at``/``version`` 属服务端管理:
        这里一律写 ``pending`` / 当前时间 / ``version: 1``(重写路径原样保留)。
        """
        unknown = sorted(k for k in entry if k not in _CREATE_INPUT_FIELDS)
        if unknown:
            raise ValueError(
                f"unknown field(s): {', '.join(unknown)}; legal fields: "
                f"{', '.join(sorted(_CREATE_INPUT_FIELDS))}"
            )
        name = (entry.get("name") or "").strip()
        description = (entry.get("description") or "").strip()
        body = (entry.get("body") or "").strip()
        tier = entry.get("tier", "available")
        if not _NAME_RE.match(name):
            raise ValueError(
                "invalid skill name: must match ^[a-z0-9][a-z0-9-]*$"
            )
        if not description:
            raise ValueError("description is required")
        if not body:
            raise ValueError("body is required")
        if tier not in _TIERS:
            raise ValueError(f"tier must be one of {_TIERS}")
        if (self.root / name / "SKILL.md").exists():
            raise ValueError(f"skill already exists: {name}")

        if tier == "validator":
            validator_meta = self._validate_validator_spec(entry)
        else:
            validator_meta = {}
            for f in VALIDATOR_FIELDS:
                if entry.get(f) is not None:
                    raise ValueError(f"{f} is only valid for tier=validator")

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        triggers = entry.get("triggers") or {}
        node = triggers.get("node")
        if node is not None and (not isinstance(node, str) or not node.strip()):
            raise ValueError("triggers.node must be a non-empty string")
        meta = {
            "name": name,
            "description": description,
            "triggers": triggers,
            "tier": tier,
            "status": "pending",
            "source": entry.get("source", "admin"),
            "lang": entry.get("lang", "en"),
            **validator_meta,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        self.skill_dir(name).mkdir(parents=True, exist_ok=True)
        self.skill_path(name).write_text(
            f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8",
        )
        saved = self._with_scan(self.read_skill(name))
        self._commit("create", name, saved, actor=actor)
        return saved

    async def draft_with_llm(
        self, name: str, description: str, node: str, purpose: str, lang: str = "en",
    ) -> dict:
        """LLM-draft an org skill body from a spec → pending draft.

        Requires a configured LLM gateway; the body is drafted in the target
        language and written as a *pending* skill for admin confirmation —
        drafts never reach prompts/tools.
        """
        if self.llm is None:
            raise RuntimeError("no LLM gateway configured for skill drafting")
        from trove.prompts import render

        spec = {"skill_name": name, "description": description, "node": node, "purpose": purpose}
        prompt = render("skills/draft", lang=lang, **spec)
        model = getattr(self.llm, "model", None) or getattr(self.llm, "target", None)
        response = await self.llm.chat(
            model=model or "openai/gpt-4o",
            messages=[
                {"role": "system", "content": (
                    "You draft concise, reusable analysis-methodology skills "
                    "for an NL→SQL agent. Output only the skill body (Markdown): "
                    "ordered, imperative steps; no frontmatter, no YAML."
                )},
                {"role": "user", "content": prompt},
            ],
        )
        body = (response or "").strip()
        if not body:
            raise RuntimeError("LLM returned an empty skill draft")
        return self.create({
            "name": name, "description": description,
            "triggers": {"node": node} if node else {},
            "tier": "available", "lang": lang, "source": "llm", "body": body,
        })

    def _load_meta(self, name: str) -> dict | None:
        entry = self.read_skill(name)
        return entry if entry and "error" not in entry else None

    @staticmethod
    def _scan_entry(entry: dict) -> list[str]:
        """注入形状的模式名列表(空 = 干净)。扫的是**投递面**。

        - 描述 + 正文:required 档投正文,available 档只投描述(广告块);
        - ``checks[].message``:validator 违反时的判词会进 ``error_feedback``
          → 进 gen_sql 的 prompt;
        - ``checks[].expr``:``message`` 不是必填,``run_validators`` 在它为空
          时回落到 ``违反：{expr}`` / ``violated: {expr}``(跟随 ``lang``)——
          同一条判词路,而表达式语法收字符串字面量,一棵**能解析**的表达式树
          同样能夹带散文。
        **投递面变了扫描面就得跟着变** —— validator 档新增了一条投递路,
        扫描面也必须多扫一处,否则"同一个缺口换个 tier 就绕过去"。

        形状守卫与 ``run_validators`` 同一套:``checks`` 不是可迭代的、或元素
        不是 mapping 的一律**跳过**。跳过的依据是"它到不了投递面" —— 运行期
        以 ``malformed check (expected a mapping)`` 拒它,原文进不了判词;把
        "畸形"记成一条命中是把两件事混成一件。手写 ``SKILL.md`` 正是绕开
        ``create`` 的那条路,不守这里就等于让畸形配置在**确认**那一刻炸成 500。
        """
        parts = [str(entry.get("description", "")), str(entry.get("body", ""))]
        checks = entry.get("checks") or []
        if isinstance(checks, Iterable):
            for c in checks:
                if not isinstance(c, dict):
                    continue
                parts.append(str(c.get("expr") or ""))
                parts.append(str(c.get("message") or ""))
        return scan_injection("\n".join(parts))

    def scan_skill(self, name: str) -> list[str]:
        """按名字扫一份 skill(不存在 → 空)。"""
        entry = self._load_meta(name)
        return self._scan_entry(entry) if entry else []

    @classmethod
    def _with_scan(cls, entry: dict) -> dict:
        """``create`` 的返回值挂上扫描结果 —— **只报不改**。

        草稿落盘时就报一次,管理员在**决定之前**看见。``confirm`` 用的是同一个
        扫描(``_scan_entry``)但**不经过这里**:它必须让落盘成为最后一个会抛的
        步骤,所以先扫后写、再把结果挂上去(见 ``confirm``)。
        正文是指令性文本,写它的人此刻在场 —— 是唯一能判断"这句是有意写的还是
        被灌进来的"的一方。把扫描放运行期只会静默毁内容(实测:一句话让整份
        方法论变成 ``[data: content isolated]``);放在这里则是一次可读的提示,
        看完确认,登记即豁免。
        """
        entry["injection_hits"] = cls._scan_entry(entry)
        return entry

    def confirm(self, name: str, *, actor: str = "") -> dict:
        """Admin confirmation: pending draft → confirmed (enters retrieval)."""
        entry = self._load_meta(name)
        if entry is None:
            raise KeyError(f"skill not found: {name}")
        # 落盘是这里**最后一个会抛**的步骤:先扫、后写。原先写的是
        # ``self._with_scan(self._rewrite_status(...))`` —— 参数先求值,扫描
        # 一抛 ``status: confirmed`` 就已经在盘上了:管理员看到 500 以为确认
        # 失败,而这份 skill 已经生效。治理门上写一半比哪一半都糟。
        # 扫落盘前的 entry 等价:扫描只读 description / body / checks,而
        # ``_rewrite_status`` 一个都不动。
        hits = self._scan_entry(entry)
        entry = self._rewrite_status(name, "confirmed")
        entry["injection_hits"] = hits
        self._commit("confirm", name, entry, actor=actor)
        return entry

    def reject(self, name: str, *, actor: str = "") -> dict:
        """Admin rejection: delete the draft directory."""
        d = self.skill_dir(name)
        if not (d / "SKILL.md").exists():
            raise KeyError(f"skill not found: {name}")
        import shutil

        shutil.rmtree(d)
        # 删除也进审计史:目录作用域的 add -A 只记录这个 skill 的删除。
        self._commit("reject", name, None, deleted=True, actor=actor)
        return {"name": name, "status": "rejected"}

    def set_tier(self, name: str, tier: str, *, actor: str = "") -> dict:
        """在 ``required`` ↔ ``available`` 之间搬;``validator`` 只能手写 SKILL.md。

        曾经这里分两个方向校验(升档跑 ``_validate_validator_spec``、降档查
        四字段残留),但两个方向**恒 400**,而且报的是指错地方的话:
        ``_load_meta`` 走 ``read_skill``,而后者按**当前** tier 投影 validator
        四字段 —— 升档时 entry 上根本没有 ``checks``,校验只读到 ``[]``,于是
        报 "checks is required"(让人去补一个这条路径递不进去的字段);
        降档时四字段又在,报 "mode is only valid for tier=validator"(让人去删
        一个删不掉的东西)。真相只有一个:tier 与 mode/severity/targets/checks
        必须在同一个文件里一起写。与其留一个永远 400、报错还误导的按钮,
        不如一次说清楚 —— 拒绝的那条不变量没变,变的是它说的人话。
        """
        if tier not in _TIERS:
            raise ValueError(f"tier must be one of {_TIERS}")
        entry = self._load_meta(name)
        if entry is None:
            raise KeyError(f"skill not found: {name}")
        if "validator" in (tier, entry.get("tier")):
            raise ValueError(
                "tier=validator is set by hand in SKILL.md: its mode / severity / "
                "targets / checks fields are projected onto the entry by the "
                "current tier, so neither direction of this switch can validate "
                f"them. Edit {self.skill_path(name)} and set tier plus those four "
                "fields together in the frontmatter."
            )
        entry = self._rewrite_field(name, {"tier": tier})
        self._commit("tier", name, entry, actor=actor)
        return entry

    def _rewrite_field(self, name: str, updates: dict,
                       body: str | None = None) -> dict:
        """整篇重 dump frontmatter(可选换正文),**修订号 +1**。

        版本语义是修订计数:任何一次内容变更(confirm / tier / 正文重写 /
        rollback)都往前走一格。取**规范化后的当前值 + 1** —— 遗留文件
        (无该字段或非整数,读作 1)第一次重写后成为 2,而不是被打回 0;
        ``version`` 与 ``updated_at`` 和点名键在同一次写盘里落定,不存在
        "内容改了、版本没动"的中间态。
        """
        path = self.skill_path(name)
        parsed = self._parse_skill(path.read_text(encoding="utf-8"))
        if "meta" not in parsed:
            raise ValueError(f"skill not found: {name}")
        meta = parsed["meta"]
        meta.update(updates)
        meta["version"] = self._normalized_version(meta) + 1
        from datetime import datetime, timezone

        meta["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        text = body if body is not None else parsed["body"]
        path.write_text(
            f"---\n{frontmatter}\n---\n\n{text}\n", encoding="utf-8",
        )
        return self.read_skill(name)

    def _rewrite_status(self, name: str, status: str) -> dict:
        return self._rewrite_field(name, {"status": status})

    def update_body(self, name: str, body: str, *, actor: str = "") -> dict:
        """整篇替换正文(frontmatter 不动,修订号 +1)。

        与 ``confirm`` 同一顺序:先扫、后写 —— 落盘是最后一个会抛的步骤,
        扫描命中只报不改(写它的人此刻在场,是唯一能判断"这句话是不是有意
        写的"的一方)。
        """
        text = (body or "").strip()
        if not text:
            raise ValueError("body is required")
        entry = self._load_meta(name)
        if entry is None:
            raise KeyError(f"skill not found: {name}")
        hits = self._scan_entry({**entry, "body": text})
        updated = self._rewrite_field(name, {}, body=text)
        updated["injection_hits"] = hits
        self._commit("body", name, updated, actor=actor)
        return updated

    # ── Git audit: history / rollback ─────────────────────

    def history(self, name: str, limit: int = 50) -> list[dict]:
        """该 skill 的提交历史(git log,按时间倒序);非 git 环境 → []。"""
        if not self.skill_path(name).exists():
            raise KeyError(f"skill not found: {name}")
        if self.git is None:
            return []
        return self.git.history_files([self.skill_dir(name)], limit=limit)

    def rollback(self, name: str, sha: str, *, actor: str = "",
                 message: str = "") -> dict:
        """把该 skill 回滚到 ``sha`` 时的内容(新建提交,修订号继续前进)。

        回滚 = **一次新的修订**:恢复目标版本的文件(正文 / 触发条件 / 档位,
        ``SKILL.<lang>.md`` 覆盖文件一并回到那个版本)之后,把 ``version``
        抬到 ``max(回滚前, 恢复后) + 1`` —— 审计史里版本号单调,前端看到的
        永远是"又改了一版"而不是时间倒流。回滚本身又是一条 commit,历史
        不改写。
        """
        if not self.skill_path(name).exists():
            raise KeyError(f"skill not found: {name}")
        if self.git is None:
            return {"rolled_back": False, "reason": "disabled"}
        current = self.read_skill(name) or {}
        base = self._normalized_version(current)

        def _bump(directory: Path) -> None:
            p = directory / "SKILL.md"
            parsed = self._parse_skill(p.read_text(encoding="utf-8"))
            if "meta" not in parsed:
                raise ValueError(f"skill not found: {name}")
            meta = parsed["meta"]
            meta["version"] = max(self._normalized_version(meta), base) + 1
            from datetime import datetime, timezone

            meta["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            frontmatter = yaml.safe_dump(
                meta, allow_unicode=True, sort_keys=False).strip()
            p.write_text(f"---\n{frontmatter}\n---\n\n{parsed['body']}\n",
                         encoding="utf-8")

        result = self.git.rollback_tree(
            self.skill_dir(name), sha,
            message or f"skills: rollback {name} to {sha[:8]}",
            transform=_bump,
            trailers={"Generator": "skills.rollback", "Approved-by": actor},
        )
        if result.get("rolled_back"):
            result["entry"] = self.read_skill(name)
        return result

    # ── Git versioning helper ─────────────────────────────

    def _skill_files(self, name: str) -> list[Path]:
        """该 skill 目录下参与版本化的文件:SKILL.md + 各语言覆盖文件。"""
        d = self.skill_dir(name)
        files = [d / "SKILL.md", *sorted(d.glob("SKILL.*.md"))]
        return [p for p in files if p.exists()]

    def _commit(self, action: str, name: str, entry: dict | None, *,
                deleted: bool = False, actor: str = "") -> dict:
        """写路径自动版本化:一次治理变更 = 一条 commit(尽力而为)。

        守卫与 KB 写路径同源(同一个 ``GitVersioning``):只暂存点名文件
        (删除走目录作用域)、没仓库/没改动/提交失败一律降级 no-op ——
        **版本化绝不阻断它要记录的那次写入**。
        """
        if self.git is None:
            return {"committed": False, "reason": "disabled"}
        version = (entry or {}).get("version")
        message = f"skills: {action} {name}" + (f" v{version}" if version else "")
        trailers = {"Generator": f"skills.{action}", "Approved-by": actor}
        if deleted:
            return self.git.commit_dir_removed(
                self.skill_dir(name), message, trailers=trailers)
        return self.git.commit_files(
            self._skill_files(name), message, trailers=trailers)

    # ── Body / rendering ──────────────────────────────────

    def get_body(self, name: str, lang: str) -> str | None:
        """Body text for a skill, preferring the per-language override file."""
        d = self.skill_dir(name)
        if not (d / "SKILL.md").exists():
            return None
        entry = self.read_skill(name)
        if entry is None or "error" in entry:
            return None
        override = d / f"SKILL.{lang}.md"
        if override.exists():
            return override.read_text(encoding="utf-8").strip()
        return entry["body"]

    def load_skill_content(
        self,
        name: str,
        lang: str,
        *,
        node: str = "gen_sql",
        skill_ctx: dict | None = None,
    ) -> str:
        """Full body for the on-demand ``load_skill`` tool. Errors are returned
        as text so the agent loop sees them without raising.

        **触发器与其余三条投递路同一个判定**(``_trigger_mismatch``)。点名不是
        提权:没被广告出来的名字也一样取不到,已确认但 ctx 不匹配的技能同样拿
        不到正文 —— 否则 ``role`` / ``lang`` 之类的声明就只约束了广告,按名直取
        即可绕过。

        ``skill_ctx`` 由装配处递入(``state.skill_ctx()``);缺省时除 ``lang``
        外的维度都不匹配 —— 收窄声明保守不命中,而不是放行。

        总开关停用时按"取不到"回答(报错文本而不是抛):广告已经不出,按名
        直取同样必须停 —— 否则一条被记住的名字就是绕过管理员开关的后门。
        """
        if not self._org_enabled():
            return (
                "Org skills are disabled by the administrator "
                "(agent.extensions.org_extensions_enabled=false) — skill "
                "bodies are not delivered while the org extension surface "
                "is switched off."
            )
        entry = self.read_skill(name)
        if entry is None:
            return f"Skill not found: {name}"
        if entry.get("status") != "confirmed":
            return (
                f"Skill '{name}' is not confirmed yet — an admin must confirm "
                "it before it can be used."
            )
        if entry.get("tier") == "validator":
            # 判据不是指令。把它当指令投给模型,等于让被检查者自己念检查
            # 标准 —— 而且模型可能"顺手"去执行它。
            return (
                f"Skill '{name}' is a validator: it checks results, it is not "
                "instructions to follow. Its criteria are applied "
                "automatically after execution."
            )
        ctx = {**(skill_ctx or {}), "lang": lang}
        dim = self._trigger_mismatch(entry.get("triggers") or {}, node, ctx)
        if dim is not None:
            return (
                f"Skill '{name}' is not available in this context: its "
                f"'{dim}' trigger does not match."
            )
        body = self.get_body(name, lang)
        if not body:
            return f"Skill '{name}' has no content."
        # 与 required 档**同一策略**:围栏 + 标注来源,不做内容净化。
        # 此前这条路把正文当数据交给隔离核,整值作废 —— 同一份内容两档两种
        # 相反处置,没有安全依据(而且 required 档本来就是原文进 system prompt)。
        return fence_org_skill(name, f"# {name}\n\n{body}")

    def _applies_to(self, entry: dict, node: str) -> bool:
        """Node-trigger projection: does ``entry`` apply to ``node`` at all?

        The node half of a match, without the other trigger dimensions. Used
        by ``has_available_for`` as the **superset** gate for tool
        registration — see that method for why it must not be ctx-aware.
        """
        triggers = entry.get("triggers") or {}
        target = _declared_node(triggers)
        return target is None or target == node

    @staticmethod
    def _trigger_mismatch(
        triggers: dict, node: str, ctx: dict, *, skip_node: bool = False,
    ) -> str | None:
        """第一个不匹配的触发维度名;全匹配返回 ``None``。

        四条投递路(required 注入 / available 广告 / validator 运行 / on-demand
        取用)共用这一个判定 —— 各写一遍必然漂移,而漂移的表现恰好是本模块最
        要防的那类事故:配了却静默不生效(或反过来,绕过了声明的收窄)。

        ``skip_node=True`` 留给 validator:它的宿主由 ``targets`` 决定,
        ``triggers.node`` 在那里是**标记**而不是筛子(见 ``validators_for``)。
        """
        declared = _declared_node(triggers)
        if not skip_node and declared not in (None, node):
            return "node"
        for k, v in triggers.items():
            if k == "node":
                continue
            if not match_trigger(k, v, ctx.get(k)):
                return k
        return None

    def _match_org(self, node: str, **ctx: object) -> list[dict]:
        """Confirmed org skills matching the node (trigger ctx 逐字段匹配)。"""
        return [
            entry
            for entry in self.list_org(confirmed_only=True)
            if self._trigger_mismatch(entry.get("triggers") or {}, node, ctx)
            is None
        ]

    def validators_for(self, node: str, **ctx: object) -> list[dict]:
        """Confirmed ``validator``-tier org skills applying to ``node``.

        与 ``available_descriptions`` 对称的一档:两者都是一条**投递路**,
        差别在投递给谁 —— available 投给模型(让它加载),validator 投给
        引擎(让它运行)。确认门对两者同样有效(``list_org(confirmed_only)``)。

        ``triggers.node`` 在这里**不是筛子**:validator 的宿主由 ``targets``
        决定(本期只有 ``result`` → 宿主恒为 ``VALIDATOR_HOST``),一份写了别的
        node 的文件永远不会运行。把它丢在这里,从任何外部面(附注、
        ``validator_hits``、``list_org``)看都和"没写"一模一样 —— 与
        ``align_schema`` 同一类事故。改为**标记**:``host_mismatch`` 带上声明的
        那个 node,由 ``run_validators`` 落一条 ``verdict: None`` 的可观测记录。

        除 ``node`` 外的触发维度(``role`` / ``lang`` / ``complexity`` /
        ``datasource`` / ``intent``)照旧参与筛选;``node`` 省略照旧命中。

        总开关停用 → 空列表:断言是组织扩展的消费面之一,停用即一条都不跑。
        """
        if not self._org_enabled():
            return []
        out: list[dict] = []
        for entry in self.list_org(confirmed_only=True):
            if entry.get("tier") != "validator":
                continue
            triggers = entry.get("triggers") or {}
            if self._trigger_mismatch(
                triggers, node, ctx, skip_node=True,
            ) is not None:
                continue
            # 副本:调用方要往条目上挂标记,``list_org`` 的条目不许被就地改。
            e = dict(entry)
            declared = _declared_node(triggers)
            if declared is not None and declared != node:
                e["host_mismatch"] = declared
            out.append(e)
        return out

    def render_skills(self, node: str, lang: str = "en", **ctx: object) -> str:
        """Merged methodology blocks for a node's system prompt.

        Code skills are always injected in full (they are de-facto required);
        confirmed org skills at ``tier: required`` are injected in full;
        ``available`` org skills are NOT injected here (single-shot nodes have
        no tool loop) — they are advertised in ``available_skills_block``.

        总开关停用 → org 层短路(**code skills 照常**:它们随代码走,不属于
        组织扩展这一层)。开关每问现读,无需重启。
        """
        blocks = []
        code = _code_render(node, lang=lang, **ctx)
        if code:
            blocks.append(code)
        if not self._org_enabled():
            return "\n\n".join(blocks)
        # lang 被 render_skills 的具名形参吃掉了,不在这里补回,它的 ctx 值恒为
        # None —— 「只对中文问题挂」这类 trigger 会静默失效。
        for entry in self._match_org(node, lang=lang, **ctx):
            if entry.get("tier") != "required":
                continue
            body = self.get_body(entry["name"], lang)
            if body:
                # org 正文围栏 + 标注来源(不改内容)。代码内置技能不经此处 ——
                # 它们随代码走,是可信模板,没有"哪个管理员确认的"这回事。
                blocks.append(fence_org_skill(entry["name"], body))
        return "\n\n".join(blocks)

    # ── On-demand loading (available tier, agentic gen_sql) ──

    def available_descriptions(self, node: str, **ctx: object) -> list[dict]:
        """Confirmed ``available``-tier org skills applying to ``node``."""
        if not self._org_enabled():
            return []
        return [e for e in self._match_org(node, **ctx)
                if e.get("tier") == "available"]

    def has_available_for(self, node: str) -> bool:
        """Any confirmed ``available``-tier skill that *could* apply to ``node``.

        Node-trigger matching only, deliberately a **superset** of
        ``available_skills_block(node, **ctx)``. The one caller registers the
        ``load_skill`` tool, and it has no ctx to match with: matching the full
        ctx here would make the registration gate narrower than the
        advertisement, so a ``{node, lang: zh}`` skill would be named in the
        prompt while the tool it names was never registered.

        总开关停用 → False:``load_skill`` 工具根本不注册(广告与工具一起
        消失,不留一个点名字才报错的空工具)。
        """
        if not self._org_enabled():
            return False
        return any(
            self._applies_to(e, node)
            for e in self.list_org(confirmed_only=True)
            if e.get("tier") == "available"
        )

    def available_skills_block(self, node: str, lang: str = "en", **ctx: object) -> str:
        """``<available_skills>`` advertisement block for the system prompt."""
        entries = self.available_descriptions(node, lang=lang, **ctx)
        if not entries:
            return ""
        lines = ["<available_skills>",
                 "The following skills are available on demand — call "
                 "load_skill(skill_name=\"...\") to read the full instructions "
                 "only when they apply to the task:"]
        for e in entries:
            desc = (e.get("description") or "").strip()
            lines.append(f'- load_skill(skill_name="{e["name"]}"): {desc}')
        lines.append("</available_skills>")
        return "\n".join(lines)

    # ── Misc ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self.root.exists() and any(
            d.is_dir() and (d / "SKILL.md").exists() for d in self.root.iterdir()
        )
