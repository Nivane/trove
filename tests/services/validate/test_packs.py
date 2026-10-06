"""``trove validate --packs`` —— 反作弊检查器(KB 示例 SQL vs 本地 gold 集)。

这个检查器的姿态在测试里必须被钉死(实施稿 §04 E5 / R8):

- **只警告**:命中也只是 warning(``kb.gold``),绝不产出硬错误、绝不
  拒绝整次校验、绝不改写 KB(红线:手动塞的 gold 不可自动修,发现器只管
  发现);
- **缺省如实报 skipped**:没有 gold 集就是"未检查",而"未检查"绝不能与
  "检查过且干净"长得一样 —— 前者出警告且没有 ``{ds}.gold`` 计数;
- **provenance 可查**:命中要说清哪条示例、命中哪个文件、第几条语句。

零 LLM / 零网络:gold 集是 tmp 目录里的一个本地文件。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trove.services.validate import run_validate

_SEMANTICS = {
    "semantic_model": [{
        "name": "mini",
        "datasets": [{
            "name": "loan",
            "source": "loan",
            "primary_key": ["loan_id"],
            "fields": [
                {"name": "loan_id", "datatype": "Integer",
                 "description": "Loan id.",
                 "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "loan_id"}]}},
                {"name": "amount", "datatype": "Decimal",
                 "description": "Loan amount.",
                 "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "amount"}]}},
            ],
        }],
        "metrics": [{
            "name": "total_amount",
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "SUM(loan.amount)"}]},
        }],
    }],
}

_SCHEMA_NOTES = {
    "tables": [{
        "name": "loan",
        "description": "Loans.",
        "columns": [{"name": "loan_id", "description": "Loan id."},
                    {"name": "amount", "description": "Loan amount."}],
    }],
}

_GOLD_SQL = "SELECT SUM(amount) FROM loan\n"

_EXAMPLES = {"examples": [
    {"question": "How many loans are there?", "sql": "SELECT COUNT(loan_id) FROM loan"},
]}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")


def _make_kb(tmp_path: Path, datasource: str = "mini", *,
             examples: dict | None = None, gold: str | None = _GOLD_SQL) -> Path:
    ds_dir = tmp_path / ".trove" / "kb" / datasource
    ds_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(ds_dir / "semantics.yml", _SEMANTICS)
    _write_yaml(ds_dir / "schema_notes.yml", _SCHEMA_NOTES)
    if examples is not None:
        _write_yaml(ds_dir / "examples.yml", examples)
    if gold is not None:
        (ds_dir / "gold.sql").write_text(gold, encoding="utf-8")
    return ds_dir


def _gold_issues(report):
    return [i for i in report.issues if i.check == "kb.gold"]


# ── 开关:不打开就没有这一节 ─────────────────────────────


async def test_packs_off_means_no_gold_section(tmp_path):
    """``packs=False`` 时这一节根本不存在(没有 gold 文件也不该报 skipped)。"""
    _make_kb(tmp_path, gold=None, examples=_EXAMPLES)
    report = await run_validate("mini", project_root=tmp_path, packs=False)
    assert _gold_issues(report) == []
    assert "mini.gold" not in report.counts


# ── 缺省:如实报 skipped ─────────────────────────────────


async def test_missing_gold_reports_skipped(tmp_path):
    """没有 gold 集 → 警告说明"未检查 ≠ 干净",且**不留**已检查计数。"""
    _make_kb(tmp_path, gold=None, examples=_EXAMPLES)
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    issues = _gold_issues(report)
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "跳过" in issues[0].message and "未检查" in issues[0].message
    assert "gold.sql" in issues[0].message
    assert "mini.gold" not in report.counts
    assert report.exit_code() == 0          # 跳过不是失败


async def test_empty_gold_reports_skipped(tmp_path):
    _make_kb(tmp_path, gold="", examples=_EXAMPLES)
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    issues = _gold_issues(report)
    assert len(issues) == 1 and "为空" in issues[0].message
    assert "mini.gold" not in report.counts


# ── 命中:警告 + provenance(绝不拒载) ───────────────────


async def test_similar_example_warns_with_provenance(tmp_path):
    """示例 SQL 与 gold 高度相似 → warning,点名问题 / 文件 / 第几条语句。"""
    _make_kb(tmp_path, examples={"examples": [
        {"question": "What is the total loan amount?", "sql": _GOLD_SQL.strip()},
    ]})
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    issues = _gold_issues(report)
    assert len(issues) == 1
    msg = issues[0].message
    assert issues[0].severity == "warning"
    assert "What is the total loan amount?" in msg
    assert "第 1 条" in msg and "gold.sql" in msg
    assert issues[0].datasource == "mini"
    assert report.counts["mini.gold_hits"] == 1
    assert report.counts["mini.gold"] == 1     # 查过几条 gold 语句
    # 只警告:没有硬错误,默认退出码 0(--strict 才拦)
    assert report.errors == []
    assert report.exit_code() == 0
    assert report.exit_code(strict=True) == 1


async def test_hit_survives_formatting_differences(tmp_path):
    """大小写 / 空白 / 结尾分号不同但语义相同 → 仍命中(规范化在起作用)。"""
    _make_kb(tmp_path, examples={"examples": [
        {"question": "Q", "sql": "  select   sum( amount )  from loan ; "},
    ]})
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    assert len(_gold_issues(report)) == 1
    assert report.counts["mini.gold_hits"] == 1


async def test_hit_is_not_refusal_and_does_not_rewrite_kb(tmp_path):
    """命中绝不自动拒载、绝不改写 KB —— examples.yml 一个字节不动。"""
    examples = {"examples": [
        {"question": "Q", "sql": _GOLD_SQL.strip()},
    ]}
    _make_kb(tmp_path, examples=examples)
    path = tmp_path / ".trove" / "kb" / "mini" / "examples.yml"
    before = path.read_bytes()
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    assert report.ok                                   # 无硬错误
    assert path.read_bytes() == before


# ── 无关:不响 ────────────────────────────────────────────


async def test_unrelated_example_stays_silent(tmp_path):
    """题型/内容无关的示例不出声 —— 而且"查过且干净"要有计数为证。"""
    _make_kb(tmp_path, examples=_EXAMPLES)
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    assert _gold_issues(report) == []
    assert report.counts["mini.gold"] == 1
    assert "mini.gold_hits" not in report.counts


async def test_related_but_different_sql_below_threshold(tmp_path):
    """同表同列换聚合(SUM vs MAX,规范化后相似度 0.93)够不上 0.95 → 不误报。

    "相关"不是"相同":这类命中一旦报出来就稀释真命中(反作弊告警要吃人工
    裁定,信噪比就是它的全部价值)。
    """
    _make_kb(tmp_path, examples={"examples": [
        {"question": "Q", "sql": "SELECT MAX(amount) FROM loan"},
    ]})
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    assert _gold_issues(report) == []
    assert report.counts["mini.gold"] == 1        # 确实查过了,只是没命中


async def test_example_without_sql_is_ignored(tmp_path):
    """示例缺 sql 字段 → 反作弊这条跳过(别的检查照常报它,不归这里管)。"""
    _make_kb(tmp_path, examples={"examples": [
        {"question": "只有问题没有 SQL"},
        {"question": "Q", "sql": "SELECT 1 FROM loan"},
    ]})
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    assert _gold_issues(report) == []
    assert not any(i.check == "validate.internal" for i in report.issues)


async def test_garbage_gold_file_does_not_crash(tmp_path):
    """gold 文件是垃圾文本 → 降级比较,不抛异常(发现器不做成新的故障源)。"""
    _make_kb(tmp_path, gold="this is not sql;;;@@@\n", examples=_EXAMPLES)
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    assert not any(i.check == "validate.internal" for i in report.issues)
    assert _gold_issues(report) == [] or all(
        i.severity == "warning" for i in _gold_issues(report))


async def test_gold_with_comments_and_multiple_statements(tmp_path):
    """gold 可含注释与多条语句;计数按语句条数(而不是行数)。"""
    _make_kb(tmp_path, gold=(
        "-- 本题不是 KB 内容,仅本地比对\n"
        "SELECT SUM(amount) FROM loan;\n"
        "SELECT COUNT(loan_id) FROM loan;\n"), examples=_EXAMPLES)
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    hits = _gold_issues(report)
    # 示例 = COUNT(loan_id) FROM loan,与第二条语句高度相似
    assert len(hits) == 1
    assert report.counts["mini.gold"] == 2


async def test_multi_statement_gold_names_statement_index(tmp_path):
    """命中要能指到 gold 的第几条语句(provenance 的粒度)。"""
    _make_kb(tmp_path, gold=(
        "SELECT COUNT(loan_id) FROM loan;\n"
        "SELECT SUM(amount) FROM loan;\n"), examples={"examples": [
            {"question": "Q", "sql": "SELECT SUM(amount) FROM loan"},
        ]})
    report = await run_validate("mini", project_root=tmp_path, packs=True)
    assert "第 2 条" in _gold_issues(report)[0].message


# ── 显式 --gold ──────────────────────────────────────────


async def test_explicit_gold_applies_to_single_datasource(tmp_path):
    _make_kb(tmp_path, gold=None, examples={"examples": [
        {"question": "Q", "sql": _GOLD_SQL.strip()},
    ]})
    outside = tmp_path / "elsewhere.sql"
    outside.write_text(_GOLD_SQL, encoding="utf-8")
    report = await run_validate("mini", project_root=tmp_path, packs=True,
                                gold=str(outside))
    issues = _gold_issues(report)
    assert len(issues) == 1 and "第 1 条" in issues[0].message
    assert report.counts["mini.gold"] == 1


async def test_explicit_gold_with_multiple_datasources_is_skipped(tmp_path):
    """一份 gold 文件对不上多个源 → 如实报跳过,不乱判。"""
    _make_kb(tmp_path, datasource="mini", gold=None, examples=_EXAMPLES)
    _make_kb(tmp_path, datasource="other", gold=None, examples=_EXAMPLES)
    outside = tmp_path / "elsewhere.sql"
    outside.write_text(_GOLD_SQL, encoding="utf-8")
    report = await run_validate(project_root=tmp_path, packs=True,
                                gold=str(outside))
    issues = _gold_issues(report)
    assert len(issues) == 1
    assert "只在该次校验恰好一个数据源时可用" in issues[0].message
    assert "mini.gold" not in report.counts


async def test_missing_explicit_gold_reports_skipped(tmp_path):
    _make_kb(tmp_path, gold=None, examples=_EXAMPLES)
    report = await run_validate("mini", project_root=tmp_path, packs=True,
                                gold=str(tmp_path / "nope.sql"))
    issues = _gold_issues(report)
    assert len(issues) == 1 and "nope.sql" in issues[0].message


# ── 多数据源:逐个查,缺省逐个报 ─────────────────────────


async def test_each_datasource_gets_its_own_verdict(tmp_path):
    _make_kb(tmp_path, datasource="withgold", examples={"examples": [
        {"question": "Q", "sql": _GOLD_SQL.strip()}]}, gold=_GOLD_SQL)
    _make_kb(tmp_path, datasource="nogold", examples=_EXAMPLES, gold=None)
    report = await run_validate(project_root=tmp_path, packs=True)
    assert report.counts["withgold.gold_hits"] == 1
    assert "nogold.gold" not in report.counts
    assert any(i.datasource == "nogold" and "跳过" in i.message
               for i in _gold_issues(report))


async def test_single_datasource_selection_limits_the_check(tmp_path):
    """``--datasource`` 只查那一个源(gold 计数也只留它)。"""
    _make_kb(tmp_path, datasource="withgold", examples=_EXAMPLES, gold=_GOLD_SQL)
    _make_kb(tmp_path, datasource="nogold", examples=_EXAMPLES, gold=None)
    report = await run_validate("withgold", project_root=tmp_path, packs=True)
    assert report.counts["withgold.gold"] == 1
    assert "nogold.gold" not in report.counts
    assert all(i.datasource != "nogold" or i.check != "kb.gold"
               for i in report.issues)
