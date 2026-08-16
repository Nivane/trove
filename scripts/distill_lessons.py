"""Eval 失败 → Hint Bank lessons(经验批量制造,冷启动一夜变热)。

Usage:
    uv run python scripts/distill_lessons.py --datasource financial \
        [--limit 5] [--confirm] [--dry-run]

Reads <cwd>/.trove/eval/failures.jsonl (eval_bird 答错的题会写入),
每条失败问 LLM 提炼一条教训,按 pattern 去重后 append 到
.trove/kb/<datasource>/lessons.yml。默认 pending(--confirm 直接确认;
pending 教训可在 /kb 里确认后再注入生成提示词)。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from trove.core.config import ConfigLoader
from trove.llm.gateway import LLMGateway
from trove.services.kb.lesson_distill import (
    DISTILL_SYSTEM,
    build_distill_prompt,
    dedupe_by_pattern,
    parse_lesson,
)
from trove.services.kb.service import KbService

FAILURES_PATH = Path.cwd() / ".trove" / "eval" / "failures.jsonl"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasource", required=True)
    parser.add_argument("--limit", type=int, default=10,
                        help="Only distill the newest N failures")
    parser.add_argument("--confirm", action="store_true",
                        help="Confirm lessons immediately (else pending)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print lessons without writing")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if not FAILURES_PATH.exists():
        print(f"没有失败记录（{FAILURES_PATH} 不存在）。先跑 eval_bird 制造失败。")
        sys.exit(1)
    raw = [l for l in FAILURES_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    failures = [json.loads(l) for l in raw][-args.limit:]

    config = ConfigLoader.load_agent_config("conf/agent.yml")
    llm = LLMGateway(providers=config.providers)
    kb = KbService(Path.cwd())
    existing = await kb.list_lessons(args.datasource, confirmed_only=False)

    lessons = []
    for f in failures:
        response = await llm.chat(
            model=config.target,
            messages=[
                {"role": "system", "content": DISTILL_SYSTEM},
                {"role": "user", "content": build_distill_prompt(f)},
            ],
            max_tokens=300,
        )
        lesson = parse_lesson(response)
        if lesson is None:
            print(f"✗ 解析失败跳过: {f.get('question', '')[:60]}")
            continue
        lessons.append(lesson)
        print(f"· {lesson['pattern']} → {lesson['note'][:90]}")

    fresh = dedupe_by_pattern(lessons, existing)
    if not fresh:
        print("无新教训（全部与已有 pattern 重复）。")
        return
    if args.dry_run:
        print(f"[dry-run] 本应写入 {len(fresh)} 条教训。")
        return
    for lesson in fresh:
        await kb.append_lesson(
            {**lesson, "confirmed": args.confirm}, args.datasource,
        )
    print(f"写入 {len(fresh)} 条教训（confirmed={args.confirm}），"
          f"跳过 {len(lessons) - len(fresh)} 条重复。")


if __name__ == "__main__":
    asyncio.run(main())
