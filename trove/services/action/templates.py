"""ActionTemplateService — org response templates as admin-managed assets.

Shape mirrors ``services/skills/service.py`` deliberately (same author flow,
same gate): a template is created as a **pending** draft under
``.trove/actions/<name>/action.yml`` and cannot be referenced by a decision
rule until an admin confirms it. ``decision/rules.py::lint_rule_assets`` is
the other half of that gate — it refuses to save a rule pointing at a template
that is missing or unconfirmed.

A template is not code and not a prompt: it is *data*. It names a channel
(never a bare URL — channels, with their URLs and secrets, live in the
deployment config, so a template can be reviewed without carrying credentials)
and a JSON payload with closed-set ``{{variable}}`` holes. Creation renders
the template once against sample values, so a typo'd variable or a JSON
mistake is a 400 to the admin who made it — not a surprise on the night the
rule first fires.

The same **scan-before-write, report-don't-block** discipline as skills: the
payload's text and the label an approver reads are scanned for
injection-shaped content at draft and at confirm, and the hits are returned to
the human deciding. Blocking would destroy content the author meant; staying
silent would let them confirm something they never read.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trove.llm.injection import scan_injection
from trove.services.action.models import (
    ACTION_TYPES,
    APPROVALS_REQUIRED_V1,
    RISKS,
)
from trove.services.action.template_render import validate_template

#: Same name rule as skills: lowercase letters/digits + hyphens, which is also
#: a safe directory name (no traversal, no case-fold collisions on macOS).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: ``target`` is a closed mapping: ``channel`` (required, resolved through the
#: deployment config) and ``resource`` (optional, free-form label such as a
#: room name — informational for the human, never used to route). Refusing
#: unknown keys keeps a typo like ``chanel:`` from reading as "no channel".
_TARGET_FIELDS = ("channel", "resource")

FRONTMATTER_FIELDS = (
    "name", "title", "description", "status", "action_type", "target",
    "risk", "approvals_required", "payload_template", "source",
    "created_at", "updated_at",
)


class ActionTemplateService:
    """Manage org action templates (the pending → confirmed gate)."""

    def __init__(self, root: Path | None = None, *,
                 max_payload_bytes: int = 8192):
        self.root = (Path(root) if root is not None
                     else Path.cwd() / ".trove" / "actions")
        self.max_payload_bytes = max(0, int(max_payload_bytes or 0))

    # ── paths ────────────────────────────────────────────

    def template_dir(self, name: str) -> Path:
        return self.root / name

    def template_path(self, name: str) -> Path:
        return self.template_dir(name) / "action.yml"

    # ── reading ──────────────────────────────────────────

    def read_template(self, name: str) -> dict | None:
        """One template as a plain dict; ``None`` when the file is absent.

        An invalid file surfaces with an ``error`` key (and is then invisible
        to ``names()`` / ``confirmed_names()``) — same as ``read_skill``:
        a file the admin hand-edited into a broken state must be *visibly*
        broken, not silently absent from the list.
        """
        path = self.template_path(name)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            return {"name": name, "error": f"invalid YAML: {e}"}
        if not isinstance(raw, dict):
            return {"name": name, "error": "action.yml must be a mapping"}
        entry: dict[str, Any] = {
            "name": str(raw.get("name") or name),
            "title": str(raw.get("title") or ""),
            "description": str(raw.get("description") or ""),
            "status": str(raw.get("status") or "pending"),
            "action_type": str(raw.get("action_type") or "notify"),
            "target": dict(raw.get("target") or {}) if isinstance(
                raw.get("target") or {}, dict) else {},
            "risk": str(raw.get("risk") or "low"),
            "approvals_required": raw.get("approvals_required",
                                          APPROVALS_REQUIRED_V1),
            "payload_template": str(raw.get("payload_template") or ""),
            "source": str(raw.get("source") or "admin"),
            "created_at": str(raw.get("created_at") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
            "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        }
        problems = self._validate_entry(entry, for_create=False)
        if problems:
            entry["error"] = "; ".join(problems)
        return entry

    def list_templates(self, confirmed_only: bool = False) -> list[dict]:
        """All valid templates, sorted by name; broken files surface with error."""
        if not self.root.exists():
            return []
        out: list[dict] = []
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not (d / "action.yml").exists():
                continue
            entry = self.read_template(d.name)
            if entry is None:
                continue
            if confirmed_only and entry.get("status") != "confirmed":
                continue
            out.append(entry)
        return out

    def _load_meta(self, name: str) -> dict | None:
        entry = self.read_template(name)
        if entry is None:
            return None
        return entry

    def names(self) -> set[str]:
        """Names usable as lint registries — only files that parse *and* validate.

        A broken file is not "declared": pointing a rule at it must read as a
        dangling reference (``lint_rule_assets`` reports it), not as a
        reference to something that will explode at render time.
        """
        return {e["name"] for e in self.list_templates() if "error" not in e}

    def confirmed_names(self) -> set[str]:
        return {e["name"] for e in self.list_templates(confirmed_only=True)
                if "error" not in e}

    def digest(self, name: str) -> str:
        entry = self.read_template(name)
        return str(entry.get("digest") or "") if entry else ""

    def get(self, name: str):
        """``ActionTemplate`` for a template that is valid, or ``None``."""
        entry = self.read_template(name)
        if entry is None or "error" in entry:
            return None
        from trove.services.action.models import ActionTemplate

        return ActionTemplate(
            name=entry["name"], title=entry["title"],
            description=entry["description"], status=entry["status"],
            action_type=entry["action_type"], target=dict(entry["target"]),
            risk=entry["risk"],
            approvals_required=int(entry["approvals_required"] or 1),
            payload_template=entry["payload_template"], source=entry["source"],
            created_at=entry["created_at"], updated_at=entry["updated_at"],
            digest=entry["digest"],
        )

    # ── validation ───────────────────────────────────────

    def _validate_entry(self, entry: dict, *, for_create: bool) -> list[str]:
        """Everything that must hold for the file to be usable, as messages.

        ``for_create`` only changes how the payload is treated: a file already
        on disk may predate a config change, so its payload is checked for
        *shape* (renderable JSON) rather than against the current byte limit —
        a limit that would make an existing template vanish from the list.
        """
        problems: list[str] = []
        name = str(entry.get("name") or "")
        if not _NAME_RE.match(name):
            problems.append(
                f"invalid template name {name!r}: must match ^[a-z0-9][a-z0-9-]*$")
        if not str(entry.get("title") or "").strip():
            problems.append("title is required")
        if entry.get("status") not in ("pending", "confirmed"):
            problems.append(
                f"status must be 'pending' or 'confirmed' (got {entry.get('status')!r})")
        if entry.get("action_type") not in ACTION_TYPES:
            problems.append(
                f"action_type must be one of {', '.join(ACTION_TYPES)} "
                f"(got {entry.get('action_type')!r})")
        if entry.get("risk") not in RISKS:
            problems.append(
                f"risk must be one of {', '.join(RISKS)} (got {entry.get('risk')!r})")
        try:
            approvals = int(entry.get("approvals_required")
                            or APPROVALS_REQUIRED_V1)
        except (TypeError, ValueError):
            approvals = -1
        if approvals != APPROVALS_REQUIRED_V1:
            # Refused rather than clamped: a "2" that behaves like "1" is a
            # single-approver gate wearing a two-approver label.
            problems.append(
                f"approvals_required must be {APPROVALS_REQUIRED_V1} in this "
                f"version (got {entry.get('approvals_required')!r}) — "
                "multi-approver flow is not implemented")
        target = entry.get("target") or {}
        if not isinstance(target, dict):
            problems.append("target must be a mapping with a 'channel'")
        else:
            unknown = sorted(set(target) - set(_TARGET_FIELDS))
            if unknown:
                problems.append(
                    f"target has unknown field(s): {', '.join(unknown)} "
                    f"(allowed: {', '.join(_TARGET_FIELDS)})")
            if not str(target.get("channel") or "").strip():
                problems.append(
                    "target.channel is required — templates name a configured "
                    "channel, never a bare URL")
        body = str(entry.get("payload_template") or "")
        if not body.strip():
            problems.append("payload_template is required")
        else:
            limit = self.max_payload_bytes if for_create else 0
            problems.extend(validate_template(body, max_bytes=limit))
        return problems

    # ── create / confirm / reject ────────────────────────

    def create(self, entry: dict) -> dict:
        """Create a template as a *pending* draft. Returns the saved entry."""
        name = str(entry.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError(
                "invalid template name: must match ^[a-z0-9][a-z0-9-]*$")
        if self.template_path(name).exists():
            raise ValueError(f"template already exists: {name}")

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target = entry.get("target") or {}
        doc = {
            "name": name,
            "title": str(entry.get("title") or "").strip(),
            "description": str(entry.get("description") or "").strip(),
            "status": "pending",
            "action_type": str(entry.get("action_type") or "notify").strip().lower(),
            # 原样落盘再交给校验:拼错的键(``chanel:``)必须在校验里被
            # **点名**,而不是被这里悄悄丢掉、再报一句"channel 缺失"。
            "target": (
                {str(k): (v.strip() if isinstance(v, str) else v)
                 for k, v in target.items()}
                if isinstance(target, dict) else target
            ),
            "risk": str(entry.get("risk") or "low").strip().lower(),
            # 原样落盘再交给校验:请求里写 2 必须**响亮被拒**,不能悄悄
            # 压成 1 —— 一个"2"跑出"1"的行为是最坏的两头不靠。
            "approvals_required": entry.get(
                "approvals_required", APPROVALS_REQUIRED_V1),
            "payload_template": str(entry.get("payload_template") or "").strip(),
            "source": str(entry.get("source") or "admin"),
            "created_at": now,
            "updated_at": now,
        }
        problems = self._validate_entry(doc, for_create=True)
        if problems:
            raise ValueError("; ".join(problems))

        self.template_dir(name).mkdir(parents=True, exist_ok=True)
        self.template_path(name).write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return self._with_scan(self.read_template(name))

    def confirm(self, name: str) -> dict:
        """Admin confirmation: pending draft → confirmed (rule-referenceable).

        Scan first, write last: the write must be the final step that can
        raise, or a failure after it would show a 500 for a flip that already
        happened (same discipline as ``SkillService.confirm``).
        """
        entry = self._load_meta(name)
        if entry is None:
            raise KeyError(f"template not found: {name}")
        if entry.get("error"):
            raise ValueError(
                f"template {name!r} is invalid and cannot be confirmed: "
                f"{entry['error']}")
        hits = self._scan_entry(entry)
        entry = self._rewrite_field(name, {"status": "confirmed"})
        entry["injection_hits"] = hits
        return entry

    def reject(self, name: str) -> dict:
        """Admin rejection: delete the draft directory."""
        d = self.template_dir(name)
        if not (d / "action.yml").exists():
            raise KeyError(f"template not found: {name}")
        import shutil

        shutil.rmtree(d)
        return {"name": name, "status": "rejected"}

    def _rewrite_field(self, name: str, updates: dict) -> dict:
        path = self.template_path(name)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"template not found: {name}")
        raw.update(updates)
        raw["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return self.read_template(name)

    # ── injection scan (report, never block) ─────────────

    @staticmethod
    def _scan_entry(entry: dict) -> list[str]:
        """Scan the **delivery surface**: what goes out, plus what the approver reads.

        ``payload_template`` is the message body; ``title``/``description`` are
        what a human sees when deciding whether to reference or approve it —
        both are places where prose can carry an instruction, and both are
        scanned for the same reason skills scan their body and description.
        """
        parts = [
            str(entry.get("title") or ""),
            str(entry.get("description") or ""),
            str(entry.get("payload_template") or ""),
        ]
        return scan_injection("\n".join(parts))

    def scan_template(self, name: str) -> list[str]:
        entry = self._load_meta(name)
        return self._scan_entry(entry) if entry else []

    @classmethod
    def _with_scan(cls, entry: dict | None) -> dict:
        if entry is None:
            return {}
        entry["injection_hits"] = cls._scan_entry(entry)
        return entry
