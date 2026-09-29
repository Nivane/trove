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
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from trove.llm.injection import scan_injection
from trove.prompts.skills import fence_org_skill
from trove.prompts.skills import render_skills as _code_render

# name = lowercase letters/digits + hyphens; also a safe directory name.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TIERS = ("required", "available")
_STATUSES = ("pending", "confirmed", "rejected")

FRONTMATTER_FIELDS = (
    "name", "description", "triggers", "tier", "status",
    "source", "lang", "created_at", "updated_at",
)


def _match_one(cond: object, value: object) -> bool:
    """One trigger field: scalar equality, or list membership (OR)."""
    if isinstance(cond, list):
        return value in cond
    return value == cond


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
        return {
            "name": meta.get("name", name),
            "description": meta.get("description", ""),
            "triggers": meta.get("triggers") or {},
            "tier": meta.get("tier", "available"),
            "status": meta.get("status", "pending"),
            "source": meta.get("source", "admin"),
            "lang": meta.get("lang", "en"),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "body": parsed["body"],
        }

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
        """注入形状的模式名列表(空 = 干净)。扫的是**投递面**:描述 + 正文。

        两档都扫 —— required 档投正文,available 档只投描述(``<available_skills>``
        广告块);只扫正文会留下"同一个缺口换个 tier 就绕过去"的路。
        """
        return scan_injection(f"{entry.get('description', '')}\n{entry.get('body', '')}")

    def scan_skill(self, name: str) -> list[str]:
        """按名字扫一份 skill(不存在 → 空)。"""
        entry = self._load_meta(name)
        return self._scan_entry(entry) if entry else []

    @classmethod
    def _with_scan(cls, entry: dict) -> dict:
        """写入口的返回值统一挂上扫描结果 —— **只报不改**。

        两个写入口(``create`` / ``confirm``)都挂:草稿落盘时就报一次,管理员
        在**决定之前**看见;确认时再报一次,兜住"草稿到确认之间被改过"。
        正文是指令性文本,写它的人此刻在场 —— 是唯一能判断"这句是有意写的还是
        被灌进来的"的一方。把扫描放运行期只会静默毁内容(实测:一句话让整份
        方法论变成 ``[data: content isolated]``);放在这里则是一次可读的提示,
        看完确认,登记即豁免。
        """
        entry["injection_hits"] = cls._scan_entry(entry)
        return entry

    def confirm(self, name: str) -> dict:
        """Admin confirmation: pending draft → confirmed (enters retrieval)."""
        if self._load_meta(name) is None:
            raise KeyError(f"skill not found: {name}")
        return self._with_scan(self._rewrite_status(name, "confirmed"))

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
        if self._load_meta(name) is None:
            raise KeyError(f"skill not found: {name}")
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

    def load_skill_content(self, name: str, lang: str) -> str:
        """Full body for the on-demand ``load_skill`` tool. Errors are returned
        as text so the agent loop sees them without raising."""
        entry = self.read_skill(name)
        if entry is None:
            return f"Skill not found: {name}"
        if entry.get("status") != "confirmed":
            return (
                f"Skill '{name}' is not confirmed yet — an admin must confirm "
                "it before it can be used."
            )
        body = self.get_body(name, lang)
        if not body:
            return f"Skill '{name}' has no content."
        # 与 required 档**同一策略**:围栏 + 标注来源,不做内容净化。
        # 此前这条路把正文当数据交给隔离核,整值作废 —— 同一份内容两档两种
        # 相反处置,没有安全依据(而且 required 档本来就是原文进 system prompt)。
        return fence_org_skill(name, f"# {name}\n\n{body}")

    def _applies_to(self, entry: dict, node: str) -> bool:
        triggers = entry.get("triggers") or {}
        target = triggers.get("node")
        return target is None or target == node

    def _match_org(self, node: str, **ctx: object) -> list[dict]:
        """Confirmed org skills matching the node (trigger ctx equality)."""
        out = []
        for entry in self.list_org(confirmed_only=True):
            triggers = entry.get("triggers") or {}
            if triggers.get("node") not in (None, node):
                continue
            if not all(
                _match_one(v, ctx.get(k)) for k, v in triggers.items() if k != "node"
            ):
                continue
            out.append(entry)
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
        for entry in self._match_org(node, **ctx):
            if entry.get("tier") != "required":
                continue
            body = self.get_body(entry["name"], lang)
            if body:
                # org 正文围栏 + 标注来源(不改内容)。代码内置技能不经此处 ——
                # 它们随代码走,是可信模板,没有"哪个管理员确认的"这回事。
                blocks.append(fence_org_skill(entry["name"], body))
        return "\n\n".join(blocks)

    # ── On-demand loading (available tier, agentic gen_sql) ──

    def available_descriptions(self, node: str) -> list[dict]:
        """Confirmed ``available``-tier org skills applying to ``node``."""
        out = []
        for entry in self._match_org(node):
            if entry.get("tier") == "available":
                out.append(entry)
        return out

    def has_available_for(self, node: str) -> bool:
        return bool(self.available_descriptions(node))

    def available_skills_block(self, node: str, lang: str = "en") -> str:
        """``<available_skills>`` advertisement block for the system prompt."""
        entries = self.available_descriptions(node)
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
