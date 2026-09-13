"""可复现基线产物 — 固定问题集 + 可对账的基线结果(零 LLM/网络)。

评测回归门能拦截的前提是有锚点:问题集固定(qid 稳定)、基线结果与问题
一一对应。本模块提供纯函数:

- build_questions:dev.json(官方 BIRD)按 db_id 提取成固定问题集,顺序 =
  dev.json 原始顺序,qid = f"{db_id}-{index:04d}" 稳定可复现;
- match_qid:结果条目按问题文本对账回 qid(归一化匹配);
- coverage_check:基线结果是否覆盖全部问题(qid 覆盖 + 无冗余条目);
- check_integrity:文件能否解析、指标能否计算、覆盖是否完整。

基线产物布局(eval/baseline/):
- questions.jsonl:固定问题集(每行 {qid, db_id, question, evidence, gold_sql})
- results.jsonl:基线判定条目(每行带 qid,含 run_id/pred_sql/verdict 等)

重建:scripts/build_eval_baseline.py --dev-json <dev.json> --db-id <id>
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

#: 问题集文件字段(顺带校验载荷,防重建时字段漂移)
_QUESTION_KEYS = {"qid", "db_id", "question", "evidence", "gold_sql"}


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """读取 jsonl(逐行 dict;容忍坏行,坏行跳过)。"""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def qid_for(db_id: str, index: int) -> str:
    """稳定 qid:db_id + 在 dev.json 中的序号(1 起,零填充)。"""
    return f"{db_id}-{index:04d}"


def _norm(s: str) -> str:
    """对账键:小写 + 空白压缩 + 去标点(re 不支持 \\p,用 \\W 取非字母数字)。"""
    s = re.sub(r"[\W_]+", " ", str(s or "").lower()).strip()
    return re.sub(r"\s+", " ", s)


def build_questions(
    dev_entries: Iterable[dict[str, Any]],
    db_id: str,
    gold_key: str = "SQL",
) -> list[dict[str, Any]]:
    """从 BIRD dev.json 条目提取固定问题集(保持 dev.json 原始顺序)。

    gold_key:dev.json 里 gold SQL 的字段名(官方用 "SQL")。
    """
    out: list[dict[str, Any]] = []
    for i, q in enumerate(dev_entries, 1):
        if q.get("db_id") != db_id:
            continue
        out.append({
            "qid": qid_for(db_id, i),
            "question_id": q.get("question_id"),
            "db_id": db_id,
            "question": q.get("question", ""),
            "evidence": q.get("evidence", ""),
            "gold_sql": q.get(gold_key, ""),
        })
    return out


def match_qid(question: str, questions: Iterable[dict[str, Any]]) -> str | None:
    """问题文本 → qid(归一化精确匹配;无匹配返回 None)。"""
    key = _norm(question)
    if not key:
        return None
    by_norm: dict[str, str] = {}
    for q in questions:
        by_norm.setdefault(_norm(q.get("question", "")), q["qid"])
    return by_norm.get(key)


def _result_qid(e: dict[str, Any], questions: list[dict[str, Any]]) -> str | None:
    """结果条目的 qid:自带 qid 优先,否则按问题文本对账。"""
    qid = e.get("qid")
    if qid:
        return str(qid)
    return match_qid(str(e.get("question", "")), questions)


def coverage_check(
    questions: list[dict[str, Any]],
    results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """基线对账:qid 覆盖(缺的题)、冗余(不在问题集的结果)、重复条目。

    Returns:
        dict: {
            "total_questions": int,
            "covered_qids": sorted list,
            "missing_qids": sorted list,
            "extra_qids": sorted list,   # 结果里有但问题集没有的 qid
            "duplicates": list[str],     # 同一 qid 出现多次的 qid 列表
        }
    """
    rows = list(results)
    question_qids = [q["qid"] for q in questions]
    result_qids = [rq for rq in (_result_qid(r, questions) for r in rows) if rq]
    from collections import Counter

    counts = Counter(result_qids)
    duplicates = sorted(q for q, c in counts.items() if c > 1)
    covered = sorted(set(result_qids))
    return {
        "total_questions": len(questions),
        "covered_qids": covered,
        "missing_qids": sorted(set(question_qids) - set(covered)),
        "extra_qids": sorted(set(covered) - set(question_qids)),
        "duplicates": duplicates,
    }


def check_integrity(
    questions_path: str | Path,
    results_path: str | Path,
    require_full: bool = False,
) -> dict[str, Any]:
    """一次基线完整性检查:解析、字段、覆盖、指标可计算。

    硬问题(解析失败/字段漂移/指标算不出/冗余 qid/重复判定)进 problems;
    软缺口(问题集尚未全部覆盖,如分片录入中的基线)进 warnings。
    require_full=True 时软缺口升级为硬问题(CI 收基线前强制全覆盖)。

    供 scripts/eval_baseline.py check 与 CI(opt-in)使用,零 LLM/网络。
    """
    questions = load_jsonl(questions_path)
    results = load_jsonl(results_path)
    problems: list[str] = []
    warnings: list[str] = []

    if not questions:
        problems.append(f"问题集为空: {questions_path}")
    else:
        bad = [
            q["qid"]
            for q in questions
            if not isinstance(q, dict)
            or not _QUESTION_KEYS.issubset(q.keys())
            or not q.get("question")
        ]
        if bad:
            problems.append(f"问题集字段缺失/非法 qid: {bad}")

    if not results:
        problems.append(f"基线结果为空: {results_path}")
    else:
        try:
            from trove.eval.gate import metrics_from_entries

            metrics_from_entries(results)
        except Exception as e:  # noqa: BLE001 — 完整性检查要把任何异常转成问题
            problems.append(f"基线指标计算失败: {e}")

    if questions and results:
        cov = coverage_check(questions, results)
        if cov["missing_qids"]:
            msg = (f"缺 {len(cov['missing_qids'])} 题结果: "
                   f"{cov['missing_qids'][:8]}{'...' if len(cov['missing_qids']) > 8 else ''}")
            (problems if require_full else warnings).append(msg)
        if cov["extra_qids"]:
            problems.append(f"基线含问题集外的 qid: {cov['extra_qids']}")
        if cov["duplicates"]:
            problems.append(f"同一 qid 重复判定: {cov['duplicates']}")

    return {
        "questions_path": str(questions_path),
        "results_path": str(results_path),
        "n_questions": len(questions),
        "n_results": len(results),
        "coverage": (
            round(len(cov["covered_qids"]) / cov["total_questions"], 4)
            if questions and results
            else 0.0
        ),
        "problems": problems,
        "warnings": warnings,
        "ok": not problems,
    }
