"""历史蒸馏 —— 用户自己的行为记录 → 待审 KB 资产(示例 / 语义候选 / 教训)。

Usage:
    uv run python scripts/distill_history.py --datasource demo \
        [--limit 50] [--since 2026-01-01T00:00:00+00:00] [--dry-run] [--no-llm]

来源 = 本机行为记录(home 的 ``memory/episodes.sqlite`` 与 ``app.db`` 审计,
项目 ``.trove/lineage/`` 频次;路径白名单在服务层硬拒,尤其 ``eval/``)。
产物全部是 **pending**(示例 / 语义草稿 / 教训),待管理端确认后才生效。
没有可蒸馏的历史 → 显式报无历史并以退出码 1 结束,不报成功。
``--no-llm`` 只跑确定性部分(示例 + 语义候选,零 LLM 成本)。
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from trove.core.config import ConfigLoader
from trove.llm.gateway import LLMGateway
from trove.services.auth.store import AppDbStore
from trove.services.kb.history_distill import (
    MAX_DISTILL_PER_RUN,
    collect_history,
    run_distill,
    run_lesson_distill,
)
from trove.services.kb.service import KbService


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasource", required=True)
    parser.add_argument("--limit", type=int, default=MAX_DISTILL_PER_RUN,
                        help=f"最多消费多少条历史记录(上限 {MAX_DISTILL_PER_RUN})")
    parser.add_argument("--since", default="",
                        help="只取该 ISO 时间之后的记录(如 2026-01-01T00:00:00+00:00)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只算不写(示例/候选/教训都不落盘)")
    parser.add_argument("--no-llm", action="store_true",
                        help="跳过教训提炼(只跑零 LLM 的示例与语义候选)")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    config = ConfigLoader.load_agent_config("conf/agent.yml")
    home = Path(config.home).expanduser()
    project_root = Path.cwd()
    kb = KbService(project_root)

    auth_store = None
    if (home / "app.db").exists():
        auth_store = AppDbStore(home / "app.db")
    try:
        records = await collect_history(
            args.datasource,
            home_dir=home,
            project_root=project_root,
            auth_store=auth_store,
            since=args.since,
            limit=args.limit,
        )
    finally:
        if auth_store is not None:
            await auth_store.dispose()

    if not records:
        print(f"没有可蒸馏的历史(数据源 {args.datasource}"
              f"{f', since {args.since}' if args.since else ''} 没有可用的"
              "行为记录:episodes/审计/lineage 都为空)。")
        sys.exit(1)

    effective = min(args.limit, MAX_DISTILL_PER_RUN)
    print(f"收集到 {len(records)} 条行为记录(上限 {effective}/run)。")

    summary = await run_distill(
        kb, args.datasource, records, dry_run=args.dry_run, max_items=effective,
    )
    tag = "[dry-run] " if args.dry_run else ""
    print(f"{tag}示例:新落 {summary['examples']} 条,"
          f"已有跳过 {summary['examples_skipped']} 条。")
    print(f"{tag}语义候选:新落 {summary['candidates']} 条,"
          f"队列已有跳过 {summary['candidates_skipped']} 条。")
    for item in summary["candidate_items"]:
        print(f"  · {item['kind']} {item['name']} ← {item['question'][:60]}")

    if args.no_llm:
        print("教训提炼已跳过(--no-llm)。")
        return

    llm = LLMGateway(providers=config.providers)
    model = config.model_for_draft("history_distill", "complex")
    # 与「蒸馏管理端」共用同一条提炼管线(history_distill.run_lesson_distill):
    # 提示词、过滤、去重、写入纪律只有一份实现,两侧不会漂移。
    lesson_out = await run_lesson_distill(
        kb, args.datasource, records, llm=llm, model=model,
        dry_run=args.dry_run, max_items=effective,
    )
    if lesson_out["error"]:
        print(f"✗ 教训提炼失败(确定性部分已写入):{lesson_out['error']}")
        sys.exit(1)
    if not lesson_out["material"]:
        print("教训:没有失败/修正记录可提炼。")
        return
    if lesson_out["parse_failed"] or lesson_out["noise"]:
        print(f"(跳过 解析失败 {lesson_out['parse_failed']} / "
              f"管线噪声 {lesson_out['noise']})")
    for item in lesson_out["items"]:
        print(f"· {item['pattern']} → {item['note'][:90]}")
    if not lesson_out["lessons"]:
        print("教训:提炼结果为空,无新条目。")
        return
    if not lesson_out["items"]:
        print("教训:无新条目(全部与已有 pattern 重复)。")
        return
    if args.dry_run:
        print(f"[dry-run] 本应写入 {len(lesson_out['items'])} 条教训(pending)。")
        return
    print(f"教训:写入 {len(lesson_out['items'])} 条(pending),"
          f"跳过 {lesson_out['duplicates']} 条重复。")


if __name__ == "__main__":
    asyncio.run(main())
