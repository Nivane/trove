"""``trove validate`` 的验收集(evalset)静态 lint。

钉住四件事:

- **issue code 就是 CI 过滤键**:解析问题原样转成 ``evalset.<code>``,内容
  问题各有一枚稳定 code(``confirmed_no_sql`` / ``confirmed_shape`` /
  ``sql_invalid`` / ``leakage`` / ``no_kb``);
- **墓碑不参与**:rejected / retired 条目留在资产里(去重语义),但静态 lint
  不检查它们的 SQL/形状/泄漏;
- **severity 跟着状态走**:同一处泄漏,confirmed 是 error(会被算进评分),
  pending 只是 warning(还没进集);
- **evalset-only 数据源也要被看见**:验收集可以先建在还没有 KB 的源上(先
  攒题、后接 KB),这种情况必须警告 ``evalset.no_kb`` —— 不能因为在循环开头
  发现 KB 目录不存在就被静默跳过。

零 LLM / 零网络:全部是 tmp 目录里的本地 YAML。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from trove.services.validate import run_validate


def _write_examples(tmp_path: Path, datasource: str, questions: list[str]) -> Path:
    ds_dir = tmp_path / ".trove" / "kb" / datasource
    ds_dir.mkdir(parents=True, exist_ok=True)
    path = ds_dir / "examples.yml"
    path.write_text(
        yaml.safe_dump(
            {"examples": [{"question": q, "sql": "SELECT 1"} for q in questions]},
            allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return path


def _write_evalset(tmp_path: Path, datasource: str, items: str) -> Path:
    ev = tmp_path / ".trove" / "evalsets" / datasource
    ev.mkdir(parents=True, exist_ok=True)
    path = ev / "evalset.yml"
    # 空条目列表必须写成 ``items: []``:裸 ``items:`` 是 YAML null,
    # 会被解析成 invalid_items,把这些测试想钉的 code 淹掉。
    body = f"version: 1\ndatasource: {datasource}\n"
    body += f"items:\n{items}" if items.strip() else "items: []\n"
    path.write_text(body, encoding="utf-8")
    return path


def _codes(report) -> set[str]:
    # ``Issue`` 的稳定过滤键叫 ``check``(与 models.py / 现有测试同源)。
    return {i.check for i in report.issues}


async def test_evalset_lint_issue_codes(tmp_path):
    _write_examples(tmp_path, "demo", ["已确认题"])
    # 第 5 条是唯一一条**活着**且 SQL 语法非法的条目 —— 第 2 条与它同为坏
    # SQL,但 qid 重复会在解析期被丢弃(只保留第一条),所以 sql_invalid 必须
    # 由一条能进 doc.items 的条目来覆盖。
    _write_evalset(tmp_path, "demo", (
        "  - {qid: e-1, question: 已确认题, reference_sql: SELECT 1,"
        " source: manual, reference_origin: human, status: confirmed}\n"
        "  - {qid: e-1, question: dup, reference_sql: SELECT bad,"
        " source: manual, reference_origin: human, status: confirmed}\n"
        "  - {qid: e-2, question: no sql, source: production,"
        " reference_origin: pipeline, status: confirmed, needs_reference: true}\n"
        "  - {qid: e-3, question: shape, reference_sql: SELECT 1,"
        " source: manual, reference_origin: human, status: confirmed, match: shape}\n"
        "  - {qid: e-4, question: broken, reference_sql: SELECT * FROM,"
        " source: manual, reference_origin: human, status: confirmed}\n"))

    report = await run_validate("demo", project_root=tmp_path)
    codes = _codes(report)
    assert "evalset.duplicate_qid" in codes
    assert "evalset.confirmed_no_sql" in codes
    assert "evalset.confirmed_shape" in codes
    assert "evalset.sql_invalid" in codes
    assert "evalset.leakage" in codes
    # 有资产才进 datasources,条目计数按解析后的 items(重复 qid 已丢弃)。
    assert "demo" in report.datasources
    assert report.counts["demo.evalset"] == 4
    # confirmed 的泄漏是硬错误(会被评分),不是提示。
    leaked = [i for i in report.issues if i.check == "evalset.leakage"]
    assert leaked and all(i.severity == "error" for i in leaked)


async def test_evalset_without_kb_warns(tmp_path):
    _write_evalset(tmp_path, "orphan", "")
    report = await run_validate("orphan", project_root=tmp_path)
    codes = _codes(report)
    assert "evalset.no_kb" in codes
    # 空列表是合法的 ``items: []``,不是 YAML null。
    assert "evalset.invalid_items" not in codes


async def test_pending_leakage_is_warning_not_error(tmp_path):
    """pending 还没进集:同一把泄漏尺子,severity 不同。"""
    _write_examples(tmp_path, "demo", ["按地区统计贷款金额"])
    _write_evalset(tmp_path, "demo", (
        "  - {qid: e-1, question: 按地区统计贷款金额, reference_sql: SELECT 1,"
        " source: semantic, reference_origin: semantic_compiler, status: pending}\n"))

    report = await run_validate("demo", project_root=tmp_path)
    leaked = [i for i in report.issues if i.check == "evalset.leakage"]
    assert len(leaked) == 1
    assert leaked[0].severity == "warning"


async def test_rejected_and_retired_items_are_skipped(tmp_path):
    """墓碑只服务去重,不再被 lint —— 坏 SQL / 缺 SQL / 形状 / 泄漏一律不出声。"""
    _write_examples(tmp_path, "demo", ["泄漏题"])
    _write_evalset(tmp_path, "demo", (
        "  - {qid: e-r, question: 泄漏题, reference_sql: SELECT * FROM,"
        " source: manual, reference_origin: human, status: rejected}\n"
        "  - {qid: e-t, question: 泄漏题, source: manual,"
        " reference_origin: human, status: retired, match: shape}\n"))

    report = await run_validate("demo", project_root=tmp_path)
    codes = _codes(report)
    assert "evalset.sql_invalid" not in codes
    assert "evalset.confirmed_no_sql" not in codes
    assert "evalset.confirmed_shape" not in codes
    assert "evalset.leakage" not in codes
    assert report.counts["demo.evalset"] == 2   # 还在资产里,只是不进评分


async def test_evalset_only_datasource_is_discovered_without_kb(tmp_path):
    """不给 --datasource:evalset 目录也参与发现,且不会被 KB 目录检查跳过。"""
    _write_evalset(tmp_path, "orphan", "")

    report = await run_validate(project_root=tmp_path)
    assert "orphan" in report.datasources
    assert report.counts["orphan.evalset"] == 0
    assert any(i.check == "evalset.no_kb" and i.datasource == "orphan"
               for i in report.issues)
    assert "evalset.invalid_items" not in _codes(report)


async def test_empty_evalsets_directory_does_not_change_report(tmp_path):
    """只有目录没有 evalset.yml:不进 datasources、不留计数、不出 issue。"""
    (tmp_path / ".trove" / "evalsets" / "stray").mkdir(parents=True)

    report = await run_validate(project_root=tmp_path)
    assert "stray" not in report.datasources
    assert "stray.evalset" not in report.counts
    assert not any(i.datasource == "stray" for i in report.issues)
