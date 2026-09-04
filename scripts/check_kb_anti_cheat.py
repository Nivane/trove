"""KB anti-cheating self-check: verify KB template SQL is not a copy of gold SQL.

Compares every `template: true` SQL in a KB's examples.yml against the gold SQL
of a BIRD question file. Fails (exit 1) if any template matches a gold SQL at
three increasing-strength levels:

  1. normalized string equality (lowercase, no backticks/whitespace)
  2. SQLGlot AST equality (dialect-aware structural identity)
  3. literal-blind structural equality (all literals replaced by a sentinel —
     catches a template that is the gold SQL with values swapped in)

Only template entries are checked (deterministic/hand-authored skeletons).
Pending auto-captured examples and hand-confirmed examples are allowed to be
near-gold by design (they ARE captured successes) and are skipped.

Usage:
    uv run python scripts/check_kb_anti_cheat.py \
        --kb examples.yml --gold /path/to/test-questions.json
    # exit 0 = clean; exit 1 = a template copies gold SQL
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


def _norm(text: str) -> str:
    """Lowercase, strip backticks, collapse whitespace, strip trailing ';'."""
    t = (text or "").lower()
    t = re.sub(r"`", "", t)
    t = re.sub(r"\s+", " ", t)
    t = t.strip().rstrip(";").strip()
    return t


def _normalize_ast(sql: str, literal_blind: bool = False) -> str:
    """SQLGlot → canonical SQL string; optional literal-blind (all literals).

    Falls back to normalized-text comparison when sqlglot is unavailable or
    the SQL fails to parse (e.g. template placeholder `{{var}}`).
    """
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return _norm(sql)
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except Exception:
        return _norm(sql)
    if literal_blind:
        for node in list(tree.walk()):
            if isinstance(node, exp.Literal):
                node.replace(exp.Literal.string("__LIT__"))
    try:
        return _norm(tree.sql(dialect="mysql"))
    except Exception:
        return _norm(sql)


def _load_templates(examples_path: Path) -> list[dict]:
    data = yaml.safe_load(examples_path.read_text(encoding="utf-8")) or {}
    templates = []
    for ex in data.get("examples", []):
        if ex.get("template") and not ex.get("pending"):
            templates.append(ex)
    return templates


def _load_gold(questions_path: Path) -> list[dict]:
    questions = json.loads(questions_path.read_text(encoding="utf-8"))
    golds = [q for q in questions if q.get("SQL")]
    return golds


def _report_violation(tmpl: dict, gold: dict, level: str, detail: str) -> bool:
    print(
        f"[VIOLATION:{level}] template Q: {tmpl.get('question','')[:80]}\n"
        f"    template SQL: {tmpl.get('sql','')[:160]}\n"
        f"    gold Q: {gold.get('question','')[:80]}\n"
        f"    gold SQL: {gold.get('SQL','')[:160]}\n"
        f"    {detail}"
    )
    return True


def check(examples_path: Path, gold_path: Path) -> list[str]:
    """Return list of violation messages; empty = clean."""
    templates = _load_templates(examples_path)
    golds = _load_gold(gold_path)
    violations: list[str] = []

    normed_golds = {g["SQL"]: _norm(g["SQL"]) for g in golds}
    ast_golds = {g["SQL"]: _normalize_ast(g["SQL"]) for g in golds}
    blind_golds = {g["SQL"]: _normalize_ast(g["SQL"], literal_blind=True) for g in golds}

    for tmpl in templates:
        t_sql = tmpl.get("sql", "")
        t_norm = _norm(t_sql)
        t_ast = _normalize_ast(t_sql)
        t_blind = _normalize_ast(t_sql, literal_blind=True)

        for g in golds:
            g_sql = g["SQL"]
            # Level 1: normalized string equality
            if t_norm and t_norm == normed_golds[g_sql]:
                violations.append(f"LEVEL-1 {tmpl['question'][:60]!r}")
                _report_violation(tmpl, g, "1-string", "normalized text identical")
            # Level 2: SQLGlot AST equality
            elif t_ast and t_ast == ast_golds[g_sql]:
                violations.append(f"LEVEL-2 {tmpl['question'][:60]!r}")
                _report_violation(tmpl, g, "2-ast", "AST-identical")
            # Level 3: literal-blind structural equality (template = gold with
            # values/table aliases swapped, e.g. {{region}} vs 'east Bohemia').
            elif t_blind and t_blind == blind_golds[g_sql]:
                violations.append(f"LEVEL-3 {tmpl['question'][:60]!r}")
                _report_violation(tmpl, g, "3-structure", "structure identical after literals removed")

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", default=".trove/kb/mysql_fin/examples.yml")
    parser.add_argument("--gold", default="/Users/zhaolipan/hub/trove-design/test-questions.json")
    args = parser.parse_args()

    examples_path = Path(args.kb)
    gold_path = Path(args.gold)
    if not examples_path.exists():
        print(f"KB examples not found: {examples_path}")
        return 2
    if not gold_path.exists():
        print(f"gold questions not found: {gold_path}")
        return 2

    templates = _load_templates(examples_path)
    golds = _load_gold(gold_path)
    print(f"checked {len(templates)} templates vs {len(golds)} gold questions")

    violations = check(examples_path, gold_path)
    if violations:
        print(f"\n{len(violations)} anti-cheating violation(s) — templates copy gold SQL")
        return 1
    print("clean: no template matches any gold SQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
