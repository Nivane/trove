"""PresetService —— 预设包的加载、版本化与套用。

**完成态画面**:新数据源接入 = 选 preset + ``/kb init`` + 逐条确认,零改码。

## 双源与遮蔽语义(确定性)

- **内置**(``trove/presets/<name>/preset.yml``,随代码分发,只读);
- **组织**(``.trove/presets/<name>/preset.yml``,admin 管理,可版本化回滚)。

同名时**组织版遮蔽内置版** —— 与 org skill 的 ``SKILL.<lang>.md`` 覆盖
``SKILL.md`` 是同一条取向:部署侧的本地定制不该要求改代码。遮蔽是**确定
且可见**的:``list``/``get`` 带 ``source`` 与 ``shadowed`` 位,``trove
validate`` 对它出一条 warning(内置版的更新从此不再生效,这件事必须有人
说出口,而不是等人某天发现改了没用)。

## 套用的红线:一切落 pending,绝不生效

``apply`` 产出的**只有草稿**:

- skills → ``SkillService.create``(**既有门,零改动复用**)→ ``status:
  pending``,不进 render/prompt/load_skill,直到 admin 确认;
- decisions → ``decision_drafts.yml``(``decision.drafts.DecisionDraftStore``)
  —— 规则**不在** ``decisions.yml`` 里,所以调度面/执行面结构上读不到它;
- domains → ``SemanticManager.create_draft``(语义层既有审批流,
  ``semantic_drafts.yml``),``semantics.yml`` 一个字节不动;
- semantics / presentation → **只校验形状 + 进报告当提示**,不产任何内容。

报告逐条 ``{section, item, status, reason}``:``drafted``(落了草稿)/
``skipped``(无需动作:引用已就位、或提示段)/ ``unresolved``(引用的资产在
目标数据源上解析不到,什么都没落)。**宁可报 unresolved,也不静默成功**。

## 三类资产的解析判据(为什么不一样)

统一原则:**草稿只有在"将来确认得下来"时才落** —— 落一份永远确认不了的
草稿,等于把死配置藏进待审队列,比不落更糟。

- 技能骨架:``SkillService.create`` 自带完整校验(名字/描述/正文/tier/
  validator 规格),落盘 = 可确认,直接落;
- 决策模板:落草稿前先过 ``lint_rule``(**结构不过 = 永远确认不了 →
  unresolved,不落**)。模板里引用的 metric 若不在目标语义模型里,仍落草稿
  —— ``save_decisions`` 的结构 lint 不查 metric(确认是可能的),但报告会
  在 reason 里点名"这些指标未声明,确认前先建模" —— 这是个**会响的**提示,
  不是静默;
- 主题域骨架:**不落**。语义层在确认那一刻硬校验 ``datasets``(必填且每个
  名字都要在模型里声明过,空作用域 = 域内什么都问不了),所以 datasets
  解析不了的主题域草稿**确认时必炸** —— 那正是"落一份确认不了的东西"。
  unresolved + 点名缺哪个数据集,让补全动作有据可依。

## 语法糖

``skills: [plan_query]``(字符串 / 只带 ``name`` 的映射)是**引用**:解析
到已生效的资产就 ``skipped``(无需草稿),解析不到就 ``unresolved``。
引用解析只认**生效**状态:一份还在 pending 的 org skill 不算解析成功 ——
preset 说"这个数据源应当具备 X 方法论",而 X 还没过确认门,那是一句尚未
成立的话,不能报成功。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from trove.core.logging import get_logger
from trove.services.decision.drafts import DecisionDraftStore
from trove.services.decision.rules import (
    RuleError,
    lint_rule,
    parse_rule,
)
from trove.services.kb.git_versioning import GitVersioning
from trove.services.presets.models import (
    Preset,
    STATUSES,
    ApplyReport,
    brief,
    parse_preset,
    preset_to_dict,
    reject_unknown,
)

logger = get_logger(__name__)

#: 提示段的条目名(apply 报告里逐条 skipped 的那几条)。
_SEMANTICS_ITEM = "semantics"
_PRESENTATION_ITEM = "presentation"


def _builtin_root() -> Path:
    import trove

    return Path(trove.__file__).parent / "presets"


class PresetService:
    """Load / version / apply presets (built-in + org)."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        builtin_root: Path | None = None,
        kb: Any = None,
        skills: Any = None,
        git_enabled: bool = True,
    ) -> None:
        self.root = Path(root) if root is not None else Path.cwd() / ".trove" / "presets"
        self.builtin_root = Path(builtin_root) if builtin_root is not None else _builtin_root()
        self.kb = kb
        self.skills = skills
        # 写路径 git 自动版本化 —— 与 KB / org skills 同一个 ``GitVersioning``,
        # 守卫(只暂存点名文件 / 失败降级 no-op)因此只有一份实现。
        self.git = GitVersioning(self.root, enabled=True) if git_enabled else None
        self._drafts = DecisionDraftStore(kb) if kb is not None else None

    # ── 双源装载 ──────────────────────────────────────────

    @staticmethod
    def _read_dir(base: Path, source: str) -> dict[str, Preset]:
        """一个根目录下的全部 preset(坏文件不进结果,由 validate 报)。"""
        out: dict[str, Preset] = {}
        if not base.is_dir():
            return out
        for d in sorted(base.iterdir()):
            path = d / "preset.yml"
            if not d.is_dir() or not path.exists():
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                out[d.name] = parse_preset(
                    data, source=source, path=path, name_hint=d.name)
            except Exception:  # noqa: BLE001 — 坏文件由 validate 点名,读取面跳过
                logger.warning("preset 读取失败: %s", path, exc_info=True)
        return out

    def _sources(self) -> tuple[dict[str, Preset], dict[str, Preset]]:
        """``(org, builtin)`` 两份按名字索引的字典。"""
        return self._read_dir(self.root, "org"), \
            self._read_dir(self.builtin_root, "builtin")

    def merged(self) -> list[dict[str, Any]]:
        """列表视图(名字序)。同名 → 内置版带 ``shadowed: true`` 退回一份。"""
        org, builtin = self._sources()
        out: list[dict[str, Any]] = [brief(p) for _, p in sorted(org.items())]
        for name, p in sorted(builtin.items()):
            out.append(brief(p, shadowed=name in org))
        return sorted(out, key=lambda e: e["name"])

    def load(self, name: str) -> Preset:
        """按名装载**生效的**那一份(org 遮蔽 builtin)。"""
        org, builtin = self._sources()
        if name in org:
            return org[name]
        if name in builtin:
            return builtin[name]
        known = ", ".join(sorted({*org, *builtin})) or "(无预设)"
        raise KeyError(f"preset 不存在: {name!r}(已装载: {known})")

    def read_raw(self, name: str) -> str:
        """``preset.yml`` 原文(admin 编辑面)。"""
        if name in self._read_dir(self.root, "org"):
            return (self.root / name / "preset.yml").read_text(encoding="utf-8")
        if name in self._read_dir(self.builtin_root, "builtin"):
            return (self.builtin_root / name / "preset.yml").read_text(encoding="utf-8")
        raise KeyError(f"preset 不存在: {name!r}")

    # ── 套用 ─────────────────────────────────────────────

    async def apply(self, name: str, datasource: str, *, actor: str = "") -> ApplyReport:
        """把 ``name`` 套到 ``datasource`` 上 —— 全部内容只落 pending 草稿。

        逐段调用目标面**既有的**草稿入口(技能门 / 决策草稿 / 语义审批流),
        本方法自己不写任何一个生效文件。
        """
        preset = self.load(name)
        report = ApplyReport(preset=preset.name, datasource=datasource,
                             source=preset.source)
        for entry in preset.skills:
            self._apply_skill(preset, entry, report, actor=actor)
        for entry in preset.decisions:
            await self._apply_decision(preset, entry, datasource, report)
        for entry in preset.domains:
            await self._apply_domain(preset, entry, datasource, report, actor=actor)
        self._apply_hints(preset, report)
        return report

    # ── 段:skills ────────────────────────────────────────

    def _skill_names(self) -> tuple[set[str], set[str], set[str]]:
        """``(code, org_confirmed, org_other)`` 三份名字集合。"""
        if self.skills is None:
            return set(), set(), set()
        code = {str(e.get("name")) for e in self.skills.list_code_skills()}
        org = self.skills.list_org()
        confirmed = {str(e.get("name")) for e in org if e.get("status") == "confirmed"}
        other = {str(e.get("name")) for e in org if e.get("status") != "confirmed"}
        return code, confirmed, other

    def _apply_skill(self, preset: Preset, entry: Any, report: ApplyReport, *,
                     actor: str) -> None:
        if isinstance(entry, str):
            code, confirmed, other = self._skill_names()
            if entry in code:
                report.add("skills", entry, "skipped",
                           "code skill(随代码分发,已生效)")
            elif entry in confirmed:
                report.add("skills", entry, "skipped",
                           "org skill 已确认,已生效")
            elif entry in other:
                report.add("skills", entry, "unresolved",
                           "org skill 存在但尚未确认(pending)—— 先确认该技能")
            else:
                report.add("skills", entry, "unresolved", "技能不存在")
            return

        name = str(entry.get("name") or "")
        if self.skills is None:
            report.add("skills", name, "unresolved", "技能服务未接线,无法落草稿")
            return
        existing = self.skills.read_skill(name)
        if existing is not None:
            status = existing.get("status", "pending")
            report.add("skills", name, "skipped",
                       f"同名 skill 已存在(status={status}),未覆盖")
            return
        code, _confirmed, _other = self._skill_names()
        if name in code:
            # org skill 按名字遮蔽 code skill —— 套一份 preset 不该悄悄换掉
            # 随码分发的方法学资产。要定制就改个名字(validate 同一条警告)。
            report.add("skills", name, "skipped",
                       "同名 code skill 已存在,不覆盖(org skill 会按名字"
                       "遮蔽它;要定制请改名)")
            return
        payload = {k: v for k, v in entry.items()}
        payload.setdefault("tier", "available")
        payload["source"] = f"preset:{preset.name}"
        try:
            saved = self.skills.create(payload, actor=actor)
        except ValueError as exc:
            # create 是唯一的技能写入门(名字/描述/正文/tier/validator 规格
            # 全在它那里校验)—— 它拒了,这条骨架就落不了草稿,如实报。
            report.add("skills", name, "unresolved", f"技能草稿被拒: {exc}")
            return
        hits = saved.get("injection_hits") or []
        note = f"已落 pending 草稿(tier={saved.get('tier')}),确认后才进入投递面"
        if hits:
            note += f";注入扫描命中: {', '.join(hits)}"
        report.add("skills", name, "drafted", note)

    # ── 段:decisions ─────────────────────────────────────

    def _metric_names(self, datasource: str) -> set[str] | None:
        """目标语义模型声明的指标名;模型不可用 → ``None``(判不了)。"""
        if self.kb is None:
            return None
        try:
            from trove.services.semantic_layer.manage import SemanticManager

            model = SemanticManager(self.kb).model(datasource)
        except Exception:  # noqa: BLE001
            return None
        if model is None:
            return None
        return {str(m.name) for m in (getattr(model, "metrics", None) or [])}

    async def _apply_decision(self, preset: Preset, entry: Any, datasource: str,
                              report: ApplyReport) -> None:
        if self._drafts is None or self.kb is None:
            report.add("decisions", str(entry)[:40], "unresolved",
                       "决策面未接线,无法落草稿")
            return
        try:
            doc = self.kb.load_decisions(datasource)
        except RuleError as exc:
            report.add("decisions", str(entry)[:40], "unresolved",
                       f"decisions.yml 读取失败: {exc}")
            return

        if isinstance(entry, str):
            if any(r.id == entry for r in doc.rules):
                report.add("decisions", entry, "skipped",
                           "规则已存在于 decisions.yml")
            elif self._drafts.find_rule(datasource, entry, status="pending"):
                report.add("decisions", entry, "skipped", "同 id 的草稿已在待审队列")
            else:
                report.add("decisions", entry, "unresolved",
                           "规则不存在(引用形态只能引用已有规则)")
            return

        raw = dict(entry)
        rid = str(raw.get("id") or "")
        # 模板**默认停用**:一份没被任何人按本数据源审过的规则,确认那一刻
        # 不该顺带获得"按点开跑"的权力。作者显式写 enabled: true 才覆盖。
        defaulted = "enabled" not in raw
        if defaulted:
            raw["enabled"] = False
        try:
            rule = parse_rule(raw)
            issues = lint_rule(rule)
        except RuleError as exc:
            report.add("decisions", rid, "unresolved", f"规则模板结构不合法: {exc}")
            return
        if issues:
            # 结构不过 = 确认时必被 save_decisions 拒 → 不落死草稿。
            report.add("decisions", rid, "unresolved",
                       "规则模板过不了结构 lint(确认时必被拒): " + "; ".join(issues))
            return

        missing = sorted(
            set(rule.subject.metrics) - (self._metric_names(datasource) or set())
        ) if self._metric_names(datasource) is not None else []
        note = f"来自 preset {preset.name}"
        if defaulted:
            note += ";模板未声明 enabled → 草稿按停用草拟(确认后仍不会调度)"
        try:
            result = self._drafts.add(
                datasource, rule, source=f"preset:{preset.name}", note=note)
        except RuleError as exc:
            report.add("decisions", rid, "unresolved", f"草稿不能落盘: {exc}")
            return
        if result["status"] == "present":
            report.add("decisions", rid, "skipped", "规则已存在于 decisions.yml")
            return
        if result["status"] == "exists":
            report.add("decisions", rid, "skipped", "同 id 的草稿已在待审队列")
            return

        reason = "已落 pending 草稿(decision_drafts.yml),确认后才写入 decisions.yml"
        if defaulted:
            reason += ";草稿为停用状态(enabled: false)"
        if missing:
            reason += (f";注意:指标 {', '.join(missing)} 未在目标语义模型声明,"
                       "确认前需先建模(否则调度时会报错)")
        report.add("decisions", rid, "drafted", reason)

    # ── 段:domains ───────────────────────────────────────

    def _model_datasets(self, datasource: str) -> set[str] | None:
        if self.kb is None:
            return None
        try:
            from trove.services.semantic_layer.manage import SemanticManager

            model = SemanticManager(self.kb).model(datasource)
        except Exception:  # noqa: BLE001
            return None
        if model is None:
            return None
        return {str(d.name) for d in (getattr(model, "datasets", None) or [])}

    def _topic_names(self, datasource: str) -> set[str] | None:
        if self.kb is None:
            return None
        try:
            from trove.services.semantic_layer.manage import SemanticManager

            model = SemanticManager(self.kb).model(datasource)
        except Exception:  # noqa: BLE001
            return None
        if model is None:
            return None
        return {str(t.name) for t in (getattr(model, "topics", None) or [])}

    async def _apply_domain(self, preset: Preset, entry: Any, datasource: str,
                            report: ApplyReport, *, actor: str = "") -> None:
        if self.kb is None:
            report.add("domains", str(entry)[:40], "unresolved", "KB 未接线")
            return
        from trove.services.semantic_layer.manage import SemanticManager

        manager = SemanticManager(self.kb)

        if isinstance(entry, str):
            topics = self._topic_names(datasource)
            if topics is None:
                report.add("domains", entry, "unresolved",
                           "目标数据源没有可用语义模型(先 /kb init)")
            elif entry in topics:
                report.add("domains", entry, "skipped", "主题域已在语义模型中声明")
            else:
                report.add("domains", entry, "unresolved", "主题域不存在")
            return

        name = str(entry.get("name") or "")
        datasets = self._model_datasets(datasource)
        if datasets is None:
            report.add("domains", name, "unresolved",
                       "目标数据源没有可用语义模型(先 /kb init)")
            return
        declared = [str(d) for d in (entry.get("datasets") or []) if str(d).strip()]
        if not declared:
            # 语义层在确认那一刻硬拒空作用域(_apply_topic:datasets 必填),
            # 落下去就是一份永远确认不了的草稿 —— 不落,点明缺什么。
            report.add("domains", name, "unresolved",
                       "主题域骨架未声明 datasets —— 语义层在确认时拒空作用域"
                       "(域内将什么都问不了);请按目标语义模型补全后重套")
            return
        missing = [d for d in declared if d not in datasets]
        if missing:
            report.add("domains", name, "unresolved",
                       f"数据集未在目标语义模型声明: {', '.join(missing)}"
                       f"(已声明: {', '.join(sorted(datasets)) or '无'})")
            return
        pending = manager.drafts(datasource).get("pending", [])
        if any(str(d.get("kind")) == "topic" and str(d.get("name")) == name
               for d in pending):
            # 幂等:重套一份 preset 不该在待审队列里叠出第二份同名草稿。
            report.add("domains", name, "skipped", "同名的主题域草稿已在待审队列")
            return
        payload: dict[str, Any] = {"datasets": declared}
        for key in ("description", "synonyms", "metrics", "examples"):
            if entry.get(key):
                payload[key] = entry[key]
        try:
            draft = await manager.create_draft(
                datasource, "topic", "upsert", name, payload,
                note=f"来自 preset {preset.name}", actor=actor)
        except (ValueError, KeyError) as exc:
            report.add("domains", name, "unresolved", f"主题域草稿不能落盘: {exc}")
            return
        report.add("domains", name, "drafted",
                   f"已落 pending 草稿(semantic_drafts.yml, id={draft.get('id')}),"
                   "确认后才写入 semantics.yml")

    # ── 段:semantics / presentation(提示) ───────────────

    def _apply_hints(self, preset: Preset, report: ApplyReport) -> None:
        """提示段**不产任何生效内容**:逐条列出,状态恒为 ``skipped``。

        它们在报告里出现是刻意的 —— 一份 preset 的价值有一半在"它建议你怎么
        看这类数据",而"没写进去"与"没看见"必须能区分。
        """
        for key, value in preset.semantics.items():
            for text in _flatten(value):
                report.add("semantics", f"{key}: {text}"[:80], "skipped",
                           "仅提示,不产发生效内容(preset 不写语义层)")
        for key, value in preset.presentation.items():
            for text in _flatten(value):
                report.add("presentation", f"{key}: {text}"[:80], "skipped",
                           "仅提示,不产发生效内容(preset 不改展示配置)")

    # ── 写路径:admin 编辑 + 版本化 ───────────────────────

    def save(self, name: str, data: Any, *, actor: str = "") -> dict[str, Any]:
        """写入/覆盖一份**组织** preset(``.trove/presets/<name>/preset.yml``)。

        契约先过(未知键/必填/闭集),后落盘 —— 与 org skill 的写入门同序:
        校验在写盘前,坏内容不进任何地方。
        """
        reject_unknown(data, (
            "name", "version", "description", "author",
            "skills", "decisions", "domains", "semantics", "presentation",
        ), "preset")
        preset = parse_preset(data, source="org", name_hint=name,
                              path=self.root / name / "preset.yml")
        # version 是**修订计数**:内容变更就往前一格(与 org skill 同义)。
        # 作者显式写更高的号(引入外部的第 N 版)则尊重作者。
        current = self._read_dir(self.root, "org").get(name)
        if current is not None:
            preset.version = max(current.version + 1, preset.version)
        version = preset.version
        path = self.root / name / "preset.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(preset_to_dict(preset), allow_unicode=True,
                           sort_keys=False),
            encoding="utf-8",
        )
        self._commit(f"preset: save {name} v{version}", name, actor=actor)
        return brief(self.load(name))

    def history(self, name: str, limit: int = 50) -> list[dict]:
        """该 org preset 的提交历史(非 git 环境 → [])。"""
        d = self.root / name
        if not (d / "preset.yml").exists():
            # 内置版没有组织侧历史(它随代码走,历史在代码仓库里)。
            if name in self._read_dir(self.builtin_root, "builtin"):
                return []
            raise KeyError(f"preset 不存在: {name!r}")
        if self.git is None:
            return []
        return self.git.history_files([d], limit=limit)

    def rollback(self, name: str, sha: str, *, actor: str = "",
                 message: str = "") -> dict[str, Any]:
        """把 org preset 回滚到 ``sha``(新建提交,``version`` 往前一格)。

        回滚是**一次新的修订**,不是时间倒流 —— 与 org skill 的 ``rollback``
        同一条纪律:审计史里版本号单调。
        """
        d = self.root / name
        if not (d / "preset.yml").exists():
            raise KeyError(f"org preset 不存在: {name!r}")
        if self.git is None:
            return {"rolled_back": False, "reason": "disabled"}
        base = (self._read_dir(self.root, "org").get(name) or Preset(name="")).version

        def _bump(directory: Path) -> None:
            target = directory / "preset.yml"
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            raw_version = data.get("version", 1)
            current = raw_version if isinstance(raw_version, int) and \
                not isinstance(raw_version, bool) else 1
            data["version"] = max(current, base) + 1
            target.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8")

        result = self.git.rollback_tree(
            d, sha, message or f"presets: rollback {name} to {sha[:8]}",
            transform=_bump,
            trailers={"Generator": "presets.rollback", "Approved-by": actor},
        )
        return result

    def _commit(self, message: str, name: str, *, actor: str = "") -> dict:
        """写路径自动版本化(尽力而为,失败降级 no-op)。"""
        if self.git is None:
            return {"committed": False, "reason": "disabled"}
        trailers = {"Generator": "presets.save", "Approved-by": actor} \
            if actor else {"Generator": "presets.save"}
        d = self.root / name
        files = [p for p in sorted(d.glob("*")) if p.is_file()]
        return self.git.commit_files(files, message, trailers=trailers)


def _flatten(value: Any) -> list[str]:
    """提示段的一行行文本(标量 / 列表都收;嵌套映射转 ``k=v``)。"""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            out.extend(_flatten(v))
        return out
    if isinstance(value, dict):
        return [f"{k}={_scalar(v)}" for k, v in value.items()]
    return [str(value)]


def _scalar(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def apply_statuses() -> tuple[str, ...]:
    """套用状态闭集(前端/文档单一来源)。"""
    return STATUSES
