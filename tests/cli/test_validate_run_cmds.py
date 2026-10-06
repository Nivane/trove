"""``trove validate --run`` CLI 冒烟:退出码就是结论(CI 直接挂它)。

tmp 项目树 + monkeypatch.chdir —— 命令的项目根 = cwd。``resolve_home()``
也钉到 tmp:CLI 不知道测试的存在,``--episodes`` 一旦没钉就会去读真实的
``~/.trove/memory/episodes.sqlite`` —— 那是开发者的会话数据,不该进测试。
"""

from __future__ import annotations

import json
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


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """CLI --run 走 resolve_home() —— 测试里钉到 tmp,不读真实 ~/.trove。"""
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


def _install_validator(tmp_path: Path, *, name="must-catch",
                       expr="min >= 100", severity="blocking") -> None:
    svc = SkillService(root=tmp_path / ".trove" / "skills")
    svc.create({
        "name": name, "description": "装前试跑用的守卫",
        "tier": "validator", "severity": severity, "targets": ["result"],
        "checks": [{"expr": expr, "columns": ["loan_count"], "message": "违反"}],
        "body": "说明",
    })
    svc.confirm(name)


def test_run_without_datasource_exit_2(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_validate(["--run"]) == 2
    assert "点名数据源" in capsys.readouterr().out


def test_run_clean_exit_0(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_validate(["--run", "--datasource", "mini"]) == 0
    out = capsys.readouterr().out
    assert "装前" in out and "装后" in out and "退出码 0" in out
    assert "guard 可用" in out                    # guard 档随 E2 接通(真实 run_guards)


def test_run_broken_install_exit_1_names_asset(tmp_path, monkeypatch, capsys):
    """验收口径:装坏守卫 → exit 1 且点名(静态检查本身是干净的)。"""
    _project(tmp_path, monkeypatch)
    _install_validator(tmp_path, name="too-strict")
    assert main_validate(["--run", "--datasource", "mini"]) == 1
    out = capsys.readouterr().out
    assert "too-strict" in out and "拦截变更 1 条" in out


def test_run_missing_explicit_fixtures_exit_2(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, fixtures=None)
    assert main_validate([
        "--run", "--datasource", "mini",
        "--fixtures", str(tmp_path / "ghost.yml")]) == 2
    assert "ghost.yml" in capsys.readouterr().out


def test_run_json_metrics_feed_eval_gate(tmp_path, monkeypatch, capsys):
    """--json 顶层 metrics = eval_gate 的 scorecard 消费面,零转换。"""
    from trove.eval.gate import score_from_file

    _project(tmp_path, monkeypatch)
    assert main_validate(["--run", "--datasource", "mini", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["exit_code"] == 0 and data["ok"] is True
    assert set(data["metrics"]) >= {"coverage", "blocking_fail_rate", "n_judged"}
    assert data["dryrun"]["counts"]["covered"] == 2  # validator + guard 两档各判过

    score = tmp_path / "scorecard.json"
    score.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    metrics = score_from_file(score)
    assert metrics["coverage"] == data["metrics"]["coverage"]


def test_run_json_ok_follows_combined_exit_code(tmp_path, monkeypatch, capsys):
    """静态干净但试跑红 → ok=false(不许被静态的 0 盖过去)。"""
    _project(tmp_path, monkeypatch)
    _install_validator(tmp_path, name="too-strict")
    assert main_validate(["--run", "--datasource", "mini", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["exit_code"] == 1 and data["ok"] is False


def test_run_masks_questions_by_default_and_flag_shows(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch)
    assert main_validate(["--run", "--datasource", "mini"]) == 0
    assert "How many loans?" not in capsys.readouterr().out

    assert main_validate([
        "--run", "--datasource", "mini", "--include-questions"]) == 0
    assert "How many loans?" in capsys.readouterr().out


def test_run_episodes_flag_reads_isolated_home(tmp_path, monkeypatch, capsys):
    """--episodes 在隔离 home 下:如实报「无」,不炸、不静默。"""
    _project(tmp_path, monkeypatch)
    assert main_validate(["--run", "--datasource", "mini", "--episodes"]) == 0
    out = capsys.readouterr().out
    assert "episodes" in out and "不存在" in out


def test_run_limit_truncates_corpus(tmp_path, monkeypatch, capsys):
    _project(tmp_path, monkeypatch, fixtures="""
items:
  - question: q1
    sql: SELECT 1
    columns: [n]
    rows:
      - [1]
  - question: q2
    sql: SELECT 2
    columns: [n]
    rows:
      - [2]
""")
    assert main_validate([
        "--run", "--datasource", "mini", "--limit", "1", "--include-questions"]) == 0
    out = capsys.readouterr().out
    assert "q1" in out and "q2" not in out and "1/2 条" in out


def test_run_plain_validate_unaffected(tmp_path, monkeypatch, capsys):
    """不带 --run 时行为与从前一致(静态报告、无 metrics 段)。"""
    _project(tmp_path, monkeypatch)
    assert main_validate(["--datasource", "mini", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "metrics" not in data and "dryrun" not in data
