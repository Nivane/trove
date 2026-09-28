"""基线构建 CLI 的两条「不依赖 BIRD 数据集」的路径。

背景:仓库不带 BIRD dev.json(体积 + 许可),但仓库**必须**能在没有它的
机器上迁移基线。原先 ``--dev-json`` 是 required,于是"补基线"这件事被一个
不在仓库里的文件锁死了 —— 这两个测试锁住解锁后的行为。
"""

from __future__ import annotations

import json

import pytest

from scripts.build_eval_baseline import main


def _write_questions(path, qids):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(
        json.dumps({"qid": q, "db_id": "financial", "question": f"q for {q}",
                    "evidence": "", "gold_sql": "SELECT 1"})
        for q in qids
    ) + "\n", encoding="utf-8")


def _write_results(path, qids):
    path.write_text("\n".join(
        json.dumps({"qid": q, "question": f"q for {q}", "verdict": "MATCH",
                    "pred_sql": "SELECT 1", "gold_sql": "SELECT 1"})
        for q in qids
    ) + "\n", encoding="utf-8")


def test_reuses_existing_questions_without_dev_json(tmp_path, monkeypatch, capsys):
    """``--questions`` 复用不重建 —— 不需要数据集,也不覆盖问题集。"""
    q = tmp_path / "questions.jsonl"
    _write_questions(q, ["financial-0001", "financial-0002"])
    before = q.read_text(encoding="utf-8")
    r = tmp_path / "results.jsonl"
    _write_results(r, ["financial-0001"])

    monkeypatch.setattr(
        "sys.argv",
        ["build", "--questions", str(q), "--results", str(r), "--out", str(tmp_path)],
    )
    assert main() == 0
    assert q.read_text(encoding="utf-8") == before  # 没用 --dev-json 就不该重写

    out = json.loads((tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert out["qid"] == "financial-0001"
    assert out["run_id"] == "baseline-financial-0001"
    assert "41%" not in capsys.readouterr().out  # 1/2 覆盖


def test_dev_json_and_questions_are_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["build", "--questions", str(tmp_path / "q.jsonl"),
         "--dev-json", str(tmp_path / "d.json")],
    )
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2


def test_shrinking_the_baseline_is_refused(tmp_path, monkeypatch, capsys):
    """**防静默变松**:基线是回归门的锚点,安静地少几条 = 门安静地变松。

    门自己不会报警 —— 它只看当前基线,不知道曾经有多少条。所以这道闸
    必须在**写入侧**。
    """
    q = tmp_path / "questions.jsonl"
    _write_questions(q, ["financial-0001", "financial-0002", "financial-0003"])
    r = tmp_path / "results.jsonl"
    _write_results(r, ["financial-0001", "financial-0002", "financial-0003"])

    monkeypatch.setattr(
        "sys.argv",
        ["build", "--questions", str(q), "--results", str(r), "--out", str(tmp_path)],
    )
    assert main() == 0

    # 第二次只喂一条:会让 3 → 1
    thin = tmp_path / "thin.jsonl"
    _write_results(thin, ["financial-0001"])
    monkeypatch.setattr(
        "sys.argv",
        ["build", "--questions", str(q), "--results", str(thin), "--out", str(tmp_path)],
    )
    assert main() == 2
    assert "拒绝写入" in capsys.readouterr().err
    # 原文件未被截断
    assert len((tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_allow_shrink_opts_out(tmp_path, monkeypatch):
    q = tmp_path / "questions.jsonl"
    _write_questions(q, ["financial-0001", "financial-0002", "financial-0003"])
    r = tmp_path / "results.jsonl"
    _write_results(r, ["financial-0001", "financial-0002", "financial-0003"])
    monkeypatch.setattr(
        "sys.argv",
        ["build", "--questions", str(q), "--results", str(r), "--out", str(tmp_path)],
    )
    assert main() == 0

    thin = tmp_path / "thin.jsonl"
    _write_results(thin, ["financial-0001"])
    monkeypatch.setattr(
        "sys.argv",
        ["build", "--questions", str(q), "--results", str(thin),
         "--out", str(tmp_path), "--allow-shrink"],
    )
    assert main() == 0
    assert len((tmp_path / "results.jsonl").read_text(encoding="utf-8").splitlines()) == 1
