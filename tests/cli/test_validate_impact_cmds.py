"""``trove validate --impact`` CLI 冒烟:退出码就是结论,输出逐字节可复现。

tmp 项目树 + monkeypatch.chdir —— 命令的项目根 = cwd。``resolve_home()``
钉到 tmp(CLI 不知道测试的存在,回放不该去读真实的 ``~/.trove``)。

``test_impact_stdout_is_byte_identical_across_hash_seeds`` 是交付里那条
**确定性证明**:``PYTHONHASHSEED=0/1/2`` 三连跑,stdout 逐字节一致 ——
"这次跑出来不一样"的回放没人敢挂 CI。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from trove.cli.commands.validate_cmds import main_validate
from trove.services.skills.service import SkillService

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
                {"name": "date", "datatype": "Date",
                 "description": "Booking date.",
                 "expression": {"dialects": [
                     {"dialect": "ANSI_SQL", "expression": "date"}]}},
            ],
        }],
        "metrics": [{
            "name": "loan_count",
            "agg_time_dimension": "date",
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]},
        }],
    }],
}

_SCHEMA_NOTES = {
    "tables": [{
        "name": "loan",
        "description": "Loans.",
        "columns": [
            {"name": "loan_id", "description": "Loan id."},
            {"name": "date", "description": "Booking date."},
        ],
    }],
}

_FIXTURES = """
items:
  - question: "How many loans?"
    sql: "SELECT COUNT(*) AS loan_count FROM loan"
    dialect: sqlite
    columns: [loan_count]
    rows:
      - [3]
    verdict: pass
"""

_GUARD_MD = """---
name: {name}
description: 回放用的候选守卫
tier: guard
status: pending
guard:
  targets: [sql]
  checks:
    - name: {check}
      severity: {severity}
      expr: {expr}
      reason: {reason}
      hint_zh: 改 SQL
      hint_en: fix the SQL
---

人类可读说明
"""


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """回放走 resolve_home() —— 测试里钉到 tmp,不读真实 ~/.trove。"""
    home = tmp_path / "trove_home"
    home.mkdir()
    monkeypatch.setattr(
        "trove.services.extensions.dryrun.resolve_home", lambda: home)
    return home


def _project(tmp_path: Path, monkeypatch, *, fixtures: str | None = _FIXTURES) -> Path:
    monkeypatch.chdir(tmp_path)
    ds_dir = tmp_path / ".trove" / "kb" / "mini"
    ds_dir.mkdir(parents=True)
    (ds_dir / "schema_notes.yml").write_text(
        yaml.safe_dump(_SCHEMA_NOTES, allow_unicode=True), encoding="utf-8")
    (ds_dir / "semantics.yml").write_text(
        yaml.safe_dump(_SEMANTICS, allow_unicode=True), encoding="utf-8")
    if fixtures is not None:
        (ds_dir / "fixtures.yml").write_text(fixtures, encoding="utf-8")
    return ds_dir


def _install_guard(tmp_path: Path, *, name="no-naked-select", expr="has_limit == 1",
                   severity="blocking", check="no-limit") -> None:
    """已确认的现状守卫(回放的 before 态)。"""
    svc = SkillService(root=tmp_path / ".trove" / "skills")
    svc.create({
        "name": name, "description": "现状守卫", "tier": "guard",
        "guard": {"targets": ["sql"], "checks": [{
            "name": check, "severity": severity, "expr": expr,
            "reason": "违反", "hint_zh": "改", "hint_en": "fix"}]},
        "body": "说明",
    })
    svc.confirm(name)


def _candidate_dir(tmp_path: Path, *, name="no-naked-select", check="no-limit",
                   expr="has_limit == 1", severity="blocking",
                   root: str = "candidates") -> Path:
    """待导入的资产目录(回放的 after 增量)。"""
    d = tmp_path / root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        _GUARD_MD.format(name=name, check=check, expr=expr, severity=severity,
                         reason="缺 LIMIT"),
        encoding="utf-8")
    return tmp_path / root


# ── 三分支退出码 ─────────────────────────────────────────


def test_impact_without_datasource_exit_2(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path)
    assert main_validate(["--impact", str(cand)]) == 2
    assert "点名数据源" in capsys.readouterr().out


def test_impact_clean_exit_0(tmp_path, monkeypatch, capsys):
    """候选守卫对语料全放行 → 无拦截变更 → 0。"""
    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path, expr="has_limit == 0")
    assert main_validate(["--impact", str(cand), "--datasource", "mini"]) == 0
    out = capsys.readouterr().out
    assert "影响面回放" in out and "无拦截变更" in out and "退出码 0" in out


def test_impact_must_block_candidate_exit_1_names_guard(tmp_path, monkeypatch, capsys):
    """验收口径:装必拦守卫 → exit 1 且点名(候选守卫名在报告里)。"""
    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path)
    assert main_validate(["--impact", str(cand), "--datasource", "mini"]) == 1
    out = capsys.readouterr().out
    assert "no-naked-select" in out and "新增拦截 1 条" in out
    assert "no-limit" in out and "has_limit == 1" in out      # 证据可指回表达式
    assert "has_limit=0" in out                               # 变量实际取值


def test_impact_looser_candidate_is_newly_released(tmp_path, monkeypatch, capsys):
    """现状拦、候选放宽 → 新放行也是拦截变更(门必须能红)。"""
    _project(tmp_path, monkeypatch)
    _install_guard(tmp_path, expr="has_limit == 1")
    cand = _candidate_dir(tmp_path, expr="has_limit >= 0")
    assert main_validate(["--impact", str(cand), "--datasource", "mini"]) == 1
    out = capsys.readouterr().out
    assert "新放行 1 条" in out and "退出码 1" in out


def test_impact_missing_candidate_exit_2(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_validate([
        "--impact", str(tmp_path / "ghost"), "--datasource", "mini"]) == 2
    assert "ghost" in capsys.readouterr().out


def test_impact_and_run_are_mutually_exclusive(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path)
    assert main_validate(["--run", "--impact", str(cand),
                          "--datasource", "mini"]) == 2
    assert "互斥" in capsys.readouterr().err


def test_impact_json_ok_follows_combined_exit_code(tmp_path, monkeypatch, capsys):
    """静态干净但回放红 → ok=false(不许被静态的 0 盖过去)。"""
    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path)
    assert main_validate([
        "--impact", str(cand), "--datasource", "mini", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["exit_code"] == 1 and data["ok"] is False
    assert data["impact"]["counts"]["newly_blocked"] == 1


# ── gate 消费面 ──────────────────────────────────────────


def test_impact_json_metrics_feed_eval_gate(tmp_path, monkeypatch, capsys):
    """--json 顶层 metrics = eval_gate 的 scorecard 消费面,零转换、**无误报**。"""
    from trove.eval.gate import compare_metrics, score_from_file

    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path, expr="has_limit == 0")     # 全放行 → 干净
    assert main_validate([
        "--impact", str(cand), "--datasource", "mini", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["metrics"]["newly_blocked_fail_rate"] == 0.0
    assert set(data["metrics"]) >= {
        "coverage", "blocking_fail_rate", "newly_blocked_fail_rate", "n_judged"}

    score = tmp_path / "scorecard.json"
    score.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    metrics = score_from_file(score)
    assert metrics["coverage"] == data["metrics"]["coverage"]

    verdict = compare_metrics(metrics, dict(metrics))     # 同一份 → 一个都不回归
    assert [r.metric for r in verdict.metrics if not r.ok] == []
    assert verdict.metrics


# ── 隐私(R5)─────────────────────────────────────────────


def test_impact_masks_questions_by_default_and_flag_shows(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path, expr="has_limit == 0")
    assert main_validate(["--impact", str(cand), "--datasource", "mini"]) == 0
    assert "How many loans?" not in capsys.readouterr().out

    assert main_validate([
        "--impact", str(cand), "--datasource", "mini",
        "--include-questions"]) == 0
    assert "How many loans?" in capsys.readouterr().out


def test_impact_scorecard_is_directly_consumed_by_eval_gate_cli(tmp_path, monkeypatch, capsys):
    """字面验收:回放报告当 scorecard **直接喂 scripts/eval_gate.py**。

    同一份报告 → 门退出 0(无误报);拦截率变差的那份 → 门退出 1(门能红)。
    两个方向都测,否则"过门"可能只是门没在看这个指标。
    """
    import importlib.util

    _project(tmp_path, monkeypatch)
    clean = _candidate_dir(tmp_path, expr="has_limit == 0", root="cand-clean")
    blocking = _candidate_dir(tmp_path, expr="has_limit == 1", root="cand-block")

    def _scorecard(target: Path, name: str) -> Path:
        code = main_validate([
            "--impact", str(target), "--datasource", "mini", "--json"])
        assert code in (0, 1)
        data = json.loads(capsys.readouterr().out)
        p = tmp_path / name
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return p

    base = _scorecard(clean, "baseline.json")          # 0 拦截
    same = _scorecard(clean, "same.json")              # 同一份
    worse = _scorecard(blocking, "worse.json")         # 新增拦截 1

    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_gate.py"
    spec = importlib.util.spec_from_file_location("eval_gate_under_test", path)
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)

    monkeypatch.setattr(sys, "argv", [
        "eval_gate", "--baseline", str(base), "--current", str(same), "--min-n", "0"])
    assert gate.main() == 0                            # 无误报
    monkeypatch.setattr(sys, "argv", [
        "eval_gate", "--baseline", str(base), "--current", str(worse), "--min-n", "0"])
    assert gate.main() == 1                            # 门能红


# ── 确定性证明:PYTHONHASHSEED 三连跑逐字节一致 ──────────

_DETERMINISM_CODE = """
import os, sys
sys.path.insert(0, {root!r})
os.chdir({project!r})
from trove.cli.commands.validate_cmds import main_validate
raise SystemExit(main_validate(
    ["--impact", {target!r}, "--datasource", "mini", "--json"]))
"""


def test_impact_stdout_is_byte_identical_across_hash_seeds(tmp_path, monkeypatch):
    """PYTHONHASHSEED=0/1/2 三连跑 → stdout 逐字节一致(挂 CI 的前提)。"""
    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path)
    repo_root = Path(__file__).resolve().parents[2]
    code = _DETERMINISM_CODE.format(
        root=str(repo_root), project=str(tmp_path), target=str(cand))
    home = tmp_path / "trove_home"

    outs: list[bytes] = []
    codes: list[int] = []
    for seed in ("0", "1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed, HOME=str(home))
        env.pop("TROVE_STORAGE_URL", None)
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, env=env,
            cwd=str(tmp_path), timeout=180,
        )
        assert r.returncode in (0, 1, 2), r.stderr.decode()
        outs.append(r.stdout)
        codes.append(r.returncode)
    assert len(set(outs)) == 1, f"跨进程 stdout 不一致: {[o[:200] for o in outs]}"
    assert len(set(codes)) == 1
    assert b'"newly_blocked"' in outs[0]          # 确实是那份报告,不是空输出


# ── 不改变既有行为 ───────────────────────────────────────


def test_impact_plain_validate_unaffected(tmp_path, monkeypatch, capsys):
    """不带 --impact 时行为与从前一致(静态报告、无 metrics/impact 段)。"""
    _project(tmp_path, monkeypatch)
    assert main_validate(["--datasource", "mini", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "metrics" not in data and "impact" not in data and "dryrun" not in data


def test_slash_validate_impact_appended(tmp_path, monkeypatch, capsys):
    """REPL 里 --impact 也接得上(没有退出码,但结论落在文本末尾)。"""
    import asyncio

    _project(tmp_path, monkeypatch)
    cand = _candidate_dir(tmp_path)
    reg_cmds: dict = {}
    from trove.cli.commands.validate_cmds import register_validate_commands

    class _Reg:
        def register(self, cmd):
            reg_cmds[cmd.name] = cmd

    register_validate_commands(_Reg(), {})
    text = asyncio.run(reg_cmds["validate"].handler(
        f"--impact {cand} --datasource mini"))
    assert "影响面回放" in text and "退出码 1" in text
