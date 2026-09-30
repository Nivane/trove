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

All four delivery paths — required injection, available advertisement,
validator execution and on-demand ``load_skill`` — share **one** trigger
predicate (``_trigger_mismatch``). A declared ``role`` / ``lang`` / … narrows
every path, so naming a skill directly cannot bypass it.
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
    "source", "lang", "created_at", "updated_at",
)


class SkillService:
    """Manage + render org skills, merged with the built-in code skills."""

    def __init__(self, root: Path | None = None, llm: Any = None):
        self.root = Path(root) if root is not None else Path.cwd() / ".trove" / "skills"
        self.llm = llm

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
        declared = (entry.get("triggers") or {}).get("node")
        if declared is not None and declared != VALIDATOR_HOST:
            raise ValueError(
                f"triggers.node must be {VALIDATOR_HOST!r} (or omitted) for tier=validator: "
                f"result assertions only run at the {VALIDATOR_HOST} node"
            )
        return {"mode": mode, "severity": severity, "targets": targets,
                "checks": checks}

    def create(self, entry: dict) -> dict:
        """Create an org skill as a *pending* draft. Returns the saved entry."""
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
            "created_at": now,
            "updated_at": now,
        }
        frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        self.skill_dir(name).mkdir(parents=True, exist_ok=True)
        self.skill_path(name).write_text(
            f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8",
        )
        return self._with_scan(self.read_skill(name))

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

    def confirm(self, name: str) -> dict:
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
        return entry

    def reject(self, name: str) -> dict:
        """Admin rejection: delete the draft directory."""
        d = self.skill_dir(name)
        if not (d / "SKILL.md").exists():
            raise KeyError(f"skill not found: {name}")
        import shutil

        shutil.rmtree(d)
        return {"name": name, "status": "rejected"}

    def set_tier(self, name: str, tier: str) -> dict:
        if tier not in _TIERS:
            raise ValueError(f"tier must be one of {_TIERS}")
        entry = self._load_meta(name)
        if entry is None:
            raise KeyError(f"skill not found: {name}")
        if tier == "validator":
            # set_tier 是一条独立的写入路径:不校验就能把一份没有 checks 的
            # skill 变成 validator —— 它永远不会生效,而且看着像生效了。
            self._validate_validator_spec(entry)
        else:
            # 反向同理:validator 的四字段在非 validator 档上没有意义,而 tier
            # 一旦不是 validator,render_skills 就会把正文当**指令**投递 ——
            # 判据被检查者念出,检查就没了意义。create() 已立了这条,set_tier
            # 是同一个不变量的另一半。
            for f in VALIDATOR_FIELDS:
                if entry.get(f) is not None:
                    raise ValueError(f"{f} is only valid for tier=validator")
        return self._rewrite_field(name, {"tier": tier})

    def _rewrite_field(self, name: str, updates: dict) -> dict:
        path = self.skill_path(name)
        parsed = self._parse_skill(path.read_text(encoding="utf-8"))
        if "meta" not in parsed:
            raise ValueError(f"skill not found: {name}")
        meta = parsed["meta"]
        meta.update(updates)
        from datetime import datetime, timezone

        meta["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        path.write_text(
            f"---\n{frontmatter}\n---\n\n{parsed['body']}\n", encoding="utf-8",
        )
        return self.read_skill(name)

    def _rewrite_status(self, name: str, status: str) -> dict:
        return self._rewrite_field(name, {"status": status})

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
        """
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
        target = triggers.get("node")
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
        declared = triggers.get("node")
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
        """
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
            declared = triggers.get("node")
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
        """
        blocks = []
        code = _code_render(node, lang=lang, **ctx)
        if code:
            blocks.append(code)
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
        """
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
