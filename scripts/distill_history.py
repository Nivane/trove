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
from trove.prompts import render
from trove.services.auth.store import AppDbStore
from trove.services.kb.history_distill import (
    MAX_DISTILL_PER_RUN,
    collect_history,
    history_lesson_evidence,
    is_lesson_material,
    run_distill,
)
from trove.services.kb.lesson_distill import (
    build_distill_prompt,
    dedupe_by_pattern,
    is_noise_lesson,
    parse_lesson,
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

    material = [r for r in records if is_lesson_material(r) and r.question]
    if not material:
        print("教训:没有失败/修正记录可提炼。")
        return
    existing = await kb.list_lessons(args.datasource, confirmed_only=False)
    llm = LLMGateway(providers=config.providers)
    model = config.model_for_draft("history_distill", "complex")
    lessons = []
    for rec in material:
        failure = history_lesson_evidence(rec)
        try:
            response = await llm.chat(
                model=model,
                messages=[
                    {"role": "system", "content": render("lesson_distill/system")},
                    {"role": "user", "content": build_distill_prompt(failure)},
                ],
                max_tokens=16000,
            )
        except Exception as e:
            print(f"✗ 教训提炼失败(确定性部分已写入):{e}")
            sys.exit(1)
        lesson = parse_lesson(response)
        if lesson is None:
            print(f"✗ 解析失败跳过: {rec.question[:60]}")
            continue
        if is_noise_lesson(rec.question, lesson):
            print(f"✗ 管线噪声跳过: {lesson['pattern'][:60]}")
            continue
        lessons.append(lesson)
        print(f"· {lesson['pattern']} → {lesson['note'][:90]}")

    fresh = dedupe_by_pattern(lessons, existing)
    if not fresh:
        print("教训:无新条目(全部与已有 pattern 重复)。")
        return
    if args.dry_run:
        print(f"[dry-run] 本应写入 {len(fresh)} 条教训(pending)。")
        return
    for lesson in fresh:
        await kb.append_lesson(
            {**lesson, "confirmed": False}, args.datasource,
            generator="history_distill",
        )
    print(f"教训:写入 {len(fresh)} 条(pending),"
          f"跳过 {len(lessons) - len(fresh)} 条重复。")


if __name__ == "__main__":
    asyncio.run(main())
