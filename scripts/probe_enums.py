"""Probe enum values of a datasource and fill them into KB schema_notes.yml.

Usage:
    uv run python scripts/probe_enums.py \
        --datasource mysql://root:root@127.0.0.1:3306/financial
    # 可选: --overwrite 覆盖已有枚举含义; --max-rows 行数护栏(默认 2M)

Writes <cwd>/.trove/kb/<datasource>/schema_notes.yml in place.
已有枚举含义的列保留含义、补齐缺失的探测值(除非 --overwrite)。
"""

import argparse
import asyncio
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.kb.enum_probe import probe_enums, merge_into_notes


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasource", required=True,
                        help="e.g. mysql://root:root@127.0.0.1:3306/financial")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing enum notes (default: keep them)")
    parser.add_argument("--max-rows", type=int, default=2_000_000,
                        help="Skip tables whose row estimate exceeds this")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    registry = ConnectorRegistry()
    await registry.register(
        parse_datasource_url(args.datasource), set_default=True,
    )
    name = registry.default_name or "default"
    schema = await registry.get_schema()
    probed = await probe_enums(registry, schema, max_rows=args.max_rows)
    await registry.close_all()

    if not probed:
        print("没有探测到低基数枚举列。")
        return

    notes_path = Path.cwd() / ".trove" / "kb" / name / "schema_notes.yml"
    if not notes_path.exists():
        print(f"{notes_path} 不存在——请先 /kb init 生成 schema_notes.yml。")
        sys.exit(1)
    notes = yaml.safe_load(notes_path.read_text(encoding="utf-8")) or {}
    merged = merge_into_notes(notes, probed, overwrite=args.overwrite)
    notes_path.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    for table, cols in probed.items():
        for col, values in cols.items():
            print(f"{table}.{col} → {values}")


if __name__ == "__main__":
    asyncio.run(main())
