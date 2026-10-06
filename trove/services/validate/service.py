"""``trove validate`` — one dry run over every extension surface.

The shape of the command is *collect, don't rewrite*: every check below
delegates to the checker that already judges that surface at write time or
at run time (KB lint functions, ``decision.rules`` lint + its compile gate,
``SkillService``'s own reader and validator-spec check). Nothing is judged
twice — if a new rule is added to one of those checkers, this command picks
it up for free, and it can never be *looser* than the surface it previews.

What it adds on top of the individual checkers is the thing no single
surface can answer: **what will actually take effect**. A skill whose tier
has no delivery path, a validator declaring a host it will never run at, a
rule whose metric no longer exists in the semantic model, a KB the query
pipeline will refuse outright — each of those is a *dead configuration*:
from every external face it looks the same as a working one, and nothing
ever reports it. That is the failure class this module exists to catch.

Zero LLM. Zero network by default (``live=True`` is the only switch that
connects to a datasource, and it is opt-in).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from trove.core.logging import get_logger
from trove.services.action.templates import ActionTemplateService
from trove.services.decision.drafts import DecisionDraftStore
from trove.services.decision.rules import (
    RuleError,
    lint_advisories,
    lint_document,
    lint_rule,
    parse_rule,
)
from trove.services.decision.service import DecisionService
from trove.services.kb.lint import (
    lint_examples,
    lint_lessons,
    lint_semantics_document,
    lint_stats,
    lint_tables,
    lint_terms,
)
from trove.services.kb.live_lint import check_enums, check_undocumented_columns
from trove.services.kb.service import KbService, _parse_file
from trove.services.presets.models import Preset, parse_preset
from trove.services.presets.service import _builtin_root
from trove.services.skills.guards import GUARD_HOST
from trove.services.skills.service import (
    FRONTMATTER_FIELDS,
    GUARD_FIELDS,
    VALIDATOR_FIELDS,
    SkillService,
    _NAME_RE,
    _STATUSES,
    _TIERS,
    _declared_node,
)
from trove.services.skills.validators import VALIDATOR_HOST
from trove.services.validate.models import Issue, MountPreview, ValidateReport

logger = get_logger(__name__)

#: Nodes whose system prompts run ``SkillService.render_skills(node, ...)`` —
#: the full set of places a ``required`` org skill can be injected. Pinned to
#: the runtime by ``skill_node_call_sites`` (a source scan of the call sites
#: themselves) so this constant cannot silently fall behind: there is no
#: single runtime constant to import — the nodes are passed as literals at
#: seven call sites across ``trove/workflow/nodes/`` — and re-deriving the set
#: any other way would be one more copy to drift.
#:
#: The validator tier's host is *not* listed here: it is read from
#: ``VALIDATOR_HOST`` (``skills/validators.py``), the same constant the
#: runtime uses, so it is never copied in the first place.
SKILL_NODES = (
    "query_sketch",
    "analyze_error",
    "gen_sql",
    "attribution",
    "chart",
    "conclusion",
    "insights",
)

#: The only node that advertises ``available``-tier skills (the on-demand
#: ``load_skill`` tool exists in the agentic gen_sql loop alone). An
#: ``available`` skill declaring any other node is never advertised anywhere.
AVAILABLE_AD_NODES = ("gen_sql",)

_SKILL_CALL_RE = re.compile(
    r"(?:render_skills|available_skills_block|available_descriptions|has_available_for)"
    r"\(\s*[\"']([a-z_][a-z0-9_]*)[\"']"
)
_VALIDATOR_CALL_RE = re.compile(
    r"validators_for\(\s*([A-Za-z_][A-Za-z0-9_]*)|"
    r"validators_for\(\s*[\"']([a-z_][a-z0-9_]*)[\"']"
)


def skill_node_call_sites(pkg_root: Path | None = None) -> tuple[set[str], set[str]]:
    """Scan the package for the nodes the runtime actually renders skills at.

    Returns ``(literal_nodes, validator_host_refs)`` where ``literal_nodes``
    are the string literals passed to the four node-keyed skill readers, and
    ``validator_host_refs`` are the *identifiers* passed to
    ``validators_for`` (``VALIDATOR_HOST`` at every call site — a literal
    there would mean a second source of truth for the host node).

    The regression test asserts ``literal_nodes == set(SKILL_NODES)`` and
    ``validator_host_refs == {"VALIDATOR_HOST"}``; a new injection point
    added without updating ``SKILL_NODES`` fails that test instead of
    silently making validate's judgement narrower than the runtime's.
    """
    import trove

    root = Path(pkg_root) if pkg_root is not None else Path(trove.__file__).parent
    here = Path(__file__).parent
    nodes: set[str] = set()
    hosts: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if here in path.parents:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("def ", "async def ")):
                continue  # 定义处不是调用处(`def validators_for(self, ...)`)
            nodes.update(_SKILL_CALL_RE.findall(line))
            for ident, literal in _VALIDATOR_CALL_RE.findall(line):
                hosts.add(ident or literal)
    return nodes, hosts


# ── helpers ──────────────────────────────────────────────


def _err(check: str, message: str, *, target: str = "",
         datasource: str = "") -> Issue:
    return Issue(check=check, severity="error", message=message,
                 target=target, datasource=datasource)


def _warn(check: str, message: str, *, target: str = "",
          datasource: str = "") -> Issue:
    return Issue(check=check, severity="warning", message=message,
                 target=target, datasource=datasource)


def _datasource_names(kb: KbService) -> list[str]:
    """KB subdirectories that carry at least one YAML asset."""
    if not kb.kb_dir.is_dir():
        return []
    return sorted(
        d.name for d in kb._datasource_dirs()
        if any(d.glob("*.yml"))
    )


def _collect_entries(ds_dir: Path) -> tuple[
        list[dict], list[dict], list[dict], list[dict]]:
    """``.trove/kb/<ds>/*.yml`` → (terms, examples, tables, lessons).

    Same walk as ``scripts/lint_kb.py`` (same filters, same payload shapes)
    so the two can never judge different content.
    """
    terms: list[dict] = []
    examples: list[dict] = []
    tables: list[dict] = []
    lessons: list[dict] = []
    for yml in sorted(ds_dir.glob("*.yml")):
        for kind, key, payload in _parse_file(yml):
            if kind == "term":
                terms.append(payload)
            elif kind in ("example", "template"):
                examples.append(payload)
            elif kind == "table":
                # payload 不含表名(在 item_key 里),补回 "name" 供 lint 使用
                tables.append({"name": key, **payload})
            elif kind == "lesson":
                lessons.append(payload)
    return terms, examples, tables, lessons


def _duplicate_example_issues(examples: list[dict]) -> list[str]:
    """同题多例 / 完全重复 —— 检索按题面打分,重复项只会稀释同分。

    规则可自动生成,重复不会 —— 所以它是 warning(提示清理),不是 error。
    """
    issues: list[str] = []
    by_question: dict[str, set[str]] = {}
    for ex in examples:
        question = str(ex.get("question") or "").strip()
        if not question:
            continue
        by_question.setdefault(question, set()).add(
            " ".join(str(ex.get("sql") or "").split()))
    for question, sqls in sorted(by_question.items()):
        if len(sqls) > 1:
            issues.append(
                f"示例 {question!r}: 同一问题有 {len(sqls)} 条不同 SQL"
                "(检索同分,先清理再新增)")
    return issues


# ── KB ───────────────────────────────────────────────────


def _check_kb(datasource: str, ds_dir: Path) -> tuple[list[Issue], dict[str, int]]:
    """Static KB lint — the exact function set ``scripts/lint_kb.py`` calls.

    Per-family ``check`` ids (``kb.terms`` / ``kb.examples`` / …) so a CI
    consumer can filter or waive one family without parsing messages.
    """
    issues: list[Issue] = []
    terms, examples, tables, lessons = _collect_entries(ds_dir)
    schema = {str(t["name"]): set(t.get("columns", {})) for t in tables}

    families: list[tuple[str, str, list[str]]] = [
        ("error", "kb.terms", lint_terms(terms, schema)),
        ("error", "kb.examples", lint_examples(examples, set(schema))),
        ("warning", "kb.tables", lint_tables(tables)),
        ("warning", "kb.lessons", lint_lessons(lessons)),
        ("warning", "kb.stats", lint_stats(tables)),
        ("warning", "kb.examples", _duplicate_example_issues(examples)),
    ]

    semantics_yml = ds_dir / "semantics.yml"
    if semantics_yml.exists():
        try:
            data = yaml.safe_load(semantics_yml.read_text(encoding="utf-8")) or {}
            families.append(("error", "kb.semantics",
                             lint_semantics_document(data)))
        except Exception as e:  # noqa: BLE001 — 读取失败按一条 error 报,不炸整轮
            families.append(("error", "kb.semantics",
                             [f"semantics.yml 读取失败: {e}"]))
    else:
        # 语义优先无开关:没有语义模型的数据源会被查询管线整体拒绝,而
        # 静态 lint 一切"正常" —— 这类静默不可答必须在这里说出来。
        families.append(("error", "kb.semantics", [
            "缺少 semantics.yml —— 语义优先下查询管线会整体拒绝该数据源"
            "(先 /kb init)"]))

    for severity, check, messages in families:
        make = _err if severity == "error" else _warn
        issues += [make(check, m, datasource=datasource) for m in messages]

    counts = {
        "terms": len(terms),
        "examples": len(examples),
        "tables": len(tables),
        "lessons": len(lessons),
    }
    return issues, counts


async def _live_kb_checks(
    datasource: str, project_root: Path, ds_dir: Path,
) -> tuple[list[Issue], str | None]:
    """Optional live enum + undocumented-column probe (needs a connection).

    Returns the issues and, when the datasource could be resolved, its real
    dialect (used by the decision compile gate so the dry run compiles the
    same SQL the scheduler would).
    """
    issues: list[Issue] = []
    registry = adapter = None
    try:
        registry, adapter = await _resolve_live_adapter(datasource, project_root)
    except Exception as e:  # noqa: BLE001 — 解析失败退化为"探测跳过"
        issues.append(_warn(
            "kb.live", f"live 探测跳过:数据源解析失败: {e}",
            datasource=datasource))
        return issues, None
    if adapter is None:
        issues.append(_warn(
            "kb.live", "live 探测跳过:数据源未注册(先到管理端注册该数据源)",
            datasource=datasource))
        return issues, None

    _, _, tables, _ = _collect_entries(ds_dir)
    table_payloads = {str(t["name"]): t for t in tables}
    dialect = None
    try:
        dialect = adapter.dialect() or None
    except Exception:  # noqa: BLE001
        dialect = None
    try:
        for m in await check_enums(adapter, table_payloads):
            issues.append(_err("kb.enums", m, datasource=datasource))
        if (dialect or "") == "mysql":
            for m in await check_undocumented_columns(adapter, tables):
                issues.append(_warn("kb.columns", m, datasource=datasource))
        else:
            # 这条检查的 SQL 是 MySQL 专用的(information_schema + DATABASE()),
            # 别的方言上逐表报 SQL 错只会刷屏 —— 一句"跳过"比八条假警报诚实。
            issues.append(_warn(
                "kb.columns",
                f"列覆盖检查(对照 information_schema)仅 MySQL 档可用,"
                f"本次 dialect={dialect or '未知'},跳过",
                datasource=datasource))
    except Exception as e:  # noqa: BLE001
        issues.append(_warn(
            "kb.live", f"live 探测失败: {e}", datasource=datasource))
    finally:
        # 必须显式关连接:sqlite 适配器的 aiosqlite 连接留着一条非 daemon
        # worker 线程,解释器退出时会 join 它 —— 结果是「报告打完了、进程
        # 永不退出」(CI 上表现为挂死)。探测成功与否都要关。
        await _close_quietly(registry=registry, adapter=adapter)
    return issues, dialect


async def _close_quietly(*, registry=None, adapter=None) -> None:
    """Best-effort close of a live connection (registry preferred)."""
    try:
        if registry is not None:
            await registry.close_all()
        elif adapter is not None:  # pragma: no cover — 防御,正常路径带 registry
            await adapter.disconnect()
    except Exception:  # noqa: BLE001
        logger.warning("validate: 关闭 live 连接失败", exc_info=True)


async def _resolve_live_adapter(datasource: str, project_root: Path):
    """Registered adapter for ``datasource``, or ``(None, None)``.

    Returns ``(registry, adapter)`` —— 连 registry 一起交回,调用方才能把
    live 期间建立的连接全部关掉(见 ``_live_kb_checks`` 的 finally)。
    """
    from trove.services.datasource.config_store import ConfigStore, boot_register
    from trove.services.datasource.registry import ConnectorRegistry

    registry = ConnectorRegistry()
    cfgs: list = []
    path = project_root / ".trove" / "datasources.yml"
    if path.exists():
        try:
            cfgs = ConfigStore(path).load_configs()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"datasources.yml 读取失败: {e}") from e
    matched = [c for c in cfgs
               if getattr(c, "name", "") == datasource
               or getattr(c, "ds_id", "") == datasource]
    if matched:
        failed = await boot_register(registry, matched)
        if failed:
            # 部分成功也要关:失败的名字留在 failed 里,成功的那几个已经
            # 连上了 —— 丢掉 registry 就是丢掉它们的 worker 线程。
            await _close_quietly(registry=registry)
            raise RuntimeError(f"数据源连接失败: {', '.join(failed)}")
    elif datasource == "demo" and (Path.home() / ".trove" / "demo.db").exists():
        # 内置 demo 未注册时**只读打开已有文件** —— 绝不走
        # setup_demo_datasource:它会 unlink 重建 ~/.trove/demo.db,一个干跑
        # 命令不该有这种副作用。文件不存在就跳过(不替用户新建)。
        from trove.core.types import DatasourceConfig

        await registry.register(DatasourceConfig(
            name="demo", type="sqlite",
            connection_params={"path": str(Path.home() / ".trove" / "demo.db")},
            vector_backend="sqlite"))
    else:
        return None, None
    try:
        return registry, await registry.get(datasource)
    except Exception:
        await _close_quietly(registry=registry)
        raise


# ── decision rules ───────────────────────────────────────


async def _check_decisions(
    datasource: str, kb: KbService, ds_dir: Path, dialect: str,
) -> tuple[list[Issue], list[MountPreview], int]:
    """Schema lint + asset lint + the *compile* gate, per rule.

    Compile is the gate ``rules.py`` explicitly does **not** own ("whether the
    subject actually compiles is checked by the service, which owns the
    model") — so this goes through the service's own ``_compile`` instead of
    re-deriving the two-pass window/anchor logic here, where it would be free
    to drift from what a scheduled run executes.
    """
    issues: list[Issue] = []
    mounts: list[MountPreview] = []

    try:
        doc = kb.load_decisions(datasource)
    except Exception as e:  # noqa: BLE001 — RuleError 或读取失败:一条 error
        issues.append(_err("decision.schema", str(e), datasource=datasource))
        return issues, mounts, 0

    for m in lint_document(doc):
        issues.append(_err("decision.schema", m, datasource=datasource))
    for m in lint_advisories(doc):
        issues.append(_warn("decision.advisory", m, datasource=datasource))
    for m in kb._asset_lint(doc):
        issues.append(_err("decision.assets", m, datasource=datasource))

    svc = DecisionService(None, kb)
    model = None
    if (ds_dir / "semantics.yml").exists():
        try:
            model = svc._model_for(datasource, dialect)
        except Exception as e:  # noqa: BLE001
            issues.append(_err(
                "decision.compile", f"语义模型不可用,规则无法编译: {e}",
                datasource=datasource))

    for rule in doc.rules:
        preview = MountPreview(
            kind="rule", name=rule.id, datasource=datasource,
            status="enabled" if rule.enabled else "disabled")
        preview.mounts.append(
            "定时任务调度面:job 按 rule id 引用(trove job list 查看引用)")
        if rule.action is not None:
            preview.notes.append(
                f"行动模板 {rule.action.template!r}"
                f"(autonomy={rule.action.autonomy},人工确认后才发)")
        if not rule.enabled:
            preview.notes.append("已停用(enabled: false)→ 不会调度")
        mounts.append(preview)

        # 停用的规则编译不过只提示(warning):草稿态不该拦 CI;启用中的
        # 编译不过 = 到点必炸,那是硬错误。
        severity = "error" if rule.enabled else "warning"
        try:
            window, _baseline_window = svc._resolve_window(rule, date.today())
            if model is not None:
                await svc._compile(rule, model, dialect, datasource, window)
        except Exception as e:  # noqa: BLE001
            msg = f"规则无法编译/求值: {e}"
            issues.append(Issue(
                check="decision.compile", severity=severity, message=msg,
                target=rule.id, datasource=datasource))

    return issues, mounts, len(doc.rules)


# ── org skills ───────────────────────────────────────────


def _skill_mounts(entry: dict) -> tuple[list[str], list[str]]:
    """Where this skill lands at runtime — derived from the tiers' own rules.

    Mirrors ``render_skills`` (tier == required, node-matched full body),
    ``available_skills_block`` (tier == available, gen_sql only),
    ``validators_for`` (tier == validator, host from ``targets``) and
    ``guards_for`` (tier == guard, host from the tier's own rule), reading the
    entry through the service's own reader.
    """
    tier = entry.get("tier", "available")
    triggers = entry.get("triggers") or {}
    declared = _declared_node(triggers)
    notes: list[str] = []
    mounts: list[str] = []

    if tier == "required":
        nodes = [declared] if declared is not None else list(SKILL_NODES)
        for node in nodes:
            if node in SKILL_NODES:
                mounts.append(f"{node} 节点系统提示(整篇注入)")
            else:
                mounts.append(f"(无投递面:node {node!r} 不是注入节点)")
    elif tier == "available":
        if declared is None or declared in AVAILABLE_AD_NODES:
            mounts.append("gen_sql 可用技能广告(仅描述)+ load_skill 按需加载")
        else:
            mounts.append(
                f"(无投递面:available 档只在 {'/'.join(AVAILABLE_AD_NODES)} 广告)")
    elif tier == "validator":
        mounts.append(
            f"{VALIDATOR_HOST} 节点结果断言(零 LLM,命中进 validator_hits)")
    elif tier == "guard":
        mounts.append(
            f"{GUARD_HOST} 节点执行前 SQL 断言(零 LLM,命中进 guard_hits;"
            "blocking 打回生成,advisory 附注答案)")
    else:
        mounts.append(f"(无投递面:tier {tier!r} 非法)")

    narrowing = {k: v for k, v in triggers.items() if k != "node"}
    if narrowing:
        notes.append("triggers 收窄:" + ", ".join(
            f"{k}={v}" for k, v in narrowing.items()) + "(运行期按 ctx 匹配)")
    status = entry.get("status", "pending")
    if status != "confirmed":
        notes.append(f"status={status} → 未确认,当前不投递(确认后才走上表挂点)")
    return mounts, notes


def _check_skills(
    skills_root: Path,
) -> tuple[list[Issue], list[MountPreview], dict[str, int]]:
    """Org-skill checks: frontmatter, closed sets, validator spec, conflicts.

    Reads through ``SkillService``'s own reader (``read_skill`` /
    ``_parse_skill`` / ``_validate_validator_spec``) — the same functions the
    runtime and the admin API use — so this can never disagree with what the
    pipeline would do with the same file.
    """
    issues: list[Issue] = []
    mounts: list[MountPreview] = []
    counts = {"org_skills": 0, "code_skills": 0}

    svc = SkillService(skills_root)
    counts["code_skills"] = len(svc.list_code_skills())
    if not skills_root.is_dir():
        return issues, mounts, counts

    seen_names: dict[str, str] = {}   # meta name → dir name
    for d in sorted(skills_root.iterdir()):
        if not d.is_dir() or not (d / "SKILL.md").exists():
            continue
        counts["org_skills"] += 1
        target = d.name
        entry = svc.read_skill(d.name)
        if entry is None:
            issues.append(_err("skill.read", "SKILL.md 读取失败", target=target))
            continue
        if entry.get("error"):
            issues.append(_err("skill.read", str(entry["error"]), target=target))
            continue

        raw = svc._parse_skill((d / "SKILL.md").read_text(encoding="utf-8"))
        meta = raw.get("meta") or {}

        # 未知键:读路径保持宽容(存量文件不能被锁死),**这里报出来** ——
        # 写路径 reject、读路径宽容、validate 报告,三层各司其职。
        legal = set(FRONTMATTER_FIELDS) | set(VALIDATOR_FIELDS) | set(GUARD_FIELDS)
        unknown = sorted(k for k in meta if k not in legal)
        if unknown:
            issues.append(_warn(
                "skill.frontmatter",
                f"未知 frontmatter 键 {', '.join(unknown)}(读路径忽略它们)",
                target=target))

        tier = entry.get("tier", "available")
        status = entry.get("status", "pending")

        if tier not in _TIERS:
            issues.append(_err(
                "skill.tier",
                f"tier {tier!r} 不在 {_TIERS} —— 没有任何投递面会取用它",
                target=target))
        if status not in _STATUSES:
            issues.append(_err(
                "skill.status",
                f"status {status!r} 不在 {_STATUSES} —— 永远不会成为 confirmed,"
                "也就永远不投递", target=target))
        if not _NAME_RE.fullmatch(d.name):
            issues.append(_warn(
                "skill.name",
                f"目录名 {d.name!r} 不是规范名(^[a-z0-9][a-z0-9-]*$)"
                " —— 管理端写入面会拒绝这个名字", target=target))

        declared_name = meta.get("name")
        if declared_name is not None and str(declared_name) != d.name:
            # available 档广告出去的是 frontmatter 里的名字,而 load_skill
            # 按名字找目录 —— 不一致时模型拿到一个**取不到**的点名。
            msg = (f"frontmatter name {declared_name!r} 与目录名 {d.name!r} "
                   "不一致")
            if tier == "available":
                issues.append(_err(
                    "skill.name",
                    msg + ":广告出的名字经 load_skill 解析不到该目录",
                    target=target))
            else:
                issues.append(_warn("skill.name", msg, target=target))

        # 冲突:两份技能声明同一个**生效名**(frontmatter 名,缺省即目录名)
        # —— 广告/加载都按名字唯一,重名先到先得,后来者静默失效。
        effective = str(declared_name) if declared_name is not None else d.name
        prev = seen_names.get(effective)
        if prev is not None and prev != d.name:
            issues.append(_err(
                "skill.conflict",
                f"技能名 {effective!r} 已被 {prev!r} 占用 —— "
                "广告/加载按名字唯一", target=target))
        else:
            seen_names[effective] = d.name

        if tier == "validator":
            try:
                svc._validate_validator_spec(entry)
            except ValueError as e:
                issues.append(_err("skill.validator", str(e), target=target))
        elif tier == "guard":
            # 与 validator 档同款:读路径宽容(绕过写入的手写文件运行时降级
            # 为"判不了"),validate 报告 —— 写路径 reject、读路径宽容、
            # validate 报告,三层各司其职。
            try:
                svc._validate_guard_spec(entry)
            except ValueError as e:
                issues.append(_err("skill.guard", str(e), target=target))

        # 档位域字段的错位:键存在、但本档不读它 —— 写下去也不会运行。
        if tier != "validator":
            stray = sorted(f for f in VALIDATOR_FIELDS if f in meta)
            if stray:
                issues.append(_err(
                    "skill.validator",
                    f"{', '.join(stray)} 只在 tier=validator 生效"
                    "(写下去也不会运行)", target=target))
        if tier != "guard":
            stray = sorted(f for f in GUARD_FIELDS if f in meta)
            if stray:
                issues.append(_err(
                    "skill.guard",
                    f"{', '.join(stray)} 只在 tier=guard 生效"
                    "(写下去也不会运行)", target=target))

        if tier not in ("validator", "guard") and not str(
                entry.get("body") or "").strip():
            issues.append(_err(
                "skill.body", "正文为空 —— 注入的是空块", target=target))

        # node 合法性:按档位各自的投递面判定(runtime 的同一条规则)。
        if tier == "required":
            declared = _declared_node(entry.get("triggers") or {})
            if declared is not None and declared not in SKILL_NODES:
                issues.append(_err(
                    "skill.node",
                    f"triggers.node {declared!r} 不是注入节点"
                    f"(可注入:{', '.join(SKILL_NODES)})—— required 档永不注入",
                    target=target))
        elif tier == "available":
            declared = _declared_node(entry.get("triggers") or {})
            if declared is not None and declared not in AVAILABLE_AD_NODES:
                issues.append(_err(
                    "skill.node",
                    f"triggers.node {declared!r} 不在 available 广告面"
                    f"({', '.join(AVAILABLE_AD_NODES)})—— 永不广告/永不加载",
                    target=target))

        mount_points, notes = _skill_mounts(entry)
        mounts.append(MountPreview(
            kind="skill", name=entry["name"], tier=tier, status=status,
            mounts=mount_points, notes=notes))

    return issues, mounts, counts


# ── preset packs ─────────────────────────────────────────
#
# preset 是"接入模板":把跨源方法论骨架声明成一份 YAML,套到新数据源上
# 只落 pending 草稿。这里的检查分两类,判据不同、**去向也不同**:
#
# - **静态**(与数据源无关,error):契约(闭键集/必填/名字==目录名)、
#   技能引用的解析(code skill / 已确认 org skill)、技能骨架能否过
#   ``SkillService.create`` 的写入面、规则模板能否过结构 lint、主题域骨架
#   有没有 datasets(语义层确认时硬拒空作用域)。这些在**任何**数据源上都
#   同样地失败 —— 套用报告里必然是 unresolved,所以是 error。
# - **数据源绑定**(给 ``--datasource`` 才判,warning):规则 id / 主题域名
#   引用、模板里的 metric、主题域 datasets 是否已在**这个**数据源的
#   decisions.yml / 语义模型里。preset 不绑定数据源(它的用途正是"套到
#   还没接入的源上"),所以按 warning 报:可能只是这个源还没建模。


def _read_presets(base: Path, source: str) -> tuple[dict[str, Preset], list[Issue]]:
    """一个根目录下的 preset —— 解析成功的与**失败的**都要带回。

    ``PresetService._read_dir`` 对坏文件宽容(跳过 + 日志),生产读取面必须
    如此;validate 的职责正是把那些跳过点出来 —— 否则一份谁都装载不了的
    preset,从任何外部面看都与"写对了"一样。
    """
    parsed: dict[str, Preset] = {}
    issues: list[Issue] = []
    if not base.is_dir():
        return parsed, issues
    for d in sorted(base.iterdir()):
        path = d / "preset.yml"
        if not d.is_dir() or not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            parsed[d.name] = parse_preset(
                data, source=source, path=path, name_hint=d.name)
        except Exception as e:  # noqa: BLE001 — 读取失败按一条 error 报
            issues.append(_err("preset.load", f"读取失败: {e}", target=d.name))
    return parsed, issues


def _preset_skill_names(skills_root: Path) -> tuple[set[str], set[str], set[str]]:
    """``(code, org_confirmed, org_other)`` —— 技能引用的解析面。

    与 ``PresetService._skill_names`` 同一条判据:引用只认**生效**状态,
    还在 pending 的 org skill 不算解析成功(preset 说"应当具备 X",而 X
    还没过确认门,那是一句尚未成立的话)。
    """
    svc = SkillService(skills_root)
    code = {str(e.get("name")) for e in svc.list_code_skills()}
    org = svc.list_org()
    confirmed = {str(e.get("name")) for e in org if e.get("status") == "confirmed"}
    other = {str(e.get("name")) for e in org if e.get("status") != "confirmed"}
    return code, confirmed, other


def _skill_template_problem(entry: dict, name: str) -> str:
    """技能骨架能不能落成草稿 —— 用**写入面自己**在一个 tmp 目录里干跑。

    与 ``SkillService.create`` 完全相同的那套校验(名字/描述/正文/tier/
    validator 规格 + 未知键),不在这里复写:复写的版本迟早与写入面漂移,
    而 validate 比写入面宽松的后果正是"干跑通过、套用报 unresolved"。
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="trove-preset-check-") as tmp:
        try:
            SkillService(Path(tmp)).create({**entry, "name": name})
        except ValueError as e:
            return str(e)
    return ""


def _preset_decision_problem(entry: dict) -> tuple[list[str], Any]:
    """规则模板 → (问题列表, 解析出的规则或 None)。

    默认 ``enabled: false`` 与 ``PresetService._apply_decision`` 同一条
    (模板默认停用):lint 必须在**套用时真正落下去的那个形状**上判。
    """
    raw = dict(entry)
    if "enabled" not in raw:
        raw["enabled"] = False
    try:
        rule = parse_rule(raw)
    except RuleError as e:
        return [f"解析失败: {e}"], None
    return [f"过不了结构 lint(确认时必被拒): {m}" for m in lint_rule(rule)], rule


def _preset_mounts(preset: Preset, *, shadowed: bool,
                   checked_datasource: str) -> MountPreview:
    counts = preset.counts
    preview = MountPreview(
        kind="preset", name=preset.name,
        status=f"{preset.source} v{preset.version}",
        mounts=[], notes=[],
    )
    if counts["skills"]:
        preview.mounts.append(
            f"skills({counts['skills']}): 技能草稿队列(pending)"
            " → 确认后按 tier 注入/广告")
    if counts["decisions"]:
        preview.mounts.append(
            f"decisions({counts['decisions']}): decision_drafts.yml"
            "(**不在** decisions.yml 里,调度面读不到)→ 确认后写入")
    if counts["domains"]:
        preview.mounts.append(
            f"domains({counts['domains']}): 语义草稿队列 semantic_drafts.yml"
            " → 确认后写入 semantics.yml")
    if counts["semantics"] or counts["presentation"]:
        preview.mounts.append(
            "(仅提示:preset 不写语义层,也不改展示配置)")
    if shadowed:
        preview.notes.append("组织版遮蔽同名内置版 —— 内置版的后续更新不再生效")
    if checked_datasource:
        preview.notes.append(f"数据源绑定引用按 {checked_datasource!r} 解析")
    else:
        preview.notes.append(
            "未给 --datasource:规则 id / 主题域 / metric 的解析只在套用报告里"
            "逐条给出(或用 trove validate --datasource <ds> 预检)")
    return preview


def _check_presets(
    root: Path, kb: KbService, skills_root: Path, *, datasource: str = "",
) -> tuple[list[Issue], list[MountPreview], dict[str, int]]:
    """预设包契约 + 引用解析(静态 error,数据源绑定 warning)。"""
    issues: list[Issue] = []
    mounts: list[MountPreview] = []
    counts = {"presets": 0, "preset_items": 0}

    org, org_issues = _read_presets(root / ".trove" / "presets", "org")
    builtin, builtin_issues = _read_presets(_builtin_root(), "builtin")
    issues += org_issues + builtin_issues

    code, confirmed, other = _preset_skill_names(skills_root)

    # 数据源绑定面(给了 datasource 才解析;读不到就当判不了,静默 —— 这些
    # 面自身的错由对应的检查器去报,不在这里刷第二条)。
    decisions_doc = None
    drafts_store = DecisionDraftStore(kb)
    model = dataset_names = topic_names = metric_names = None
    if datasource:
        try:
            decisions_doc = kb.load_decisions(datasource)
        except Exception:  # noqa: BLE001
            decisions_doc = None
        try:
            from trove.services.semantic_layer.manage import SemanticManager

            model = SemanticManager(kb).model(datasource)
        except Exception:  # noqa: BLE001
            model = None
        if model is not None:
            metric_names = {str(m.name) for m in (getattr(model, "metrics", None) or [])}
            dataset_names = {str(d.name) for d in (getattr(model, "datasets", None) or [])}
            topic_names = {str(t.name) for t in (getattr(model, "topics", None) or [])}

    for name in sorted({*org, *builtin}):
        preset = org.get(name) or builtin[name]
        shadowed = name in org and name in builtin
        if shadowed:
            issues.append(_warn(
                "preset.shadow",
                f"组织版(v{preset.version})遮蔽内置版"
                f"(v{builtin[name].version}) —— 内置版的后续更新不再生效",
                target=name))
        counts["presets"] += 1
        counts["preset_items"] += sum(preset.counts.values())

        # ── skills ──
        for entry in preset.skills:
            if isinstance(entry, str):
                if entry in code or entry in confirmed:
                    continue
                if entry in other:
                    issues.append(_err(
                        "preset.ref",
                        f"技能引用 {entry!r} 存在但尚未确认(pending)"
                        " —— 套用会报 unresolved,先确认该技能",
                        target=name))
                    continue
                issues.append(_err(
                    "preset.ref",
                    f"技能引用 {entry!r} 解析不到(code skill / 已确认 org skill "
                    "都没有)—— 套用会报 unresolved",
                    target=name))
                continue
            tname = str(entry.get("name") or "")
            if tname in other or tname in confirmed:
                issues.append(_warn(
                    "preset.conflict",
                    f"技能骨架 {tname!r} 与已有 org skill 同名 —— "
                    "套用时会跳过(骨架内容不会落)",
                    target=name))
                continue
            if tname in code:
                issues.append(_warn(
                    "preset.conflict",
                    f"技能骨架 {tname!r} 与 code skill 同名 —— 套用会跳过"
                    "(org skill 会按名字遮蔽它,要定制请改名)",
                    target=name))
            problem = _skill_template_problem(entry, tname)
            if problem:
                issues.append(_err(
                    "preset.skill",
                    f"技能骨架 {tname!r} 落不了草稿(create 会拒): {problem}",
                    target=name))

        # ── decisions ──
        seen_ids: set[str] = set()
        for entry in preset.decisions:
            if isinstance(entry, str):
                if decisions_doc is not None and not any(
                        r.id == entry for r in decisions_doc.rules):
                    issues.append(_warn(
                        "preset.ref",
                        f"规则引用 {entry!r} 不在 {datasource!r} 的 decisions.yml "
                        "—— 套用会报 unresolved",
                        target=name, datasource=datasource))
                continue
            rid = str(entry.get("id") or "")
            if rid in seen_ids:
                issues.append(_err(
                    "preset.rule",
                    f"规则模板 id {rid!r} 在本 preset 内重复", target=name))
                continue
            seen_ids.add(rid)
            problems, rule = _preset_decision_problem(entry)
            issues += [
                _err("preset.rule", f"规则模板 {rid!r}: {p}", target=name)
                for p in problems
            ]
            if problems or rule is None:
                continue
            if decisions_doc is not None and any(
                    r.id == rid for r in decisions_doc.rules):
                issues.append(_warn(
                    "preset.conflict",
                    f"规则模板 {rid!r} 与 {datasource!r} 已有规则同 id —— "
                    "套用会跳过", target=name, datasource=datasource))
            elif drafts_store.find_rule(datasource, rid, status="pending"):
                issues.append(_warn(
                    "preset.conflict",
                    f"规则模板 {rid!r} 的同 id 草稿已在待审队列 —— 套用会跳过",
                    target=name, datasource=datasource))
            if metric_names is not None:
                missing = sorted(set(rule.subject.metrics) - metric_names)
                if missing:
                    issues.append(_warn(
                        "preset.metric",
                        f"规则模板 {rid!r} 引用的指标未在 {datasource!r} 语义模型"
                        f"声明: {', '.join(missing)}(确认前需先建模,否则调度报错)",
                        target=name, datasource=datasource))

        # ── domains ──
        for entry in preset.domains:
            if isinstance(entry, str):
                if topic_names is not None and entry not in topic_names:
                    issues.append(_warn(
                        "preset.ref",
                        f"主题域引用 {entry!r} 不在 {datasource!r} 语义模型里"
                        " —— 套用会报 unresolved",
                        target=name, datasource=datasource))
                continue
            dname = str(entry.get("name") or "")
            declared = [str(d) for d in (entry.get("datasets") or [])
                        if str(d).strip()]
            if not declared:
                # 空作用域 = 一份 datasource-agnostic 的骨架的**定义形状**:
                # "域要收敛到哪几个数据集"是数据源事实,preset 里写不出来
                # (内置示例签的就是这个形状)。语义层在确认那一刻硬拒空作用域
                # (``_apply_topic``:datasets 必填),套用因此**不落**这份草稿
                # 而是报 unresolved —— 不会有死配置,但这件事必须在体检里
                # 说话:warning 级,重复 apply 报告里的那条判定。
                issues.append(_warn(
                    "preset.domain",
                    f"主题域骨架 {dname!r} 未声明 datasets —— 套用会报 unresolved"
                    "(语义层确认时拒空作用域);按目标语义模型补全 datasets 后"
                    "重套,或确认时手工收窄",
                    target=name))
                continue
            if dataset_names is not None:
                missing = [d for d in declared if d not in dataset_names]
                if missing:
                    issues.append(_warn(
                        "preset.domain",
                        f"主题域骨架 {dname!r} 的数据集未在 {datasource!r} 语义"
                        f"模型声明: {', '.join(missing)} —— 套用会报 unresolved",
                        target=name, datasource=datasource))

        mounts.append(_preset_mounts(
            preset, shadowed=shadowed, checked_datasource=datasource))

    return issues, mounts, counts


# ── entry point ──────────────────────────────────────────


async def run_validate(
    datasource: str = "",
    *,
    project_root: str | Path | None = None,
    live: bool = False,
) -> ValidateReport:
    """Run every check; never raises for a bad *configuration*.

    ``datasource=""`` validates every KB directory found under
    ``.trove/kb`` (skills and the semantic-model gate are cross-datasource
    and always run). A checker itself crashing is reported as an internal
    error rather than swallowed — a dry run that dies on one bad file would
    be useless exactly when it is needed most.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    report = ValidateReport(project_root=str(root), live=live)

    actions = ActionTemplateService(root / ".trove" / "actions")
    kb = KbService(root, git_kb=False, action_templates=actions)

    if datasource:
        names = [datasource]
        if not (kb.kb_dir / datasource).is_dir():
            report.issues.append(_err(
                "kb", f"KB 目录不存在: {kb.kb_dir / datasource}",
                datasource=datasource))
    else:
        names = _datasource_names(kb)
        if not names:
            report.issues.append(_warn(
                "kb", "未发现任何数据源 KB(.trove/kb/<datasource>/)"))

    for name in names:
        ds_dir = kb.kb_dir / name
        if not ds_dir.is_dir():
            continue
        report.datasources.append(name)
        try:
            issues, counts = _check_kb(name, ds_dir)
            report.issues += issues
            for k, v in counts.items():
                report.counts[f"{name}.{k}"] = v
        except Exception as e:  # noqa: BLE001
            logger.exception("validate: KB 检查失败 %s", name)
            report.issues.append(_err(
                "validate.internal", f"KB 检查异常: {e}", datasource=name))

        dialect = "sqlite"
        if live:
            try:
                live_issues, live_dialect = await _live_kb_checks(name, root, ds_dir)
                report.issues += live_issues
                if live_dialect:
                    dialect = live_dialect
            except Exception as e:  # noqa: BLE001
                logger.exception("validate: live 检查失败 %s", name)
                report.issues.append(_warn(
                    "validate.internal", f"live 检查异常: {e}",
                    datasource=name))

        try:
            issues, mounts, n_rules = await _check_decisions(name, kb, ds_dir, dialect)
            report.issues += issues
            report.mounts += mounts
            if n_rules:
                report.counts[f"{name}.rules"] = n_rules
        except Exception as e:  # noqa: BLE001
            logger.exception("validate: 决策规则检查失败 %s", name)
            report.issues.append(_err(
                "validate.internal", f"决策规则检查异常: {e}",
                datasource=name))

    try:
        issues, mounts, counts = _check_skills(root / ".trove" / "skills")
        report.issues += issues
        report.mounts += mounts
        report.counts.update(counts)
    except Exception as e:  # noqa: BLE001
        logger.exception("validate: 技能检查失败")
        report.issues.append(_err("validate.internal", f"技能检查异常: {e}"))

    try:
        # 引用按**显式点名的数据源**解析(不点名 = 静态面,绑定引用留给
        # 套用报告逐条给出 —— preset 的用途正是"套到还没接入的源上",
        # 拿全量数据源逐个判定只会刷屏)。
        issues, mounts, counts = _check_presets(
            root, kb, root / ".trove" / "skills", datasource=datasource)
        report.issues += issues
        report.mounts += mounts
        report.counts.update(counts)
    except Exception as e:  # noqa: BLE001
        logger.exception("validate: preset 检查失败")
        report.issues.append(_err("validate.internal", f"preset 检查异常: {e}"))

    return report
