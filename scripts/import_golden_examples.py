"""Import BIRD gold question-SQL pairs as KB few-shot examples.

Half-split experiment: import the FIRST N questions of one database,
evaluate on the rest (no test-set leakage into the examples).

Usage:
    uv run python scripts/import_golden_examples.py \
        <mini_dev_mysql.json> financial --take 16

Appends to .trove/kb/<db_id>/examples.yml (existing entries kept).
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import yaml

from trove.services.kb.service import KbService


def derive_tags(evidence: str) -> list[str]:
    """Crude tags from the evidence hint (short alphanumeric tokens)."""
    tokens = re.findall(r"[A-Za-z][A-Za-z_]{2,12}", evidence or "")
    return list(dict.fromkeys(tokens))[:3]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dev_json")
    parser.add_argument("db_id")
    parser.add_argument("--take", type=int, default=16)
    args = parser.parse_args()

    dev = json.loads(Path(args.dev_json).read_text(encoding="utf-8"))
    questions = [q for q in dev if q.get("db_id") == args.db_id][: args.take]
    if not questions:
        print(f"没有 db_id={args.db_id} 的问题")
        sys.exit(1)

    kb = KbService(Path.cwd())
    path = kb.kb_dir / args.db_id / "examples.yml"
    existing = {}
    if path.exists():
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    examples = list(existing.get("examples", []))
    for q in questions:
        examples.append({
            "question": q["question"],
            "sql": q["SQL"],
            "tags": derive_tags(q.get("evidence", "")),
        })

    kb.init_examples(examples, args.db_id, overwrite=True)
    await kb.force_sync(args.db_id)
    print(f"已导入 {len(questions)} 条 golden examples（共 {len(examples)} 条）→ "
          f".trove/kb/{args.db_id}/examples.yml")


if __name__ == "__main__":
    asyncio.run(main())
