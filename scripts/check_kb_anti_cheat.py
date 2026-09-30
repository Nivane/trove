"""KB anti-cheating self-check: verify KB example SQL is not a copy of gold SQL.

CLAUDE.md 里这条约束是文字;这个脚本把它变成退出码,CI 把它变成每次 PR 都会
跑的一道门(见 .github/workflows/eval-gate.yml)。零 LLM、零网络、零数据库。

比对三个递增强度:

  1. normalized string equality (lowercase, no backticks/whitespace)
  2. SQLGlot AST equality (dialect-aware structural identity)
  3. literal-blind structural equality (all literals replaced by a sentinel —
     catches a template that is the gold SQL with values swapped in)

**检查范围 = 所有可检索条目**(``pending`` 为假)。``pending`` 草稿按项目规则
不进检索,故跳过 —— 但跳过条数每次都打印,免得「查了 622 条」被读成「全查了」。

判定分两档,这是刻意的,也是本脚本最容易被误读的地方:

  - ``template: true`` 命中 → **违规,exit 1**。模板是 kb init 确定性生成的,
    命中 gold 只可能是有人塞进去的。
  - 已确认的人工条目命中 → **告警,exit 0**。捕获的成功案例本来就允许接近
    gold(它们就是真实跑出来的),所以不判违规。但会打出来:这条豁免通道同时
    也是抄 gold 最省事的形状(手打一条不带 ``template`` 的 gold),静默的豁免
    等于假绿。告警不挡提交,它只是让这条通道在 CI 日志里留下痕迹。

gold 题集支持两种形状,自动识别:

  - 仓库冻结基线 ``eval/baseline/questions.jsonl`` —— JSONL,SQL 键为 ``gold_sql``
    (**缺省**;CI 只有仓库,没有 ~/hub/trove-design)
  - 设计仓库的 ``test-questions.json`` —— JSON 数组,SQL 键为 ``SQL``

  两者实测 32/32 逐字节一致,所以指向哪个都不改变判定。

Usage:
    uv run python scripts/check_kb_anti_cheat.py            # 仓库内三个 KB
    uv run python scripts/check_kb_anti_cheat.py --kb .trove/kb/demo/examples.yml

退出码:0 = 干净;1 = 模板抄了 gold;2 = **查不成**(请求的 KB 或 gold 缺失)。
沿用 check_drift.py 的约定 —— 查不成的绿是假绿,所以它不记 0。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 缺省 gold = 仓库内冻结基线(CI 里唯一存在的那份)。
DEFAULT_GOLD = REPO_ROOT / "eval" / "baseline" / "questions.jsonl"

#: 缺省检查仓库内全部 KB —— 只查一个的话,另外两个就是无门状态。
DEFAULT_KBS = (
    REPO_ROOT / ".trove" / "kb" / "demo" / "examples.yml",
    REPO_ROOT / ".trove" / "kb" / "financial" / "examples.yml",
    REPO_ROOT / ".trove" / "kb" / "mysql_fin" / "examples.yml",
)


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


def _load_entries(examples_path: Path) -> list[dict]:
    """examples.yml → 带 sql 的条目(可检索与否由调用方按 pending 分)。"""
    data = yaml.safe_load(examples_path.read_text(encoding="utf-8")) or {}
    return [ex for ex in data.get("examples", []) if ex.get("sql")]


def _load_gold(questions_path: Path) -> list[dict]:
    """读取 gold 题集,两种形状都吃;统一归一化成 ``SQL`` 键。

    形状按首个非空白字符判定(``[`` → JSON 数组,否则按 JSONL 逐行),不靠
    文件后缀 —— 设计仓库那份叫 .json,基线那份叫 .jsonl,但两者都可能是任何
    后缀的导出。
    """
    text = questions_path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        records = json.loads(text)
    else:
        records = [
            json.loads(line) for line in text.splitlines() if line.strip()
        ]
    golds: list[dict] = []
    for q in records:
        sql = q.get("SQL") or q.get("gold_sql")
        if sql:
            golds.append({**q, "SQL": sql})
    return golds


def _describe(entry: dict, gold: dict, kind: str, level: str, detail: str) -> str:
    return (
        f"[{kind}:{level}] {detail}\n"
        f"    KB   Q: {str(entry.get('question', ''))[:80]}\n"
        f"    KB SQL: {str(entry.get('sql', ''))[:160]}\n"
        f"    gold Q: {str(gold.get('question', ''))[:80]}\n"
        f"    goldSQL: {str(gold.get('SQL', ''))[:160]}"
    )


class KbResult(NamedTuple):
    """一个 KB 的判定结果(纯数据,不打印 —— 便于单测)。"""

    kb: Path
    checked: int
    skipped: int
    violations: list[str]
    warnings: list[str]


def check(examples_path: Path, golds: list[dict]) -> KbResult:
    """决定一个 KB 是否抄了 gold;纯函数,不做 I/O 之外的任何事。"""
    entries = _load_entries(examples_path)
    retrievable = [e for e in entries if not e.get("pending")]
    skipped = len(entries) - len(retrievable)

    # dict 而非 set:命中时要能报出撞上的是哪道 gold。
    norm_index = {_norm(g["SQL"]): g for g in golds if _norm(g["SQL"])}
    ast_index = {_normalize_ast(g["SQL"]): g for g in golds}
    blind_index = {_normalize_ast(g["SQL"], literal_blind=True): g for g in golds}
    levels = (
        ("1-string", norm_index, lambda s: _norm(s)),
        ("2-ast", ast_index, lambda s: _normalize_ast(s)),
        ("3-structure", blind_index, lambda s: _normalize_ast(s, literal_blind=True)),
    )

    violations: list[str] = []
    warnings: list[str] = []
    for entry in retrievable:
        sql = str(entry.get("sql") or "")
        for level, index, key_of in levels:
            key = key_of(sql)
            if not key or key not in index:
                continue
            kind = "VIOLATION" if entry.get("template") else "WARNING"
            message = _describe(entry, index[key], kind, level, "copies gold SQL")
            (violations if kind == "VIOLATION" else warnings).append(message)
            break

    return KbResult(examples_path, len(retrievable), skipped, violations, warnings)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--kb",
        action="append",
        default=None,
        help="examples.yml 路径(可重复);缺省检查仓库内三个 KB",
    )
    parser.add_argument(
        "--gold",
        default=str(DEFAULT_GOLD),
        help="gold 题集(JSON 数组或 JSONL,自动识别);缺省用仓库冻结基线",
    )
    args = parser.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.exists():
        print(f"gold questions not found: {gold_path}")
        return 2
    golds = _load_gold(gold_path)
    if not golds:
        print(f"gold questions empty (no SQL found): {gold_path}")
        return 2

    kb_paths = [Path(p) for p in args.kb] if args.kb else list(DEFAULT_KBS)
    missing = [p for p in kb_paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"KB examples not found: {p}")
        return 2

    print(f"gold: {len(golds)} questions from {gold_path}")
    total_violations = total_warnings = 0
    for path in kb_paths:
        result = check(path, golds)
        for message in result.violations + result.warnings:
            print(message)
        print(
            f"  {result.kb}: checked {result.checked} retrievable entries "
            f"({result.skipped} pending skipped) → "
            f"{len(result.violations)} violation(s), {len(result.warnings)} warning(s)"
        )
        total_violations += len(result.violations)
        total_warnings += len(result.warnings)

    if total_violations:
        print(
            f"\n{total_violations} anti-cheating violation(s) — "
            f"template SQL copies gold SQL"
        )
        return 1
    if total_warnings:
        print(
            f"\nclean (no template copies gold), but {total_warnings} confirmed "
            f"entry/entries are near-gold — see warnings above"
        )
        return 0
    print("\nclean: no retrievable KB entry matches any gold SQL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
