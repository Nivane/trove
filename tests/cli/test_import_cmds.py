"""CLI ``trove import semantic``:解析/退出码/JSON 报告(零 LLM、零网络)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trove.cli.commands import import_cmds
from trove.cli.commands.import_cmds import main_import

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "semantic_import"
PROJECT = FIXTURES / "dbt_project"
MANIFEST = PROJECT / "target" / "manifest.json"
LOSSY = FIXTURES / "dbt_lossy"
CUBE = FIXTURES / "cube_meta.json"


def _args(*extra: str) -> list[str]:
    return ["semantic", "--source", "dbt", "--path", str(PROJECT),
            "--datasource", "demo", "--manifest", str(MANIFEST), *extra]


def test_usage_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main_import([])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        main_import(["semantic"])
    assert exc.value.code == 2


def test_preview_json_report_is_machine_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main_import(_args("--mode", "preview", "--json"))

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "dbt"
    assert report["mode"] == "preview"
    assert report["datasource"] == "demo"
    assert report["counts"] == {
        "datasets": 2, "fields": 3, "metrics": 4, "relationships": 1,
    }
    assert report["suggestion"] == "bootstrap"
    assert report["blocking"] == []
    assert report["landed"] is None
    assert not (tmp_path / ".trove").exists()


def test_preview_human_report_prints_suggestion(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main_import(_args()) == 0

    out = capsys.readouterr().out
    assert "datasets 2" in out
    assert "建议落点: --mode bootstrap" in out


def test_clean_import_passes_strict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main_import(_args("--strict", "--json")) == 0


def test_strict_aborts_on_warnings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main_import([
        "semantic", "--source", "dbt", "--path", str(LOSSY),
        "--datasource", "demo", "--strict", "--json",
    ])

    assert code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["aborted"] == "strict"
    assert report["warnings"]
    assert report["unsupported"]


def test_stage_requires_existing_model(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main_import([
        "semantic", "--source", "cube", "--path", str(CUBE),
        "--datasource", "demo", "--mode", "stage",
    ])

    assert code == 2
    assert "还没有语义模型" in capsys.readouterr().err


def test_stage_json_error_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main_import([
        "semantic", "--source", "cube", "--path", str(CUBE),
        "--datasource", "demo", "--mode", "stage", "--json",
    ])

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert "还没有语义模型" in payload["error"]


def test_bootstrap_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main_import(_args("--mode", "bootstrap")) == 0

    assert (tmp_path / ".trove" / "kb" / "demo" / "semantics.yml").exists()
    assert "已写入" in capsys.readouterr().out

    code = main_import(_args("--mode", "bootstrap"))
    assert code == 2
    assert "已有非空语义模型" in capsys.readouterr().err


def test_cube_with_manifest_is_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = main_import([
        "semantic", "--source", "cube", "--path", str(CUBE),
        "--datasource", "demo", "--manifest", str(MANIFEST),
    ])

    assert code == 2
    assert "--manifest 只适用于" in capsys.readouterr().err


def test_runtime_error_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    async def boom(*args, **kwargs):
        raise RuntimeError("装配失败")

    monkeypatch.setattr(import_cmds, "run_semantic_import", boom)
    code = main_import(_args())

    assert code == 1
    assert "RuntimeError: 装配失败" in capsys.readouterr().err
