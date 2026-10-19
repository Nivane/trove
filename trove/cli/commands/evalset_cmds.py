"""``trove evalset`` —— 按数据源验收集(evalset)的生成 / 审计 / 运行 / 就绪视图。

    trove evalset generate --datasource demo [--sources semantic,synthetic]
    trove evalset list --datasource demo [--status pending] [--json]
    trove evalset create --datasource demo --question ... --sql ...
    trove evalset confirm <qid> --datasource demo
    trove evalset vouch <qid> --datasource demo --note "口径已核"
    trove evalset run --datasource demo [--tier all] [--yes]
    trove evalset readiness --datasource demo [--json]

服务在 ``trove/services/evalset/``(生成/确认门)与 ``trove/eval/evalset_run.py``
(运行器)——本模块只是入口(与 ``trove validate`` / ``trove preset`` 同一拆分)。

Exit codes(R25):0 成功/成本干跑/ok·empty 的生成;1 ``EvalsetError`` 与生成
失败状态(``invalid_asset | no_semantic_model | no_llm``);2 用法/解析错误
(argparse 自身);``run`` 原样返回运行器的退出码,``--tier all`` 取三次的最大值。

成本护栏:``generate`` 含 ``synthetic`` 或 ``--rewrite`` 而没有 ``--yes`` 时,
只打印将要消耗的模型与题数就返回 0 —— **绝不构造 ``LLMGateway``**(语义生成
是确定性的,不需要 LLM)。``run`` 的成本护栏在运行器里(无 ``--yes`` = dry-run)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from trove.core.config import ConfigLoader
from trove.core.errors import DatasourceError
from trove.core.logging import get_logger
from trove.eval.evalset_run import RunOptions, run_evalset
from trove.llm.gateway import LLMGateway
from trove.services.auth.store import AppDbStore
from trove.services.datasource.config_store import ConfigStore
from trove.services.datasource.demo_setup import setup_demo_datasource
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.evalset.generate import (
    GenerateRequest,
    generate_evalset,
)
from trove.services.evalset.models import EvalsetError
from trove.services.evalset.service import EvalsetService
from trove.services.kb.service import KbService

logger = get_logger(__name__)

#: ``GenerateReport.status`` 里算成功的两个值(R25)。
GENERATE_OK_STATUSES = frozenset({"ok", "empty"})

#: ``--tier all`` 的固定顺序(分数永不跨 tier 合并,这里只决定跑的顺序)。
RUN_TIERS = ("verified", "semantic", "synthetic")


# ── 参数解析 ─────────────────────────────────────────────


def _actor_default() -> str:
    """写操作的 actor 缺省:``TROVE_ACTOR`` → ``"cli"``。"""
    return os.environ.get("TROVE_ACTOR") or "cli"


def _common_parser() -> argparse.ArgumentParser:
    """每个子命令共享的全局参数(放在子命令**之后**也能解析)。"""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--datasource", required=True,
        help="目标数据源(demo / scheme:// URL / .trove/datasources.yml 中的名字)",
    )
    common.add_argument(
        "--project-root", type=Path, default=Path.cwd(),
        help="项目根(默认当前目录;.trove/ 在其下)",
    )
    return common


def _add_actor(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--actor", default=_actor_default(),
        help="操作者(审计/确认记录用;默认 TROVE_ACTOR 或 cli)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trove evalset",
        description=(
            "验收集(evalset):生成 / 待审 / 确认 / 运行 / 就绪视图。"
            "生成与手工新建一律落 pending,确认门一条都不绕。"
        ),
    )
    common = _common_parser()
    sub = parser.add_subparsers(dest="action")

    p = sub.add_parser(
        "generate", parents=[common],
        help="生成候选条目(全部落 pending;含 synthetic 时需 --yes)",
    )
    p.add_argument(
        "--sources", default="semantic",
        help="逗号分隔的生成源:semantic / synthetic / production(默认 semantic)",
    )
    p.add_argument("--limit", type=int, default=30, help="最多生成的题数")
    p.add_argument("--lang", default="zh", help="问题语言(默认 zh)")
    p.add_argument("--since", default="", help="production 源的时间下界(ISO)")
    p.add_argument(
        "--rewrite", action="store_true",
        help="LLM 改写模板问句(一次调用;需 --yes)",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="确认承担 LLM 成本(synthetic 生成 / 改写)",
    )

    p = sub.add_parser(
        "list", parents=[common], help="列出条目(默认一题一行)")
    p.add_argument("--status", default="", help="pending / confirmed / rejected / retired")
    p.add_argument("--tier", default="", help="verified / semantic / synthetic")
    p.add_argument("--source", default="", help="semantic / synthetic / production / manual")
    p.add_argument("--q", default="", help="按问题文本过滤")
    p.add_argument("--json", action="store_true", help="机器可读输出(整份结果)")

    p = sub.add_parser(
        "create", parents=[common], help="手工新建一条 pending 条目")
    p.add_argument("--question", required=True, help="问题原文")
    p.add_argument(
        "--sql", dest="reference_sql", required=True, help="参考 SQL",
    )
    p.add_argument("--tags", default="", help="标签,k=v 逗号分隔")
    p.add_argument("--note", default="", help="备注")
    p.add_argument(
        "--force", action="store_true",
        help="同题不同 SQL 时保留两者(冲突记录 duplicate_of)",
    )
    _add_actor(p)

    p = sub.add_parser("confirm", parents=[common], help="确认一条 pending 条目")
    p.add_argument("qid", help="条目 id")
    p.add_argument("--force", action="store_true", help="duplicate_of 冲突也确认")
    _add_actor(p)

    p = sub.add_parser("reject", parents=[common], help="拒绝一条待审条目(墓碑)")
    p.add_argument("qid", help="条目 id")
    p.add_argument("--reason", default="", help="拒绝原因")
    _add_actor(p)

    p = sub.add_parser(
        "vouch", parents=[common], help="为已确认的生成条目的人工背书(升 verified)")
    p.add_argument("qid", help="条目 id")
    p.add_argument("--note", required=True, help="背书说明(必填)")
    _add_actor(p)

    p = sub.add_parser(
        "retire", parents=[common], help="让已确认条目退出评分(不硬删除)")
    p.add_argument("qid", help="条目 id")
    p.add_argument("--reason", default="", help="退役原因")
    _add_actor(p)

    p = sub.add_parser(
        "run", parents=[common], help="运行验收集(分 tier 落盘;无 --yes = dry-run)")
    p.add_argument(
        "--tier", default="verified",
        help="verified / semantic / synthetic / all(依次跑三个 tier)",
    )
    p.add_argument("--limit", type=int, default=0, help="最多跑 N 题(0 = 全部)")
    p.add_argument(
        "--sample", type=int, default=0, help="稳定抽样 N 题(0 = 不抽样)")
    p.add_argument(
        "--compile-only", action="store_true",
        help="只做 SQLGlot + 试执行自检(零 LLM,不写运行文件)",
    )
    p.add_argument("--yes", action="store_true", help="确认调用 LLM(正式运行)")
    p.add_argument(
        "--strict", action="store_true",
        help="reference_errors/leakage_suspect > 0 时退出码 2",
    )
    p.add_argument("--lang", default="zh", help="问题语言(默认 zh)")

    p = sub.add_parser(
        "readiness", parents=[common], help="只读就绪/覆盖视图(零写入)")
    p.add_argument("--json", action="store_true", help="机器可读输出(整份结果)")

    return parser


def _parse_sources(raw: str) -> list[str]:
    return [name.strip() for name in str(raw or "").split(",") if name.strip()]


def _parse_tags(raw: str) -> dict[str, str]:
    """``k=v,...`` → dict;形状错是**用法**错误(退出码 2),不是静默丢弃。"""
    tags: dict[str, str] = {}
    for chunk in str(raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(
                f"invalid --tags entry {chunk!r} (expected key=value)")
        key, value = chunk.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(
                f"invalid --tags entry {chunk!r} (empty key)")
        tags[key] = value.strip()
    return tags


# ── 运行时装配(R26)──────────────────────────────────────


async def _semantic_layer(root: Path, datasource: str, adapter: Any, kb: Any):
    """按 ``scripts/eval_bird.py`` 的接法建语义层;不可用 → ``None``。

    语义层只影响编译路径的归因与覆盖率,装配失败绝不阻断 CLI(与 eval_bird
    同一取向:打印/记录一行,继续)。
    """
    try:
        from trove.services.semantic_layer.provider import (
            SemanticLayerProvider,
        )

        schema = await adapter.get_schema()
        known_tables = {t.name.lower() for t in schema.tables}
        provider = SemanticLayerProvider(
            directory=root / ".trove" / "semantic" / datasource,
            datasource=datasource,
            dialect=adapter.dialect(),
            table_exists=lambda t: t.lower() in known_tables,
            kb_semantics_path=kb.semantics_path(datasource),
        )
        return provider if provider.enabled else None
    except Exception as exc:  # noqa: BLE001 — 语义层缺席是降级,不是失败
        logger.warning(
            "evalset CLI: semantic layer unavailable for %s: %s",
            datasource, exc,
        )
        return None


async def build_runtime(project_root: Path, datasource: str) -> SimpleNamespace:
    """解析 ``--datasource`` 并装配一次 CLI 运行所需的全部服务。

    解析规则与 ``trove/main.py::setup_datasource`` 同源:``demo`` → 重建内置
    演示库;``scheme://`` URL → 直接注册;其余名字 → ``.trove/datasources.yml``
    中查找(找不到抛与 main.py 同形状的 :class:`DatasourceError`)。

    返回字段 ``config/kb/registry/adapter/semantic_layer/auth_store``。
    **调用方负责** ``await runtime.registry.close_all()``(见 :func:`_close_runtime`)。
    """
    root = Path(project_root)
    config = ConfigLoader.load_agent_config("conf/agent.yml")
    registry = ConnectorRegistry()
    try:
        if datasource == "demo":
            await setup_demo_datasource(registry)
        elif "://" in datasource:
            await registry.register(
                parse_datasource_url(datasource), set_default=True)
        else:
            configs = ConfigStore(
                root / ".trove" / "datasources.yml").load_configs()
            match = next(
                (cfg for cfg in configs if cfg.name == datasource), None)
            if match is None:
                raise DatasourceError(
                    message=(
                        f"Unknown datasource: {datasource}. Use demo, a "
                        f"scheme:// URL, or a name registered in "
                        f".trove/datasources.yml."
                    ),
                    datasource=datasource,
                )
            await registry.register(match, set_default=True)

        adapter = await registry.get()
        kb = KbService(root)
        semantic_layer = await _semantic_layer(root, datasource, adapter, kb)
        app_db = Path.home() / ".trove" / "app.db"
        auth_store = AppDbStore(app_db) if app_db.exists() else None
        return SimpleNamespace(
            config=config,
            kb=kb,
            registry=registry,
            adapter=adapter,
            semantic_layer=semantic_layer,
            auth_store=auth_store,
        )
    except BaseException:
        # 装配到一半失败时调用方拿不到 runtime,也就没人会关连接。
        try:
            await registry.close_all()
        except Exception:  # noqa: BLE001 — 清理不掩盖原始异常
            pass
        raise


async def _close_runtime(runtime: Any) -> None:
    """best-effort 释放 registry(与 auth_store 的后台 worker)。

    brief 的测试替身把 ``registry`` 给成 ``object()`` —— 没有 ``close_all``
    是合法形态(没有连接可关),不能因此把一次成功的命令打成异常。
    """
    close_all = getattr(getattr(runtime, "registry", None), "close_all", None)
    if callable(close_all):
        try:
            await close_all()
        except Exception as exc:  # noqa: BLE001 — 清理失败不改退出码
            logger.warning("evalset CLI: closing connectors failed: %s", exc)
    dispose = getattr(getattr(runtime, "auth_store", None), "dispose", None)
    if callable(dispose):
        try:
            await dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("evalset CLI: closing auth store failed: %s", exc)


# ── 子命令实现 ───────────────────────────────────────────


async def _cmd_generate(args, root: Path, runtime: Any) -> int:
    """生成候选;LLM 成本面先经 ``--yes`` 闸(缺省只打印,零 LLM)。"""
    sources = _parse_sources(args.sources)
    needs_llm = "synthetic" in sources or bool(args.rewrite)
    draft_model = runtime.config.model_for_draft("evalset", "simple")

    if needs_llm and not args.yes:
        rewrite_note = "，含改写" if args.rewrite else ""
        print(
            f"dry-run: 将使用模型 {draft_model} 处理最多 {args.limit} 条问题"
            f"（生成源 {', '.join(sources) or '无'}{rewrite_note}）；"
            "加 --yes 才会调用 LLM。"
        )
        return 0

    llm = None
    if needs_llm:
        llm = LLMGateway(providers=runtime.config.providers)
    request = GenerateRequest(
        datasource=args.datasource,
        sources=sources,
        limit=args.limit,
        lang=args.lang,
        since=args.since,
        rewrite=bool(args.rewrite),
    )
    report = await generate_evalset(
        root,
        request,
        kb=runtime.kb,
        registry=runtime.registry,
        llm=llm,
        draft_model=draft_model,
        semantic_layer=runtime.semantic_layer,
        auth_store=runtime.auth_store,
    )
    _print_json(asdict(report))
    return 0 if report.status in GENERATE_OK_STATUSES else 1


async def _cmd_run(args, root: Path, runtime: Any) -> int:
    """运行验收集;``--tier all`` 依次跑三个 tier 并取最大退出码。"""
    tiers = list(RUN_TIERS) if args.tier == "all" else [args.tier]
    codes: list[int] = []
    for tier in tiers:
        options = RunOptions(
            project_root=root,
            datasource=args.datasource,
            tier=tier,
            limit=args.limit,
            sample=args.sample,
            compile_only=bool(args.compile_only),
            yes=bool(args.yes),
            strict=bool(args.strict),
            lang=args.lang,
            kb=runtime.kb,
            registry=runtime.registry,
            config=runtime.config,
            semantic_layer=runtime.semantic_layer,
        )
        result = await run_evalset(options)
        raw_code = getattr(result, "exit_code", 1)
        codes.append(int(raw_code) if raw_code is not None else 1)
        message = str(getattr(result, "message", "") or "")
        if message:
            prefix = f"[{tier}] " if len(tiers) > 1 else ""
            print(f"{prefix}{message}")
    return max(codes) if codes else 0


async def _cmd_list(args, service: EvalsetService) -> int:
    data = await service.list_items(
        args.datasource,
        status=args.status,
        tier=args.tier,
        source=args.source,
        q=args.q,
    )
    if args.json:
        _print_json(data)
        return 0
    for issue in data.get("issues") or []:
        print(
            f"warning: {issue.get('code', '')}: {issue.get('message', '')}",
            file=sys.stderr,
        )
    for item in data.get("items") or []:
        print(
            f"{item.get('qid', '')} {item.get('status', '')} "
            f"{item.get('tier', '')} {item.get('source', '')} "
            f"{item.get('question', '')}"
        )
    return 0


async def _cmd_readiness(args, service: EvalsetService) -> int:
    data = await service.readiness(args.datasource)
    if args.json:
        _print_json(data)
    else:
        _print_readiness(data)
    return 0


async def _cmd_write(args, service: EvalsetService) -> int:
    """五个写子命令:成功 0,``EvalsetError`` 由 :func:`main_evalset` 收口为 1。"""
    action = args.action
    if action == "create":
        result = await service.create(
            args.datasource,
            question=args.question,
            reference_sql=args.reference_sql,
            tags=args.tags,
            note=args.note,
            actor=args.actor,
            force=args.force,
        )
    elif action == "confirm":
        result = await service.confirm(
            args.datasource, args.qid, actor=args.actor, force=args.force)
    elif action == "reject":
        result = await service.reject(
            args.datasource, args.qid, actor=args.actor, reason=args.reason)
    elif action == "vouch":
        result = await service.vouch(
            args.datasource, args.qid, actor=args.actor, note=args.note)
    else:  # retire
        result = await service.retire(
            args.datasource, args.qid, actor=args.actor, reason=args.reason)
    _print_json(result)
    return 0


async def _dispatch(args) -> int:
    root = Path(args.project_root)
    runtime = None
    try:
        runtime = await build_runtime(root, args.datasource)
        if args.action == "generate":
            return await _cmd_generate(args, root, runtime)
        if args.action == "run":
            return await _cmd_run(args, root, runtime)
        service = EvalsetService(
            root,
            kb=runtime.kb,
            registry=runtime.registry,
            semantic_layer=runtime.semantic_layer,
            config=runtime.config,
        )
        if args.action == "list":
            return await _cmd_list(args, service)
        if args.action == "readiness":
            return await _cmd_readiness(args, service)
        return await _cmd_write(args, service)
    finally:
        if runtime is not None:
            await _close_runtime(runtime)


# ── 输出 ─────────────────────────────────────────────────


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _unknown(value: Any) -> str:
    """None = 读不到/未测,按 ``unknown`` 打印 —— 绝不洗成 0(R24)。"""
    return "unknown" if value is None else str(value)


def _print_readiness(data: dict[str, Any]) -> None:
    print(f"datasource: {data.get('datasource', '')}")
    print(f"available: {data.get('available')}")
    print(f"evalset_rev: {_unknown(data.get('evalset_rev'))}")

    counts = data.get("counts")
    if counts is None:
        print("counts: unknown")
    else:
        print("counts: " + " ".join(f"{k}={v}" for k, v in counts.items()))

    by_tier = data.get("by_tier")
    if by_tier is None:
        print("by_tier: unknown")
    else:
        for tier, row in by_tier.items():
            print(
                f"by_tier.{tier}: "
                f"confirmed={_unknown(row.get('confirmed'))} "
                f"pending={_unknown(row.get('pending'))}"
            )

    coverage = data.get("coverage")
    if coverage is None:
        print("coverage: unknown")
    else:
        print(
            "coverage: "
            + json.dumps(coverage, ensure_ascii=False, sort_keys=True)
        )

    latest = data.get("latest_runs") or {}
    for tier in RUN_TIERS:
        summary = latest.get(tier)
        if summary is None:
            print(f"latest_runs.{tier}: unknown")
        else:
            print(
                f"latest_runs.{tier}: "
                + json.dumps(summary, ensure_ascii=False, sort_keys=True)
            )

    for entry in data.get("degraded") or []:
        print(f"degraded: {entry.get('code', '')}: {entry.get('message', '')}")


def _print_evalset_error(exc: EvalsetError) -> None:
    print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
    for issue in exc.issues:
        print(
            f"  {json.dumps(issue, ensure_ascii=False, sort_keys=True)}",
            file=sys.stderr,
        )


# ── 入口 ─────────────────────────────────────────────────


def main_evalset(argv: list[str]) -> int:
    """``trove evalset`` 入口 —— 返回进程退出码(不自行 ``sys.exit``)。"""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        # argparse:--help → 0;用法错误 → 2。入口只返回码,不把 SystemExit
        # 漏给调用方(``trove main`` 会把它当退出码,测试直接读返回值)。
        return exc.code if isinstance(exc.code, int) else 2

    if not getattr(args, "action", None):
        parser.print_help()
        return 2

    if args.action == "create":
        try:
            args.tags = _parse_tags(args.tags)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except EvalsetError as exc:
        _print_evalset_error(exc)
        return 1
    except DatasourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


__all__ = ["build_runtime", "main_evalset"]
