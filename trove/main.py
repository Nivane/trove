"""Main entry point for Trove.

Supports multiple modes:
  - trove (REPL): Interactive terminal UI
  - trove-cli --datasource demo: Command-line mode
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from trove.core.config import ConfigLoader, AgentConfig
from trove.core.errors import DatasourceError
from trove.core.logging import get_logger
from trove.storage.session_store import SessionStore
from trove.storage.checkpoint_store import build_checkpointer
from trove.llm.gateway import LLMGateway
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.demo_setup import setup_demo_datasource
from trove.workflow.graphs import GraphServices, build_graphs
from trove.agent.session import SessionManager

logger = get_logger(__name__)


def parse_args(argv: list[str] | None = None):
    """Parse command-line arguments.

    Args:
        argv: Optional argv override (tests); defaults to sys.argv.
    """
    parser = argparse.ArgumentParser(
        description="Trove — Intelligent Data Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--datasource", "-d",
        default="",
        help=(
            "Datasource to use (default: empty — load .trove/datasources.yml; "
            "use demo for the built-in SQLite, or a scheme:// URL)"
        ),
    )
    parser.add_argument(
        "--topic",
        default="",
        help=(
            "Topic domain to scope questions to (declared in the datasource's "
            "semantic model; default: unrestricted). In the REPL this sets "
            "the initial value — /topic shows or changes it"
        ),
    )
    parser.add_argument(
        "--config", "-f",
        default=None,
        help="Path to agent.yml config file",
    )
    parser.add_argument(
        "--model", "-m",
        default=None,
        help="LLM model to use (overrides config)",
    )
    parser.add_argument(
        "--print", "-p",
        action="store_true",
        dest="print_mode",
        help="Print raw JSON output (for pipelining)",
    )
    parser.add_argument(
        "--workflow", "-w",
        default="reflection",
        choices=["reflection", "fixed", "empty"],
        help="Workflow to use (default: reflection)",
    )
    parser.add_argument(
        "--version", "-v",
        action="store_true",
        help="Print version and exit",
    )
    return parser.parse_args(argv)


async def setup_datasource(args, registry: ConnectorRegistry) -> None:
    """Register the --datasource target.

    Accepts:
      - "demo": the built-in BIRD financial demo database
      - scheme:// URLs: sqlite://, mysql://, doris://, clickhouse://,
        duckdb://, snowflake://, bigquery://

    Raises:
        DatasourceError: Unknown target, malformed URL, or connection failure.
    """
    from trove.services.datasource.urls import parse_datasource_url

    target = args.datasource
    if target == "demo":
        await setup_demo_datasource(registry)
    elif "://" in target:
        adapter_config = parse_datasource_url(target)
        await registry.register(adapter_config, set_default=True)
    else:
        raise DatasourceError(
            message=(
                f"Unknown datasource: {target}. "
                f"Use --datasource demo or a scheme:// URL "
                f"(sqlite/mysql/doris/clickhouse/duckdb/snowflake/bigquery)."
            ),
            datasource=target,
        )


async def create_app_components(
    args,
    config: AgentConfig,
    checkpointer=None,
) -> dict:
    """Create and wire together all application components.

    Args:
        args: Parsed command-line arguments.
        config: Loaded AgentConfig.
        checkpointer: Optional LangGraph checkpointer (from build_checkpointer).

    Returns:
        Dict with all initialized components.
    """
    # ── Storage ────────────────────────────────────────────
    session_store = SessionStore(home_dir=config.home)

    # ── Runtime settings (DB overrides applied on top of agent.yml) ──
    # 管理台写入 ~/.trove/settings.db;这里在启动时把已存配置合入运行时
    # AgentConfig(DB 优先,CLI 最优先——顺序见 apply_runtime_overrides)。
    from trove.services.admin_settings.store import SettingsStore

    settings_store = SettingsStore(Path(config.home).expanduser() / "settings.db")
    settings_overrides = await settings_store.get_all()
    apply_runtime_overrides(config, args, settings_overrides)
    # 结果限制镜像进 pipeline 节点可读的进程级注册表(默认 50/1000;
    # DB 覆盖后 config 已改,这里统一同步一次)。
    from trove.services.limits import set_result_limits
    set_result_limits(config.result_max_rows, config.result_display_rows)
    # 置信度开关同样是进程级镜像(output 是模块级函数,拿不到 config)。
    from trove.agent.confidence import set_confidence_enabled
    set_confidence_enabled(config.confidence_score)

    # ── Auth (central app.db: users/tokens/grants/audit) ────
    from trove.services.auth.service import AuthService
    auth = AuthService(Path(config.home).expanduser() / "app.db")
    bootstrap_admin, bootstrap_password = await auth.ensure_bootstrap_admin(
        os.environ.get("TROVE_ADMIN_PASSWORD")
    )

    # ── LLM Gateway ───────────────────────────────────────
    llm_gateway = LLMGateway(providers=config.providers)

    # ── Datasource ────────────────────────────────────────
    from trove.services.datasource.config_store import ConfigStore, boot_register
    config_store = ConfigStore()
    connector_registry = ConnectorRegistry()
    try:
        if args.datasource:
            await setup_datasource(args, connector_registry)
        else:
            failed = await boot_register(
                connector_registry, config_store.load_configs()
            )
            if not connector_registry.list_names() and not failed:
                logger.info(
                    "No datasource configured — register one in the admin UI "
                    "(or start with --datasource demo / a scheme:// URL)."
                )
            if failed:
                logger.warning(
                    "Datasources failed to connect at boot (retry in admin): %s",
                    ", ".join(failed),
                )
    except DatasourceError as e:
        logger.error("Datasource setup failed: %s", e)
        raise

    # ── Catalog ───────────────────────────────────────────
    catalog_service = CatalogService(connector_registry)

    # ── Knowledge base (optional: .trove/kb/) ─────────────
    # 检索后端按数据源读时 dispatch(默认 builtin;datasources.yml 里配了
    # hybrid 走 FTS5/BM25 稀疏,rag 走稀疏 + 稠密 embedding RRF)。
    # 稠密通道:embedder_backend=bge-m3 走本地模型(免凭证),否则经 LLM 网关
    # (embedding_model 为空的数据源 rag 退化为稀疏)。
    from trove.services.kb.backends.dense import build_embedder
    from trove.services.kb.backends.registry import resolver_from_configs
    from trove.services.kb.service import KbService

    def _embedder_for(cfg):
        return build_embedder(cfg, llm_gateway)

    resolve_backend, bind_kb = resolver_from_configs(
        config_store.load_configs(), embedder_factory=_embedder_for)

    # ── Action layer (P3: 模板门 → 提案 → 单人审批 → 命名通道外送) ──
    # 模板是**组织资产**(`.trove/actions/<name>/action.yml`,与 skills 同门:
    # 草稿 → admin 确认才可被规则引用);提案/审批/回执是**运行状态**
    # (ActionStore 落 config.home,与 JobStore/VerdictStore 同根 —— 提案按
    # run_id/job_id 指向 run 行,分家会出现悬空引用)。
    # enabled 默认 false:关着时 propose 与 dispatch 都不可用。
    from trove.services.action import (
        ActionDispatcher,
        ActionService,
        ActionStore,
        ActionTemplateService,
    )

    action_templates = ActionTemplateService(
        Path.cwd() / ".trove" / "actions",
        max_payload_bytes=int(config.action.max_payload_bytes),
    )
    actions = ActionService(
        ActionStore(config.home),
        action_templates,
        # kind 缺省 generic:payload 原样外送(与塑形器不存在时逐字节一致);
        # slack/feishu/dingtalk/wecom 走 services.im 的信封塑形。
        ActionDispatcher(
            {name: {"url": ch.url, "secret": ch.secret, "kind": ch.kind}
             for name, ch in (config.action.channels or {}).items()},
        ),
        enabled=bool(config.action.enabled),
        approval_ttl_hours=int(config.action.approval_ttl_hours),
        max_payload_bytes=int(config.action.max_payload_bytes),
        max_attempts=int(config.action.max_attempts),
        lang=config.language,
    )
    # 护栏走运行时绑定(构造签名由姿态守卫钉死;唯一例外是 B7 的 verifier
    # 可注入,见下):默认档三道全关,配置了 agent.action.max_risk /
    # rate_limit / retry_backoff_* 才生效。
    from trove.services.action.guard import ActionGuards

    actions.guards = ActionGuards.from_config(config.action)

    kb = KbService(Path.cwd(), backend_resolver=resolve_backend,
                   git_kb=config.git_kb, action_templates=action_templates)
    bind_kb(kb)

    # ── Org skill assets (methodology, admin-managed) ──────
    # `.trove/skills/<name>/SKILL.md`:org 级分析方法论资产,LLM 草稿/管理端
    # 确认后才进入 prompt(required 档注入 / available 档 load_skill 按需)。
    # 与 KB 同款「草稿 → 确认」门禁,无技能时零开销(空目录即空 render)。
    from trove.services.skills.service import SkillService
    skills = SkillService(
        Path.cwd() / ".trove" / "skills", llm=llm_gateway,
        # 写路径 git 自动版本化与 KB 同源(config.git_kb);config 是组织扩展
        # 总开关的现读宿主(管理端 PUT 就地改同一个对象)。
        git_enabled=config.git_kb, config=config,
    )

    # ── Preset packs (datasource onboarding templates) ──────
    # 双源:内置 ``trove/presets/<name>`` 随码分发只读,组织
    # ``.trove/presets/<name>`` 由 admin 管理(同名时**组织版遮蔽内置版**)。
    # ``apply`` 只落 pending 草稿(技能门 / 决策草稿 / 语义审批流),
    # 逐条确认后才生效 —— 这条红线由各段的既有写入口保证,preset 自己不写
    # 任何生效文件。
    from trove.services.presets.service import PresetService
    presets = PresetService(
        Path.cwd() / ".trove" / "presets", kb=kb, skills=skills,
        git_enabled=config.git_kb,
    )

    # ── User facts (per-user memory: ~/.trove/user_facts.db) ──
    # 独立于数据源级 KB 的用户级记忆层:偏好/口径事实,按用户+数据源
    # 隔离,注入 gen_sql 个性化上下文(多用户共用时每人有自己的口径)。
    from trove.services.user_facts.service import UserFactsService
    user_facts = UserFactsService(
        Path(config.home).expanduser() / "user_facts.db"
    )

    # ── Unified memory facade (episodes / observations / preferences /
    #    promotion / profile) — ~/.trove/memory/ ──
    # 统一记忆服务:情景记忆(跨会话过去查询)、成功→示例草稿、修正/失败→
    # pending 教训、偏好自动提取、生命周期清理。全部渐进开关(agent.yml
    # 的 agent.memory 段),任何失败静默降级,不阻断查询。
    from trove.services.drift import DriftStore
    from trove.services.memory.service import MemoryService
    # 漂移落库:后台周期巡检此前只把结果打进日志,**在跑但白跑**。接上同一个
    # drift store 之后,巡检与 ``/v1/admin/drift`` 读写的是同一个文件 ——
    # 项目根按 drift 路由那条既有口径反推(``kb_dir`` → ``<root>/.trove/kb``),
    # 两处各推各的会推出两个目录,表现成「巡检跑了但管理端一条都看不到」。
    memory = MemoryService(
        config.home,
        config.memory,
        kb=kb,
        user_facts=user_facts,
        llm=llm_gateway,
        connectors=connector_registry,
        catalog=catalog_service,
        config_resolver=config_store,
        drift_store=DriftStore(Path(kb.kb_dir).parent.parent),
    )

    # ── Data lineage (optional: .trove/lineage/) — definitions.yml lazy
    # sync + executed-query capture; never blocks agent startup.
    from trove.services.lineage.service import LineageService
    lineage = LineageService(Path.cwd())

    # ── Semantic layer (optional: config.semantic_layer_path) ──
    # 单一真源 = 数据源的 KB semantics.yml(kb init 生成 + 人审);配置目录
    # (.trove/semantic/<ds>)只作补充源。KB 有模型或配置目录有文件即启用,
    # 任何初始化失败都不阻断问题流程。
    semantic_layer = None
    try:
        from trove.services.semantic_layer.provider import (
            SemanticLayerProvider,
        )
        adapter = await connector_registry.get()
        ds_name = connector_registry.default_name or "default"
        schema = await adapter.get_schema()
        known_tables = {t.name.lower() for t in schema.tables}
        catalog = {
            t.name.lower(): {c.name.lower() for c in t.columns}
            for t in schema.tables
        }
        semantic_dir = (
            Path.cwd() / config.semantic_layer_path / ds_name
            if config.semantic_layer_path else Path.cwd() / ".trove" / "semantic" / ds_name
        )
        semantic_layer = SemanticLayerProvider(
            directory=semantic_dir,
            datasource=ds_name,
            dialect=adapter.dialect(),
            table_exists=lambda t: t.lower() in known_tables,
            kb_semantics_path=kb.semantics_path(ds_name),
            catalog=catalog,
        )
        if semantic_layer.enabled:
            logger.info(
                "Semantic layer enabled: %s (+KB semantics.yml)",
                semantic_layer.directory)
        else:
            semantic_layer = None
    except Exception as e:
        logger.warning(
            "Semantic layer init failed (%s); continuing without it.", e)
        semantic_layer = None

    # ── Hybrid retrieval indexer (FTS + pgvector, RRF + rerank) ──
    # 默认数据源的混合检索库 + 索引器(CLI `trove index` / 管理端触发)。
    from trove.services.retrieval.factory import build_store
    from trove.services.retrieval.indexer import Indexer

    default_ds_cfg = None
    for _c in config_store.load_configs():
        if getattr(_c, "name", "") == (connector_registry.default_name or "default"):
            default_ds_cfg = _c
            break
    hybrid_store = None
    indexer = None
    if default_ds_cfg is not None:
        try:
            hybrid_store = build_store(default_ds_cfg, llm_gateway, config.home)
            indexer = Indexer(
                hybrid_store, kb, connector_registry, config.home)
        except Exception as e:
            logger.warning("hybrid retrieval unavailable: %s", e)

    # ── Graphs ────────────────────────────────────────────
    services = GraphServices(
        llm=llm_gateway,
        catalog=catalog_service,
        connectors=connector_registry,
        config=config,
        kb=kb,
        user_facts=user_facts,
        semantic_layer=semantic_layer,
        lineage=lineage,
        memory=memory,
        skills=skills,
    )
    graphs = build_graphs(services, checkpointer=checkpointer)

    # ── Session Manager ───────────────────────────────────
    # Langfuse 采用 per-run handler(确定性 trace_id = run_id,见
    # SessionManager._run_config)——不在此创建全局 handler,避免双 trace。

    async def _resolve_user_roles(user_id: str) -> list[str]:
        """user_id → 工具 ACL 角色(gen_sql catalog 工具的可见性裁决)。

        admin 隐含 analyst 能力;其他用户按其声明角色返回(默认 'user'
        拿不到 search_values/lookup_schema/explain_plan 等物理 schema
        探索工具)。查询失败 → 空角色 = 全部工具不可见(保守 fail-closed)。
        """
        try:
            row = await auth.store.get_user_by_id(int(user_id))
        except (TypeError, ValueError):
            return []
        if row is None:
            return []
        if row.get("role") == "admin":
            return ["admin", "analyst"]
        return [row.get("role") or "user"]

    session_manager = SessionManager(
        config=config,
        session_store=session_store,
        graphs=graphs,
        llm_gateway=llm_gateway,
        kb=kb,
        connectors=connector_registry,
        memory=memory,
        role_resolver=_resolve_user_roles,
        auth=auth,
    )

    # ── Maintenance (retention sweeps: daemon tick + serve lifespan) ──
    from trove.services.maintenance import MaintenanceService

    # ── Scheduled jobs (cron/interval + threshold alerts) ──
    # 与 CLI `trove-cli job/schedule` 共用同一存储与服务;serve 内置调度
    # tick(lifespan 后台任务),不再需要另起 daemon。JobStore 落在
    # config.home 下,与 CLI 的 Path.cwd() 语义对齐(prod 容器里即工作目录)。
    from trove.services.jobs.runner import SchedulerRunner
    from trove.services.jobs.service import JobsService
    from trove.services.jobs.store import JobStore

    jobs = JobsService(JobStore(config.home))
    # 订阅投递（定时分析产出物 → 订阅者）：与 JobStore 共用同一个 SQLite ——
    # 订阅行按 job_id 指向任务行，分家会出现「有订阅、没任务」的悬空引用。
    from trove.services.jobs.subscribe import SubscriptionService

    subscriptions = SubscriptionService(jobs.store)
    # `decision` is optional: without it a decision job would have to be
    # reported as an error rather than quietly run as an NL question — the
    # service resolves the semantic model per job.datasource itself, so no
    # datasource-bound provider is injected here.
    from trove.services.decision.service import DecisionService
    from trove.services.decision.verdict_store import VerdictStore

    # 判定历史(P2):与 JobStore 同根 —— verdict 行按 run_id 指向 run 行,
    # 两者分家会出现"历史里有判定、日程里没有那次运行"的悬空引用。
    verdicts = VerdictStore(config.home)

    # 决策规则的执行预算与交互管线同源(budget.timeout_ms):定时任务没有
    # 人在等,无界查询会把 job 的 schedule 永远吊住。具名变量 + 进 components,
    # 是因为装配点**有两处**(serve 的 lifespan 与 CLI `schedule --daemon`)——
    # 两边必须注入同一组件集,parity 测试钉住这一点。
    decision = DecisionService(
        connector_registry, kb, timeout_ms=int(config.budget.timeout_ms),
        # 组织扩展总开关现读:停用中的决策任务以 status=error 报「已停用」,
        # 绝不静默按零规则通过。
        config=config,
    )

    # 闭环验收(B7):效果测量器从 decision 侧装配(**包外执行面** —— 它
    # 复用判定的语义解析与只读执行契约,测量与判定因此读同一套口径),
    # 以 callable 注入行动包。行动包拿到的是一次调用,不是任何能自己伸向
    # 业务库的东西 —— 姿态守卫的例外条款兑现为这一句装配。
    from trove.services.decision.outcome import make_verifier

    actions.verifier = make_verifier(decision, kb=kb)
    # 测量延迟(天)。默认 0 = 测量腿全关(app.py 的 sweep 腿按它门控)。
    actions.outcome_after_days = int(config.action.outcome_after_days or 0)
    # 主动扫描(B6):执行面**注入**而不是交给它连接器 —— scan 包物理上
    # 够不到 registry(姿态守卫钉住),这里给它两条只读通道:一跳执行 +
    # 方言解析,都经 registry 的只读守卫,与判定路径同一条。
    from trove.services.scan.service import ScanService

    async def _scan_hop(sql: str, datasource: str):
        result = await asyncio.wait_for(
            connector_registry.execute(sql, datasource),
            timeout=int(config.budget.timeout_ms) / 1000.0,
        )
        return list(result.columns), list(result.rows)

    async def _scan_dialect(datasource: str) -> str:
        adapter = await connector_registry.get(datasource)
        return getattr(adapter, "dialect", lambda: "")() or ""

    scans = ScanService(
        kb,
        runner=_scan_hop,
        dialect_of=_scan_dialect,
        config=config,
        llm=llm_gateway,
        timeout_ms=int(config.budget.timeout_ms),
    )
    scheduler = SchedulerRunner(
        session_manager, jobs, lang=config.language,
        decision=decision,
        verdicts=verdicts,
        # 触发 + autonomy=propose → 建 pending 提案(best-effort:
        # 提案失败绝不让判定 run 变成 error,见 runner._propose_action)。
        actions=actions,
        # 报告订阅投递(best-effort 同级,见 runner._deliver)。
        subscriptions=subscriptions,
        # 扫描任务(best-effort 同级的报告节;缺席时按 error 记账)。
        scans=scans,
    )

    # ── API 速率限制(进程内令牌桶 + 日配额,按 user)──
    from trove.services.ratelimit import RateLimiter

    rate_limiter = RateLimiter()

    return {
        "config": config,
        "session_store": session_store,
        "settings": settings_store,
        "auth": auth,
        "bootstrap_admin_password": bootstrap_password,
        "llm_gateway": llm_gateway,
        "connector_registry": connector_registry,
        "config_store": config_store,
        "catalog_service": catalog_service,
        "kb": kb,
        "rate_limiter": rate_limiter,
        "user_facts": user_facts,
        "memory": memory,
        "indexer": indexer,
        "hybrid_store": hybrid_store,
        "lineage": lineage,
        "skills": skills,
        "presets": presets,
        "graphs": graphs,
        "session_manager": session_manager,
        "checkpointer": checkpointer,
        "jobs": jobs,
        "scheduler": scheduler,
        "decision": decision,
        "scans": scans,
        "verdicts": verdicts,
        "actions": actions,
        "subscriptions": subscriptions,
        "action_templates": action_templates,
        "maintenance": MaintenanceService(
            session_store, checkpointer, config.retention,
        ),
    }


def format_print_payload(summary: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the --print JSON payload from the stream summary and events.

    Args:
        summary: Terminal event summary (final state essentials).
        events: All stream events collected during the run.

    Returns:
        JSON-serializable payload.
    """
    printable_events = [
        {k: v for k, v in event.items() if k in ("type", "node", "content", "row_count")}
        for event in events
    ]
    return {
        "session_id": summary.get("session_id", ""),
        "response": summary.get("final_response", ""),
        "sql": summary.get("sql", ""),
        "row_count": summary.get("row_count", -1),
        "verdict": summary.get("verdict", ""),
        "error": summary.get("error", ""),
        "kb_hits": summary.get("kb_hits", []),
        "semantics": summary.get("semantics", ""),
        "insights": summary.get("insights", []),
        "conclusion": summary.get("conclusion", ""),
        "hitl_status": summary.get("hitl_status", ""),
        "events": printable_events,
    }


# ── Entry Points ──────────────────────────────────────────


def apply_cli_model_override(config: AgentConfig, args) -> None:
    """把命令行 ``--model`` 盖回 ``config.target``。

    只在 ``apply_runtime_overrides`` 里出现是对的:两个覆盖层谁压谁,由那一个
    函数说了算,不散落在调用点上。调用点各自的顺序反而容易漏。
    """
    if getattr(args, "model", None):
        config.target = args.model


def apply_runtime_overrides(
    config: AgentConfig, args, settings_overrides: dict | None
) -> None:
    """把 agent.yml 之外的两层覆盖依次盖到 ``config`` 上 —— **顺序即契约**。

    1. ``settings.db``(管理台存的运行时设置):赢过 agent.yml。
    2. 命令行 ``--model``:赢过前两层。``_load_config`` 早就应用过一次,这里必须
       **再应用一次**,否则管理台存过默认模型之后 ``--model`` 就被静默压掉 —— 而
       README 承诺的是「CLI ``--model`` > conf/agent.yml」。``apply_overrides`` 的
       docstring 只说「DB wins over yaml」,它本就不该赢过命令行这一次性的显式意图。
    """
    if settings_overrides:
        from trove.services.admin_settings.service import apply_overrides

        apply_overrides(config, settings_overrides)
        logger.info(
            "Applied %d runtime settings overrides from settings.db",
            len(settings_overrides),
        )
    apply_cli_model_override(config, args)


async def _load_config(args) -> AgentConfig:
    """Load .env and config with CLI overrides applied."""
    from dotenv import load_dotenv

    # Explicitly anchor on cwd (load_dotenv() defaults to searching from
    # this file's location, which is wrong for a CLI invoked from a project).
    load_dotenv(Path.cwd() / ".env")
    config = ConfigLoader.load_agent_config(args.config)
    from trove.llm.tracing import configure_tracing
    configure_tracing(config.tracing)
    from trove.tracing.local import configure_trace_store
    configure_trace_store(config.home)
    apply_cli_model_override(config, args)
    return config


async def async_main_repl():
    """Async main for REPL mode."""
    args = parse_args()

    if args.version:
        from trove import __version__
        print(f"Trove v{__version__}")
        return

    config = await _load_config(args)

    async with build_checkpointer(config.home) as checkpointer:
        components = await create_app_components(args, config, checkpointer)

        # Create or load a session
        session_manager = components["session_manager"]
        session = await session_manager.start_session(
            project_cwd=".",
            user_id="local",
        )

        # Launch REPL
        from trove.cli.app import TroveREPL
        repl = TroveREPL(
            session_manager=session_manager,
            config=components["config"],
            catalog_service=components["catalog_service"],
            connector_registry=components["connector_registry"],
            session_store=components["session_store"],
            current_session=session,
            kb_service=components["kb"],
            llm_gateway=components["llm_gateway"],
            user_facts=components["user_facts"],
            # --topic 是 REPL 的初始值;/topic 在会话内查看/修改。
            topic=args.topic,
        )

        try:
            await repl.run()
        finally:
            await repl.cleanup()
            await components["connector_registry"].close_all()


async def _run_index(argv: list[str]) -> None:
    from trove.cli.index_cmds import main_index

    await main_index(argv)


async def async_main_cli():
    """Async main for CLI (non-interactive) mode."""
    if len(sys.argv) > 1 and sys.argv[1] in ("job", "schedule", "maintenance", "index"):
        from trove.cli.maintenance_cmds import main_maintenance
        from trove.cli.schedule_cmds import main_job, main_schedule

        handler = {
            "job": main_job,
            "schedule": main_schedule,
            "maintenance": main_maintenance,
            "index": _run_index,
        }[sys.argv[1]]
        await handler(sys.argv[2:])
        return

    args = parse_args()

    if args.print_mode:
        # Non-interactive: read from stdin, print JSON
        config = await _load_config(args)

        async with build_checkpointer(config.home) as checkpointer:
            components = await create_app_components(args, config, checkpointer)
            session_manager = components["session_manager"]
            session = await session_manager.start_session()

            user_input = sys.stdin.read().strip()
            if user_input:
                events = []
                summary = {}
                async for event in session_manager.ask_stream(
                    session=session,
                    question=user_input,
                    workflow_name=args.workflow,
                    # 透传,不做预检:域是否存在由管线自己响亮拒绝(与前端
                    # 同一条路径),CLI 不自建第二套判定。
                    topic=args.topic,
                ):
                    events.append(event)
                    if "summary" in event:
                        summary = event["summary"]

                import json
                print(json.dumps(
                    format_print_payload(summary, events),
                    ensure_ascii=False, indent=2,
                ))
        return

    # Default: launch REPL
    await async_main_repl()


def main_cli():
    """Entry point for 'trove-cli' command."""
    try:
        asyncio.run(async_main_cli())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)


# ── HTTP API (trove serve) ───────────────────────────────


def serve_parser() -> argparse.ArgumentParser:
    """Argument parser for the 'trove serve' REST API subcommand."""
    parser = argparse.ArgumentParser(
        prog="trove serve",
        description="Run the Trove REST API (uvicorn)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    parser.add_argument(
        "--datasource", "-d", default="",
        help=(
            "Datasource to use (empty — load .trove/datasources.yml; "
            "demo — built-in SQLite; or a scheme:// URL)"
        ),
    )
    parser.add_argument("--config", "-f", default=None, help="Path to agent.yml config file")
    parser.add_argument("--model", "-m", default=None, help="LLM model to use (overrides config)")
    # 没有 --workflow:单轮工作流由 API 请求体逐条指定(/v1/chat 的 workflow
    # 字段,见 session.run(workflow_name=...)),启动参数选不了也不该假装能选。
    return parser


async def async_main_serve(argv: list[str]) -> None:
    """Async main for 'trove serve' (REST API over uvicorn)."""
    import uvicorn

    args = serve_parser().parse_args(argv)
    config = await _load_config(args)
    # HITL interrupt 依赖 checkpointer(REPL/CLI 同样传入);serve 的
    # /resume 端点靠它恢复被打断的图线程,缺失会导致 resume 抛错。
    async with build_checkpointer(config.home) as checkpointer:
        components = await create_app_components(args, config, checkpointer)

        bootstrap_password = components.get("bootstrap_admin_password")
        if bootstrap_password:
            # print + logger 双路:uvicorn 可能吞掉/延迟 stdout 顺序
            print(
                f"\n[!] Bootstrap admin 'admin' created — initial password: "
                f"{bootstrap_password}\n"
                f"    Set TROVE_ADMIN_PASSWORD to control it; change it after login.\n",
                flush=True,
            )
            logger.warning(
                "Bootstrap admin 'admin' created — initial password: %s "
                "(set TROVE_ADMIN_PASSWORD to control it)",
                bootstrap_password,
            )

        from trove.api.app import create_app
        app = create_app(components)
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port))
        try:
            await server.serve()
        finally:
            await components["connector_registry"].close_all()
            jobs = components.get("jobs")
            if jobs is not None:
                try:
                    await jobs.store.dispose()
                except Exception:
                    pass


def main_serve(argv: list[str] | None = None) -> None:
    """Entry point for 'trove serve'."""
    try:
        asyncio.run(async_main_serve(argv if argv is not None else sys.argv[2:]))
    except KeyboardInterrupt:
        sys.exit(0)


# ── MCP server (trove mcp) ───────────────────────────────


def mcp_parser() -> argparse.ArgumentParser:
    """Argument parser for the 'trove mcp' MCP-server subcommand."""
    parser = argparse.ArgumentParser(
        prog="trove mcp",
        description="Run the Trove MCP server (stdio / sse / streamable-http)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Transports:\n"
            "  stdio            (default) stdio — for Claude Desktop / Cursor /\n"
            "                   Claude Code local mounts.\n"
            "  sse|streamable-http  HTTP — mount over the network (Cursor /\n"
            "                   Claude Code remote). Use --token for bearer auth.\n\n"
            "Example (remote, with auth):\n"
            "  trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 \\\n"
            "      --token $TROVE_MCP_TOKEN\n"
        ),
    )
    parser.add_argument(
        "--datasource", "-d", default="",
        help=(
            "Datasource to use (empty — load .trove/datasources.yml; "
            "demo — built-in SQLite; or a scheme:// URL)"
        ),
    )
    parser.add_argument("--config", "-f", default=None, help="Path to agent.yml config file")
    parser.add_argument("--model", "-m", default=None, help="LLM model to use (overrides config)")
    # 同 serve:无 --workflow(它在这里从来没被读过)。
    parser.add_argument(
        "--transport", "-t", default="stdio",
        choices=("stdio", "sse", "streamable-http", "http"),
        help=(
            "MCP transport. stdio (default, local mounts); sse / "
            "streamable-http (alias http) for network mounts."
        ),
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind host for sse / streamable-http (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8001,
        help="Bind port for sse / streamable-http (default: 8001)",
    )
    parser.add_argument(
        "--path", default=None,
        help="Endpoint path for sse / streamable-http (default per transport)",
    )
    parser.add_argument(
        "--token", default=None,
        help=(
            "Optional bearer token required for sse / streamable-http. "
            "When set, HTTP clients must send 'Authorization: Bearer <token>'."
        ),
    )
    return parser


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


async def _mcp_identity_for(
    transport: str, host: str, token: str | None, auth,
) -> dict[str, Any] | None:
    """MCP 通道鉴权:网络绑定必须带 token,且 token 必须是真实用户 token。

    返回解析出的用户 dict(HTTP transport + token 已配置时);stdio 本地
    挂载或 loopback + 无 token → None(不设限)。

    Raises:
        SystemExit: 非 loopback 网络绑定无 token,或 token 无效。
    """
    if transport == "stdio":
        return None
    if host not in _LOOPBACK_HOSTS and not token:
        raise SystemExit(
            "MCP network bind requires --token (bearer auth) — refusing "
            "to expose an open MCP endpoint. Use stdio for local mounts."
        )
    if not token:
        return None
    identity = None
    if auth is not None:
        identity = await auth.resolve_token(token)
    if identity is None:
        raise SystemExit(
            "MCP --token is not a valid Trove user token — create one in "
            "the admin UI (admin → users → tokens) and pass it here."
        )
    return identity


def _build_mcp_auth_middleware(token: str | None):
    """Bearer-token gate for MCP HTTP transports.

    Returns a Starlette middleware that enforces 'Authorization: Bearer
    <token>' on every request when ``token`` is set; passes through
    untouched when no token is configured. Network binds are required to
    set ``--token`` (see async_main_mcp), and the token must resolve to a
    real Trove user so the server can enforce per-user datasource grants.
    health/metadata endpoints are not exempted so that unauthorized
    clients fail fast.
    """
    if not token:
        return None
async def async_main_mcp(argv: list[str]) -> None:
    """Async main for 'trove mcp' (MCP server over stdio / sse / streamable-http)."""
    args = mcp_parser().parse_args(argv)
    config = await _load_config(args)
    async with build_checkpointer(config.home) as checkpointer:
        components = await create_app_components(args, config, checkpointer)
        from trove.mcp.server import build_mcp_server

        transport = args.transport
        # 网络绑定 = 安全边界:非 loopback 必须带 bearer token(fail-closed),
        # 不再默认裸奔;stdio 本地挂载保持无鉴权(本机进程可信)。
        identity = await _mcp_identity_for(
            transport, args.host, args.token, components.get("auth"),
        )
        server = build_mcp_server(components, identity=identity)
        try:
            if transport == "stdio":
                await server.run_stdio_async()
            else:
                middleware = _build_mcp_auth_middleware(args.token)
                http_kwargs: dict[str, Any] = {
                    "transport": "streamable-http" if transport == "http" else transport,
                    "host": args.host,
                    "port": args.port,
                }
                if args.path:
                    http_kwargs["path"] = args.path
                if middleware is not None:
                    http_kwargs["middleware"] = [middleware]
                if args.token:
                    logger.info(
                        "MCP %s listening on %s:%s (bearer auth required)",
                        transport, args.host, args.port,
                    )
                await server.run_http_async(**http_kwargs)
        finally:
            await components["connector_registry"].close_all()


def main_mcp(argv: list[str] | None = None) -> None:
    """Entry point for 'trove mcp'."""
    try:
        asyncio.run(async_main_mcp(argv if argv is not None else sys.argv[2:]))
    except KeyboardInterrupt:
        sys.exit(0)


def main_repl():
    """Entry point for 'trove' command (REPL / serve / job / admin / mcp)."""
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        main_serve()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        main_mcp()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "admin":
        # run_admin_cmds manages its own event loop (asyncio.run inside)
        from trove.cli.admin_cmds import run_admin_cmds
        run_admin_cmds(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("job", "schedule"):
        async def _run_sub():
            from trove.cli.schedule_cmds import main_job, main_schedule

            handler = main_job if sys.argv[1] == "job" else main_schedule
            await handler(sys.argv[2:])

        try:
            asyncio.run(_run_sub())
        except KeyboardInterrupt:
            sys.exit(0)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        # 扩展面干跑校验:零 LLM / 零网络(除非 --live),退出码即结论
        # (0 干净 / 1 硬错误),可直接挂 CI。
        from trove.cli.commands.validate_cmds import main_validate

        sys.exit(main_validate(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "preset":
        # 预设包:list / show / apply —— 套用只落 pending 草稿(确认门在
        # 各服务自身,这里不另开一条写路径)。
        from trove.cli.commands.preset_cmds import main_preset

        sys.exit(main_preset(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "maintenance":
        async def _run_maint():
            from trove.cli.maintenance_cmds import main_maintenance

            await main_maintenance(sys.argv[2:])

        try:
            asyncio.run(_run_maint())
        except KeyboardInterrupt:
            sys.exit(0)
        return
    try:
        asyncio.run(async_main_repl())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)
