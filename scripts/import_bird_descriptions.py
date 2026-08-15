"""Import BIRD official database_description CSVs into a KB schema_notes.yml.

Usage:
    uv run python scripts/import_bird_descriptions.py \
        <database_description_dir> <datasource_name>
    # e.g. .../financial/database_description financial

Writes .trove/kb/<datasource>/schema_notes.yml (refuses to overwrite).
"""

import csv
import sys
from pathlib import Path

import yaml

from trove.services.kb.service import KbService


def read_descriptions(csv_path: Path) -> list[dict]:
    rows = []
    with csv_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            desc = (row.get("column_description") or "").strip()
            alias = (row.get("column_name") or "").strip()
            if not desc and alias:
                desc = alias
            enums = (row.get("value_description") or "").strip()
            rows.append({
                "name": (row.get("original_column_name") or "").strip(),
                "description": desc,
                "enums": [enums] if enums else [],
            })
    return rows


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    desc_dir, datasource = Path(sys.argv[1]), sys.argv[2]

    kb = KbService(Path.cwd())
    existing_path = kb.kb_dir / datasource / "schema_notes.yml"
    existing = {}
    if existing_path.exists():
        data = yaml.safe_load(existing_path.read_text(encoding="utf-8")) or {}
        existing = {t["name"]: t for t in data.get("tables", [])}

    # Merge: official column descriptions/enums win; keep existing table
    # descriptions and metrics (official CSVs have neither).
    tables = []
    for csv_path in sorted(desc_dir.glob("*.csv")):
        old = existing.get(csv_path.stem, {})
        old_cols = {c["name"]: c for c in old.get("columns", [])}
        columns = []
        for col in read_descriptions(csv_path):
            if not col["description"]:
                col["description"] = old_cols.get(col["name"], {}).get("description", "")
            if not col["enums"]:
                col["enums"] = old_cols.get(col["name"], {}).get("enums", [])
            columns.append(col)
        tables.append({
            "name": csv_path.stem,
            "description": old.get("description", ""),
            "columns": columns,
            "metrics": old.get("metrics", []),
        })

    kb.init_notes(tables, datasource, overwrite=True)

    total_cols = sum(len(t["columns"]) for t in tables)
    described = sum(
        1 for t in tables for c in t["columns"] if c["description"]
    )
    print(f"已合并 {len(tables)} 张表 / {total_cols} 列（{described} 列有描述）")
    print(f"→ .trove/kb/{datasource}/schema_notes.yml")


if __name__ == "__main__":
    main()
