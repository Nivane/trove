"""Result shapes for ``trove validate`` — the pre-install dry run.

Pure data: an :class:`Issue` is one finding, a :class:`ValidateReport` is the
whole run. Rendering lives on the report (both the CLI and the REPL slash
command print the same text), JSON lives in ``to_dict`` — the same dict is
what ``--json`` emits, so a CI consumer and a human see the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Severity vocabulary. ``error`` blocks (exit 1); ``warning`` is reported and
#: only blocks under ``--strict``. The split mirrors the repo's blocking/
#: advisory convention (decision rules, validator skills): something that
#: would never take effect is an error, something merely under-specified is
#: not — the author may still be mid-draft.
SEVERITIES = ("error", "warning")


@dataclass(frozen=True)
class Issue:
    """One finding. ``check`` is the stable id CI can filter on."""

    check: str
    severity: str
    message: str
    target: str = ""       # rule id / skill name / file name
    datasource: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "target": self.target,
            "datasource": self.datasource,
        }


@dataclass
class MountPreview:
    """Where one extension asset will actually be injected at runtime.

    ``mounts`` are the injection points *as the runtime renders them*
    (rendered from the same read functions the pipeline calls, never from a
    parallel re-derivation); ``notes`` carry the gate state — pending /
    rejected / disabled assets are still previewed, because "where would this
    go once confirmed" is exactly the question the admin is asking.
    """

    kind: str              # "skill" | "rule"
    name: str
    tier: str = ""
    status: str = ""
    datasource: str = ""
    mounts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "tier": self.tier,
            "status": self.status,
            "datasource": self.datasource,
            "mounts": list(self.mounts),
            "notes": list(self.notes),
        }


@dataclass
class ValidateReport:
    project_root: str = ""
    datasources: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    mounts: list[MountPreview] = field(default_factory=list)
    #: 信封(E1):每条资产的编译产物 —— capabilities 是**推导物**,
    #: unresolved 已同步落成 issues(响亮),这里保留结构供 --json 消费。
    #: 序列化后的 dict(ExtensionEnvelope.to_dict),additive:旧消费方零改。
    envelopes: list[dict[str, Any]] = field(default_factory=list)
    live: bool = False

    # ── verdict ───────────────────────────────────────────

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def exit_code(self, *, strict: bool = False) -> int:
        """0 = clean; 1 = hard errors (or, under ``--strict``, any warning)."""
        if self.errors:
            return 1
        if strict and self.warnings:
            return 1
        return 0

    # ── output ────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "project_root": self.project_root,
            "datasources": list(self.datasources),
            "counts": dict(self.counts),
            "live": self.live,
            "mounts": [m.to_dict() for m in self.mounts],
            "envelopes": list(self.envelopes),
            "issues": [i.to_dict() for i in self.issues],
        }

    def render(self) -> str:
        lines = [
            "trove validate — 扩展面干跑校验（零 LLM，静态检查默认不联网）",
            f"项目根: {self.project_root}",
        ]
        if self.datasources:
            lines.append(f"数据源: {', '.join(self.datasources)}"
                         + ("（含 live 探测）" if self.live else ""))
        else:
            lines.append("数据源: （未发现 KB 目录）")
        counts = " | ".join(f"{k} {v}" for k, v in self.counts.items())
        if counts:
            lines.append(f"条目: {counts}")

        if self.mounts:
            lines.append("")
            lines.append("挂点预览")
            for m in self.mounts:
                head = f"  [{m.kind}] {m.name}"
                if m.tier:
                    head += f"  tier={m.tier}"
                if m.status:
                    head += f"  status={m.status}"
                if m.datasource:
                    head += f"  ({m.datasource})"
                lines.append(head)
                for point in m.mounts:
                    lines.append(f"      → {point}")
                for note in m.notes:
                    lines.append(f"      ! {note}")

        if self.envelopes:
            lines.append("")
            lines.append("信封(capabilities 为推导物,非作者声明)")
            for e in self.envelopes:
                caps = e.get("capabilities") or {}
                head = (f"  [{e.get('kind')}] {e.get('name')}"
                        f"  source={e.get('source')}  state={e.get('state')}")
                lines.append(head)
                mounts = e.get("mounts") or []
                if mounts:
                    lines.append("      ⤷ " + ", ".join(
                        f"{m['node']}({m['tier']}/{m['effect']})"
                        for m in mounts))
                lines.append(
                    "      caps: vars={vars} effects={effects} targets={targets}".format(
                        vars=",".join(caps.get("variables") or []) or "—",
                        effects=",".join(caps.get("effects") or []) or "—",
                        targets=",".join(caps.get("targets") or []) or "—"))
                for u in e.get("unresolved") or []:
                    lines.append(f"      ! {u}")

        for label, issues in (("ERROR", self.errors), ("WARN", self.warnings)):
            lines.append("")
            if not issues:
                lines.append(f"{label}: 无")
                continue
            lines.append(f"{label} ({len(issues)}):")
            for i in issues:
                where = f"{i.datasource}/" if i.datasource else ""
                target = f"{where}{i.target}: " if i.target else ""
                lines.append(f"  - [{i.check}] {target}{i.message}")

        lines.append("")
        if self.errors:
            lines.append(f"结论: {len(self.errors)} 个硬错误 → 退出码 1")
        elif self.warnings:
            lines.append(
                f"结论: 0 个硬错误，{len(self.warnings)} 个警告"
                "（默认不拦，--strict 下退出码 1）")
        else:
            lines.append("结论: 干净 → 退出码 0")
        return "\n".join(lines)
