"""KB 禁背题检查器:今天干净不算数,得**能红**才算数。

这个脚本现在是 CI 的一道门(.github/workflows/eval-gate.yml)。门最坏的失败
形态不是报错,是「永远不会红」—— 比如有人把三级比对简化成一级、把 pending
豁免写成了全豁免。所以这里钉住的是**机制**:每一级都能抓、两个豁免档各是
哪一档、跳过多少条。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from scripts.check_kb_anti_cheat import DEFAULT_GOLD, check, _load_gold

GOLD_SQL = "SELECT COUNT(*) FROM account WHERE region = 'east Bohemia'"
GOLD = [{"question": "How many accounts in East Bohemia?", "SQL": GOLD_SQL}]


def _kb(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "examples.yml"
    path.write_text(
        yaml.safe_dump({"examples": entries}, allow_unicode=True), encoding="utf-8"
    )
    return path


def _entry(sql: str, **extra) -> dict:
    return {"question": "q", "sql": sql, **extra}


# ── 三级各自都能红 ──────────────────────────────────────────


def test_level1_exact_copy_as_template_fails(tmp_path):
    result = check(_kb(tmp_path, [_entry(GOLD_SQL, template=True)]), GOLD)
    assert len(result.violations) == 1
    assert "1-string" in result.violations[0]


def test_level1_ignores_case_backticks_and_whitespace(tmp_path):
    """归一化的意义:换个大小写/反引号/换行,抄还是抄。"""
    sql = "select  count(*)\n  from `account`\n  where region='east bohemia';"
    result = check(_kb(tmp_path, [_entry(sql, template=True)]), GOLD)
    assert len(result.violations) == 1, result.violations


def test_level2_catches_reformatted_sql(tmp_path):
    """一级漏掉的(标点/括号间距不同)由 AST 级抓住。"""
    sql = "SELECT COUNT( * ) FROM account WHERE region='east Bohemia'"
    result = check(_kb(tmp_path, [_entry(sql, template=True)]), GOLD)
    assert len(result.violations) == 1
    assert "2-ast" in result.violations[0]


def test_level3_catches_swapped_literals(tmp_path):
    """把 gold 的取值换掉是最像「自己写的」的抄法 —— 只有盲化结构能抓。"""
    sql = GOLD_SQL.replace("east Bohemia", "west Bohemia")
    result = check(_kb(tmp_path, [_entry(sql, template=True)]), GOLD)
    assert len(result.violations) == 1
    assert "3-structure" in result.violations[0]


# ── 两个豁免档:各是各的,不能互相渗透 ──────────────────────


def test_confirmed_entry_warns_but_does_not_fail(tmp_path):
    """捕获的成功案例允许接近 gold —— 但必须留痕,不能静默豁免。"""
    result = check(_kb(tmp_path, [_entry(GOLD_SQL, template=False)]), GOLD)
    assert result.violations == []
    assert len(result.warnings) == 1
    assert "WARNING" in result.warnings[0]


def test_pending_draft_is_skipped_entirely(tmp_path):
    """pending 不进检索,故不判 —— 但跳过条数要数出来。"""
    path = _kb(tmp_path, [_entry(GOLD_SQL, template=True, pending=True)])
    result = check(path, GOLD)
    assert result.violations == [] and result.warnings == []
    assert (result.checked, result.skipped) == (0, 1)


def test_harmless_template_is_clean(tmp_path):
    result = check(
        _kb(tmp_path, [_entry("SELECT COUNT(*) FROM client", template=True)]), GOLD
    )
    assert result.violations == [] and result.warnings == []
    assert (result.checked, result.skipped) == (1, 0)


def test_empty_entries_are_ignored(tmp_path):
    """没有 sql 的条目(占位/纯注释)不该被当成候选。"""
    result = check(_kb(tmp_path, [{"question": "q"}]), GOLD)
    assert result.checked == 0


# ── gold 两种形状 ───────────────────────────────────────────


def test_gold_jsonl_shape(tmp_path):
    path = tmp_path / "g.jsonl"
    path.write_text('{"qid": "a", "gold_sql": "SELECT 1"}\n', encoding="utf-8")
    assert _load_gold(path)[0]["SQL"] == "SELECT 1"


def test_gold_json_array_shape(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('[{"question_id": 1, "SQL": "SELECT 1"}]', encoding="utf-8")
    assert _load_gold(path)[0]["SQL"] == "SELECT 1"


def test_gold_rows_without_sql_are_dropped(tmp_path):
    path = tmp_path / "g.jsonl"
    path.write_text('{"question": "no sql"}\n{"gold_sql": "SELECT 2"}\n', encoding="utf-8")
    golds = _load_gold(path)
    assert len(golds) == 1 and golds[0]["SQL"] == "SELECT 2"


def test_repo_baseline_is_loadable():
    """仓库冻结基线是 CI 的缺省 gold —— 它读不出来,门就是空的。"""
    golds = _load_gold(DEFAULT_GOLD)
    assert len(golds) >= 30
    assert all(g["SQL"].strip() for g in golds)
