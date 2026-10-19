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


def test_cli_trajectory_coverage_counts_questions_not_rows(tmp_path, capsys):
    """覆盖数是**题**数:q1 两条失败行只算一题,不能被重试行数虚高。"""
    failures = _write(tmp_path, "failures.jsonl", [
        {"question": "q1", "gold_sql": "SELECT 1", "pred_sql": "SELECT 1",
         "verdict": "MISMATCH"},
        {"question": "q1", "gold_sql": "SELECT 1", "pred_sql": "SELECT 1",
         "verdict": "MISMATCH"},
        {"question": "q2", "gold_sql": "SELECT 1", "pred_sql": "SELECT 1",
         "verdict": "MISMATCH"}])
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "a.log").write_text(
        "════ Run r1 · 2026-08-19 10:00:00\n"
        "question: q1\n"
        '│   matched_tables = ["aa"]\n',
        encoding="utf-8")
    code = cli.main(["--failures", str(failures), "--traces", "",
                     "--runs-dir", str(runs), "--json"])
    assert code == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["meta"]["trajectory_covered_questions"] == 1


def test_cli_out_file_byte_identical_to_stdout(tmp_path, capsys):
    """--out 文件与 stdout 逐字节同(两个输出模式);JSON 也以换行收尾。"""
    failures = _write(tmp_path, "failures.jsonl", [
        {"question": "q1", "gold_sql": "SELECT a.x FROM aa a",
         "pred_sql": "SELECT a.y FROM aa a", "verdict": "MISMATCH"}])
    for extra, name in ((["--json"], "r.json"), ([], "r.txt")):
        out = tmp_path / name
        code = cli.main(["--failures", str(failures), "--traces", "",
                         "--runs-dir", str(tmp_path / "runs"),
                         "--out", str(out), *extra])
        assert code == 0
        stdout = capsys.readouterr().out
        body = out.read_text(encoding="utf-8")
        assert body == stdout
        assert body.endswith("\n")
        if extra:
            json.loads(body)


def test_cli_out_expanduser(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    failures = _write(tmp_path, "failures.jsonl", [])
    code = cli.main(["--failures", str(failures), "--traces", "",
                     "--runs-dir", str(tmp_path), "--json", "--out", "~/r.json"])
    assert code == 0
    capsys.readouterr()
    assert (tmp_path / "r.json").exists()


def test_cli_failures_is_a_directory_exits_2(tmp_path, capsys):
    code = cli.main(["--failures", str(tmp_path), "--traces", "",
                     "--runs-dir", str(tmp_path)])
    assert code == 2
    assert "读失败记录出错" in capsys.readouterr().err


def test_cli_out_unwritable_exits_2(tmp_path, capsys):
    failures = _write(tmp_path, "failures.jsonl", [])
    code = cli.main(["--failures", str(failures), "--traces", "",
                     "--runs-dir", str(tmp_path),
                     "--out", str(tmp_path / "no-such-dir" / "x.json")])
    assert code == 2
    assert "写报告失败" in capsys.readouterr().err


def test_cli_explicit_missing_traces_warns_and_continues(tmp_path, capsys):
    """显式给的 traces 路径不存在:stderr 说一句,报告照出(按空源)。"""
    failures = _write(tmp_path, "failures.jsonl", [])
    missing = tmp_path / "nope.jsonl"
    code = cli.main(["--failures", str(failures), "--traces", str(missing),
                     "--runs-dir", str(tmp_path), "--json"])
    assert code == 0
    cap = capsys.readouterr()
    assert "--traces" in cap.err and str(missing) in cap.err
    assert json.loads(cap.out)["meta"]["traces"] == str(missing)


def test_cli_explicit_missing_runs_dir_warns_and_continues(tmp_path, capsys):
    failures = _write(tmp_path, "failures.jsonl", [])
    code = cli.main(["--failures", str(failures), "--traces", "",
                     "--runs-dir", str(tmp_path / "nope"), "--json"])
    assert code == 0
    cap = capsys.readouterr()
    assert "--runs-dir" in cap.err
    json.loads(cap.out)


def test_cli_missing_structured_warns_and_continues(tmp_path, capsys):
    failures = _write(tmp_path, "failures.jsonl", [])
    missing = tmp_path / "nope_records.jsonl"
    code = cli.main(["--failures", str(failures), "--structured", str(missing),
                     "--traces", "", "--runs-dir", str(tmp_path), "--json"])
    assert code == 0
    cap = capsys.readouterr()
    assert "--structured" in cap.err
    json.loads(cap.out)


def test_cli_meta_declares_actual_sources(tmp_path, capsys):
    """I-1:纪元声明跟实际来源走;meta 带 traces_enabled 与 failures mtime。"""
    failures = _write(tmp_path, "failures.jsonl", [])
    code = cli.main(["--failures", str(failures), "--traces", "",
                     "--runs-dir", str(tmp_path), "--json"])
    assert code == 0
    meta = json.loads(capsys.readouterr().out)["meta"]
    assert meta["traces_enabled"] is False
    assert "run 日志" in meta["pipeline_note"]
    assert "2026-08" in meta["pipeline_note"]
    assert meta["failures_mtime"].endswith("+00:00")


def test_cli_traces_enabled_note_does_not_claim_august(tmp_path, capsys):
    """I-1:traces 开启时不得声称「轨迹来自八月两段式管线」。"""
    failures = _write(tmp_path, "failures.jsonl", [])
    traces = tmp_path / "traces.jsonl"
    traces.write_text("", encoding="utf-8")
    code = cli.main(["--failures", str(failures), "--traces", str(traces),
                     "--runs-dir", str(tmp_path), "--json"])
    assert code == 0
    meta = json.loads(capsys.readouterr().out)["meta"]
    assert meta["traces_enabled"] is True
    assert "2026-08" not in meta["pipeline_note"]
    assert "traces.jsonl" in meta["pipeline_note"]
