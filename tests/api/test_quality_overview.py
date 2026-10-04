"""``GET /v1/admin/quality/overview`` 质量运营台端点测试(真实往返;零网络)。

覆盖设计稿 §4.1 的六条硬规则 + 2026-10-02 五项裁决:

1. 产物缺失 → 该块 null + degraded(FileNotFoundError),HTTP 仍 200,
   gate 判 ``not_concluded`` 并带原因 —— 判不了就明说,不输出假的 "pass"。
2. 未判定条目(verdict 不在判定集)不进分子也不进分母 —— 由 gate 纯函数
   保证,这里断言端点与 ``compare_metrics`` **逐项一致**(门禁 CLI 同源)。
3. ``not_concluded`` 是合法结论:样本量 < min_n 时同样如此。
4. 覆盖率(coverage)在端点算:baseline_qids / covered / ratio,以及
   裁决②配套的 ``duplicate_qids`` —— **失败清单不按 qid 去重**。
5. ``gold_sql`` 只出现在 admin 端点(anon 401 / 非 admin 403)。
6. 草稿 lesson 不在此端点写(零新写入端点,起草走既有 KB 路由)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trove.api.routers import quality
from trove.eval.gate import compare_metrics, score_from_file

_TOP_LEVEL_KEYS = {
    "available", "generated_at", "current", "baseline", "gate",
    "failures", "feedback", "decisions", "not_measured", "degraded",
}
_ARTIFACT_KEYS = {
    "path", "kind", "n", "n_judged", "mtime", "batch_at", "metrics", "coverage",
}
_GATE_KEYS = {
    "verdict", "reason", "min_n", "tolerances", "metrics", "unpaired",
    "denominator_notes",
}
_FAILURE_ITEM_KEYS = {
    "qid", "question", "verdict", "path", "error", "retries",
    "pred_sql", "gold_sql", "run_id",
}
_FEEDBACK_KEYS = {
    "up", "down", "by_datasource", "pending_lessons", "confirmed_lessons",
    "pending_examples", "promotion_enabled", "promotion_threshold",
    "promotion_net_upvotes_min", "last_rated_at",
}

RUN_ID = "eval-1-1759300000"          # 尾段 = Unix 秒(批次时刻的来源)
BATCH_ISO_PREFIX = "2025-10-01T"


def _paths(api_app) -> tuple[Path, Path]:
    root = api_app.state.kb.kb_dir.parent.parent
    return (root / ".trove" / "eval" / "results.jsonl",
            root / "eval" / "baseline" / "results.jsonl")


def _entry(
    qid: str, verdict: str, *, path: str = "compiled", retries: int = 0,
    run_id: str = RUN_ID, pred: str | None = None, gold: str | None = None,
) -> dict:
    """一条 eval_bird 形状的结果条目(字段对齐 scripts/eval_bird.py)。"""
    return {
        "run_id": run_id,
        "question": f"q {qid}",
        "qid": qid,
        "verdict": verdict,
        "path": path,
        "retries": retries,
        "pred_sql": pred if pred is not None else f"SELECT {qid}",
        "gold_sql": gold if gold is not None else f"SELECT {qid}",
        "evidence": {"row_count": 1},
        "compile_meta": {"outcome": "compiled"},
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8",
    )


def _seed_pair(api_app) -> tuple[Path, Path]:
    """基线 8 题(4 对 4 错)、当前 6 题(5 对 1 错,q1 重复一次)。

    当前侧故意:① 覆盖率只有 5/8=0.625;② q1 出现两次(不去重)—— 两件
    事都要在响应里可区分。指标方向全部向上(EX 0.5 → 0.8333),门应判 pass。
    """
    cur_path, base_path = _paths(api_app)
    base = [
        _entry(
            f"q{i}", "MATCH" if i % 2 == 0 else "MISMATCH",
            pred=f"SELECT q{i}" if i % 2 == 0 else f"SELECT q{i} FROM u",
            gold=f"SELECT q{i}" if i % 2 == 0 else f"SELECT q{i} FROM t",
        )
        for i in range(1, 9)
    ]
    cur = [
        _entry("q1", "MATCH"),
        _entry("q2", "MATCH"),
        _entry("q3", "MATCH"),
        _entry("q4", "MATCH"),
        _entry("q5", "MATCH"),
        _entry("q1", "MISMATCH", retries=1, path="llm",
               gold="SELECT q1 FROM t", pred="SELECT q1 FROM u"),
        _entry("q1", "MISMATCH",
               gold="SELECT q1 FROM t", pred="SELECT q1 FROM v"),
    ]
    _write_jsonl(base_path, base)
    _write_jsonl(cur_path, cur)
    return cur_path, base_path


class TestQualityShape:
    async def test_no_artifacts_is_honest_null_not_zero(self, client, api_app, api_kb):
        r = await client.get("/v1/admin/quality/overview")
        assert r.status_code == 200, r.text
        body = r.json()

        assert set(body) == _TOP_LEVEL_KEYS
        assert body["available"] is False
        assert body["current"] is None and body["baseline"] is None
        assert body["failures"] is None          # 没有产物 → null,不是 0
        assert body["generated_at"]
        assert body["not_measured"] == ["failures.by_error_class", "feedback.trend"]

        # 两条产物腿各记一条降级(缺失 ≠ 坏了,但也不许静默)
        missing = [d for d in body["degraded"] if d["block"] == "eval_artifacts"]
        assert len(missing) == 2
        assert {d["error"] for d in missing} == {"FileNotFoundError"}
        assert all(d["at"] for d in missing)
        assert all(d["source"].endswith("results.jsonl") for d in missing)

        # 判不了 → not_concluded 且带原因(不是假的 "pass")
        gate = body["gate"]
        assert set(gate) == _GATE_KEYS
        assert gate["verdict"] == "not_concluded"
        assert "当前评测产物不可读" in gate["reason"]
        assert gate["metrics"] == [] and gate["unpaired"] == []

        # 反馈块与产物无关,照常有值(api_kb 播了一条未确认 lesson)
        fb = body["feedback"]
        assert set(fb) == _FEEDBACK_KEYS
        assert fb["up"] == 0 and fb["down"] == 0      # 测到且为零
        assert fb["pending_lessons"] == 1 and fb["confirmed_lessons"] == 0
        assert fb["pending_examples"] == 0
        assert fb["promotion_enabled"] is False
        assert fb["promotion_net_upvotes_min"] == 3
        assert fb["last_rated_at"] is None            # 没票 → null,不填假时间

        # 判定质量块:没接 store → null + 如实记「没接」,不是 500、不是空报告
        assert body["decisions"] is None
        not_conf = [d for d in body["degraded"] if d["block"] == "decisions"]
        assert {d["error"] for d in not_conf} == {"not_configured"}

    async def test_full_roundtrip_artifacts_and_gate_parity(self, client, api_app, api_kb):
        cur_path, base_path = _seed_pair(api_app)
        r = await client.get("/v1/admin/quality/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is True
        # 评测两腿零降级(decisions 块在本 fixture 里没接 store,自有专测)
        assert [d for d in body["degraded"] if d["block"] != "decisions"] == []

        cur = body["current"]
        assert set(cur) == _ARTIFACT_KEYS
        assert cur["path"] == str(cur_path) and cur["kind"] == "eval_bird"
        assert cur["n"] == 7 and cur["n_judged"] == 7
        assert cur["mtime"] and cur["metrics"]["ex"] == 0.7143
        base = body["baseline"]
        assert base["n"] == 8 and base["metrics"]["ex"] == 0.5
        # 批次时刻从 run_id 尾段解析(不是 mtime)
        assert cur["batch_at"].startswith(BATCH_ISO_PREFIX)
        assert base["coverage"] is None               # 覆盖率只对"当前"有意义

        # 覆盖率 + 重复 qid(裁决②:不合并,显式暴露)
        cov = cur["coverage"]
        assert cov["baseline_qids"] == 8 and cov["covered"] == 5
        assert cov["ratio"] == 0.625
        assert cov["duplicate_qids"] == ["q1"]

        # 门禁:与 gate 纯函数逐项一致(端点不重写任何指标口径)
        expected = compare_metrics(score_from_file(base_path), score_from_file(cur_path))
        assert body["gate"]["verdict"] == "pass"
        assert body["gate"]["reason"] is None
        got = [
            (m["metric"], m["baseline"], m["current"], m["delta"], m["ok"], m["direction"], m["tolerance"])
            for m in body["gate"]["metrics"]
        ]
        want = [
            (m.metric, m.baseline, m.current, m.delta, m.ok, m.direction, m.tolerance)
            for m in expected.metrics
        ]
        assert got == want
        assert body["gate"]["unpaired"] == sorted(set(expected.unpaired))
        assert body["gate"]["denominator_notes"] == list(expected.denominator_notes)
        ex = next(m for m in body["gate"]["metrics"] if m["metric"] == "ex")
        assert ex["baseline"] == 0.5 and ex["current"] == 0.7143 and ex["ok"] is True

        # 失败清单:逐条(同一 qid 的两条都在 —— 裁决②不去重),
        # by_verdict/by_path 是全量计数
        fails = body["failures"]
        assert fails["total"] == 2 and fails["truncated"] is False
        assert fails["by_verdict"] == {"MISMATCH": 2}
        assert fails["by_path"] == {"compiled": 1, "llm": 1}
        assert len(fails["items"]) == 2
        assert all(set(i) == _FAILURE_ITEM_KEYS for i in fails["items"])
        dupes = [i for i in fails["items"] if i["qid"] == "q1"]
        assert len(dupes) == 2                        # 同一 qid 两条都在
        assert {i["verdict"] for i in dupes} == {"MISMATCH"}
        assert {i["retries"] for i in dupes} == {0, 1}
        mism = next(i for i in fails["items"] if i["path"] == "llm")
        assert mism["retries"] == 1 and mism["path"] == "llm"
        assert mism["gold_sql"] and mism["run_id"] == RUN_ID

    async def test_failures_limit_slices_items_not_counts(self, client, api_app, api_kb):
        cur_path, _ = _paths(api_app)
        _write_jsonl(cur_path, [_entry(f"q{i}", "MISMATCH") for i in range(1, 4)])
        r = await client.get("/v1/admin/quality/overview", params={"failures_limit": 2})
        assert r.status_code == 200, r.text
        fails = r.json()["failures"]
        assert fails["total"] == 3 and len(fails["items"]) == 2
        assert fails["truncated"] is True
        assert fails["by_verdict"] == {"MISMATCH": 3}   # 计数不截断

        r = await client.get("/v1/admin/quality/overview", params={"failures_limit": 0})
        assert r.status_code == 422
        r = await client.get("/v1/admin/quality/overview", params={"failures_limit": 201})
        assert r.status_code == 422

    async def test_min_n_below_threshold_not_concluded(self, client, api_app, api_kb, monkeypatch):
        _seed_pair(api_app)
        monkeypatch.setattr(api_app.state.config.eval, "min_n", 50)
        r = await client.get("/v1/admin/quality/overview")
        assert r.status_code == 200, r.text
        gate = r.json()["gate"]
        assert gate["verdict"] == "not_concluded"
        assert gate["min_n"] == 50
        assert "数据不足" in gate["reason"] and "7" in gate["reason"]
        assert gate["metrics"] == []

    async def test_corrupt_current_degrades_alone(self, client, api_app, api_kb):
        cur_path, base_path = _seed_pair(api_app)
        cur_path.write_text("not json at all\n", encoding="utf-8")
        r = await client.get("/v1/admin/quality/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["current"] is None and body["failures"] is None
        assert body["baseline"] is not None           # 另一条腿不受影响
        assert body["available"] is True
        entry = next(d for d in body["degraded"] if d["source"] == str(cur_path))
        assert entry["error"] == "ValueError"
        assert body["gate"]["verdict"] == "not_concluded"
        assert "当前评测产物不可读" in body["gate"]["reason"]

    async def test_feedback_timeout_is_isolated(self, client, api_app, api_kb, monkeypatch):
        import asyncio

        async def hang(_request):
            await asyncio.sleep(30)                   # 远高于 _SOURCE_TIMEOUT_S

        monkeypatch.setattr(quality, "_feedback_block", hang)
        r = await client.get("/v1/admin/quality/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["feedback"] is None
        entry = next(d for d in body["degraded"] if d["source"] == "kb")
        assert entry["block"] == "feedback" and entry["error"] == "Timeout"


class TestQualityFeedback:
    async def test_votes_pending_and_last_rated(self, client, api_app, api_kb):
        kb = api_app.state.kb
        ds_dir = kb.kb_dir / "test_db"
        (ds_dir / "lessons.yml").write_text(
            "lessons:\n"
            "  - pattern: 日期列误当文本比较\n"
            "    note: 先确认列类型\n"
            "    confirmed: false\n"
            "    upvotes: 2\n"
            "    downvotes: 1\n"
            '    updated_at: "2026-10-01T10:00:00+00:00"\n'
            "  - pattern: 金额单位换算\n"
            "    note: 千元口径\n"
            "    confirmed: true\n"
            "    upvotes: 3\n"
            '    updated_at: "2026-10-02T08:00:00+00:00"\n',
            encoding="utf-8",
        )
        (ds_dir / "examples.yml").write_text(
            "examples:\n"
            "  - question: 学生们的平均成绩是多少\n"
            "    sql: SELECT county, AVG(grade) FROM students GROUP BY county\n"
            "    tags: [成绩]\n"
            "  - question: 每个县的学生数\n"
            "    sql: SELECT county, COUNT(*) FROM students GROUP BY county\n"
            "    pending: true\n",
            encoding="utf-8",
        )
        await kb.ensure_synced("test_db")

        r = await client.get("/v1/admin/quality/overview")
        assert r.status_code == 200, r.text
        fb = r.json()["feedback"]
        assert fb["up"] == 5 and fb["down"] == 1
        assert fb["pending_lessons"] == 1 and fb["confirmed_lessons"] == 1
        assert fb["pending_examples"] == 1
        assert fb["by_datasource"] == [{"datasource": "test_db", "up": 5, "down": 1}]
        assert fb["last_rated_at"] == "2026-10-02T08:00:00+00:00"


class TestQualityDecisions:
    """``decisions`` 块(B8):逐源报告,包装 ``eval/quality_report.py``。

    store 缺失 → null + degraded(不 500);接了 → 判定史/效果条目按
    ``(rule_id, rule_rev)`` 分桶;store 坏了 → 只降级该块,其余照答。
    """

    async def _seed(self, api_app, tmp_path):
        from trove.services.decision.verdict_store import VerdictStore
        from trove.services.decision.verdicts import VerdictRecord

        store = VerdictStore(tmp_path / "proj")
        api_app.state.verdicts = store
        for i, (status, trig) in enumerate(
                [("ok", False), ("alert", True), ("alert", True)]):
            await store.record(VerdictRecord(
                datasource="demo", rule_id="revenue_drop", status=status,
                triggered=trig, evidence={"rule_rev": "abc123"},
                evaluated_at=f"2026-10-0{i + 1}T00:00:00+00:00",
            ))
        return store

    async def test_seeded_store_reports_buckets(self, client, api_app, tmp_path):
        store = await self._seed(api_app, tmp_path)
        try:
            r = await client.get("/v1/admin/quality/overview")
            assert r.status_code == 200, r.text
            body = r.json()
            block = body["decisions"]
            assert block["datasources"] == 1
            rep = block["reports"][0]
            assert rep["datasource"] == "demo"
            assert rep["summary"]["total"] == 3 and rep["summary"]["alert"] == 2
            b = rep["buckets"][0]
            assert b["key"] == "revenue_drop@abc123"
            assert b["triggered_rate"] == pytest.approx(2 / 3)
            assert b["effects"]["measured"] == 0
            assert "no_effects" in b["insufficient"]   # 先别读比率,说清为什么
            # 行动层没接 → 记一笔(「没有效果数据」不许冒充「没有效果」)
            dec_deg = [d for d in body["degraded"] if d["block"] == "decisions"]
            assert {d["source"] for d in dec_deg} == {"actions"}
            assert {d["error"] for d in dec_deg} == {"not_configured"}
        finally:
            await store.dispose()

    async def test_effects_join_into_buckets(self, client, api_app, tmp_path):
        from types import SimpleNamespace

        store = await self._seed(api_app, tmp_path)

        class _Effects:
            async def list_effect_entries(self, datasource, *, limit=500):
                return [
                    {"rule_id": "revenue_drop", "rule_rev": "abc123",
                     "outside_band": band, "error": "",
                     "measured_at": f"2026-10-0{i}T00:00:00+00:00"}
                    for i, band in enumerate([True, False, True, False], start=5)
                ]

        api_app.state.actions = SimpleNamespace(store=_Effects())
        try:
            r = await client.get("/v1/admin/quality/overview")
            assert r.status_code == 200, r.text
            body = r.json()
            b = body["decisions"]["reports"][0]["buckets"][0]
            assert b["effects"] == {"measured": 4, "effective": 2,
                                    "no_effect": 2, "unverifiable": 0,
                                    "errors": 0}
            assert b["decided"] == 4 and b["effective_rate"] == 0.5
            # 效果侧够了(4 ≥ MIN_EFFECTS)可读比率;判定侧 3 < LOW_N 仍标
            # few_verdicts —— 两个域的样本门互不代偿
            assert b["insufficient"] == ["few_verdicts"]
            assert [d for d in body["degraded"]
                    if d["block"] == "decisions"] == []
        finally:
            await store.dispose()

    async def test_store_failure_degrades_block_only(self, client, api_app):
        class _Boom:
            async def list_datasources(self):
                raise RuntimeError("boom")

        api_app.state.verdicts = _Boom()
        r = await client.get("/v1/admin/quality/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["decisions"] is None
        entry = next(d for d in body["degraded"] if d["block"] == "decisions")
        assert entry["error"] == "RuntimeError"        # 只报异常类型名
        assert body["feedback"] is not None            # 其余腿不受影响


class TestQualityAuth:
    async def test_non_admin_403_anon_401(self, user_client, anon_client):
        r = await user_client.get("/v1/admin/quality/overview")
        assert r.status_code == 403
        r = await anon_client.get("/v1/admin/quality/overview")
        assert r.status_code == 401
