"""CLI 测试:退出码、JSON 输出、缺文件响亮失败、空输入全 0。"""
from __future__ import annotations

import json

from scripts import eval_chain_attribution as cli


def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines),
                 encoding="utf-8")
    return p


def test_cli_json_report(tmp_path, capsys):
    failures = _write(tmp_path, "failures.jsonl", [
        {"question": "q1", "gold_sql": "SELECT a.x FROM aa a",
         "pred_sql": "SELECT a.y FROM aa a", "verdict": "MISMATCH"}])
    code = cli.main(["--failures", str(failures), "--traces", "",
                     "--runs-dir", str(tmp_path / "runs"), "--json"])
    assert code == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["counts"]["rows"] == 1
    assert rep["counts"]["l1"]["projection_diff"] == 1


def test_cli_missing_failures_file_exits_2(tmp_path, capsys):
    code = cli.main(["--failures", str(tmp_path / "nope.jsonl"),
                     "--traces", "", "--runs-dir", str(tmp_path)])
    assert code == 2
    assert "nope.jsonl" in capsys.readouterr().err


def test_cli_empty_input_zeros(tmp_path, capsys):
    failures = _write(tmp_path, "failures.jsonl", [])
    code = cli.main(["--failures", str(failures), "--traces", "",
                     "--runs-dir", str(tmp_path / "runs"), "--json"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["counts"]["rows"] == 0


def test_cli_bad_line_exits_2(tmp_path, capsys):
    p = tmp_path / "failures.jsonl"
    p.write_text("{not json\n", encoding="utf-8")
    code = cli.main(["--failures", str(p), "--traces", "",
                     "--runs-dir", str(tmp_path)])
    assert code == 2
