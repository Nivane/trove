"""``eval/quality_report.py`` 纯函数测试:包装 score.py,口径不重写。

三条纪律各钉一组:
  - 比率只在分母够时给(``insufficient`` 明列原因,呈现层不改判定);
  - 纯派生可复算(同一输入 → 逐字节同一输出;``generated_at`` 由调用方给);
  - 呈现如实(None 比率渲染「—」不折成 0%;``rev_unknown`` 桶单独说明)。
"""

from __future__ import annotations

from trove.eval.quality_report import build_report, fleet_report, render_markdown


def _v(rule_id: str = "r1", status: str = "ok", triggered: bool = False,
       at: str = "2026-10-01T00:00:00+00:00", rev: str = "abc") -> dict:
    return {"rule_id": rule_id, "status": status, "triggered": triggered,
            "evaluated_at": at, "evidence": {"rule_rev": rev}}


def _e(rule_id: str = "r1", rev: str = "abc", outside: object = True,
       error: str = "") -> dict:
    return {"rule_id": rule_id, "rule_rev": rev, "outside_band": outside,
            "error": error, "measured_at": "2026-10-05T00:00:00+00:00"}


class TestBuildReport:
    def test_empty_is_honest_not_zero_rate(self):
        r = build_report([], [], datasource="demo", generated_at="T")
        assert r["datasource"] == "demo" and r["generated_at"] == "T"
        assert r["buckets"] == []
        s = r["summary"]
        assert s["total"] == 0
        assert s["triggered_rate"] is None and s["effective_rate"] is None

    def test_buckets_split_by_rule_rev(self):
        r = build_report([
            _v(rev="v1"), _v(rev="v1", status="alert", triggered=True),
            _v(rev="v2"), _v(rev="v2", status="alert", triggered=True),
        ])
        keys = [b["key"] for b in r["buckets"]]
        assert keys == ["r1@v1", "r1@v2"]           # 按 key 升序,确定性
        v1 = next(b for b in r["buckets"] if b["key"] == "r1@v1")
        assert v1["total"] == 2 and v1["alert"] == 1
        assert v1["triggered_rate"] == 0.5

    def test_missing_rev_lands_in_rev_unknown(self):
        r = build_report([_v(rev="")])
        assert r["buckets"][0]["key"] == "r1@rev_unknown"
        assert r["buckets"][0]["rule_rev"] == "rev_unknown"

    def test_rate_only_when_denominator_enough(self):
        few = build_report([_v()], [_e(outside=True), _e(outside=False)])
        b = few["buckets"][0]
        assert b["decided"] == 2 and b["effective_rate"] is None
        assert "few_effects" in b["insufficient"] and "few_verdicts" in b["insufficient"]

        enough = build_report([_v()],
                              [_e(outside=True), _e(outside=False), _e(outside=True)])
        b = enough["buckets"][0]
        assert b["effective_rate"] == 2 / 3
        assert "few_effects" not in b["insufficient"]

    def test_errors_do_not_count_as_measured(self):
        r = build_report([_v()], [_e(error="boom"), _e(outside=True)])
        eff = r["buckets"][0]["effects"]
        assert eff == {"measured": 1, "effective": 1, "no_effect": 0,
                       "unverifiable": 0, "errors": 1}

    def test_deterministic_byte_for_byte(self):
        args = ([_v(), _v(status="alert", triggered=True)],
                [_e(outside=True), _e(outside=None)])
        a = build_report(*args, datasource="d", generated_at="T")
        b = build_report(*args, datasource="d", generated_at="T")
        assert a == b


class TestFleetReport:
    def test_merge_and_rollup(self):
        r1 = build_report([_v()], [_e(outside=True)], datasource="a")
        r2 = build_report([_v(rev="v2", status="alert", triggered=True)],
                          datasource="b")
        fleet = fleet_report([r1, r2], generated_at="T")
        assert fleet["datasources"] == 2 and fleet["generated_at"] == "T"
        assert fleet["reports"] == [r1, r2]         # 明细原样,不折算
        assert fleet["summary"]["total"] == 2
        assert fleet["summary"]["alert"] == 1
        assert fleet["summary"]["buckets"] == 2

    def test_garbage_entries_dropped_not_crashed(self):
        fleet = fleet_report([None, "x", {"buckets": [{"total": 1}]}])
        assert fleet["datasources"] == 1
        assert fleet["summary"]["total"] == 1

    def test_empty_fleet(self):
        fleet = fleet_report([])
        assert fleet["datasources"] == 0 and fleet["reports"] == []
        assert fleet["summary"]["total"] == 0


class TestRenderMarkdown:
    def test_zh_full_report(self):
        r = build_report(
            [_v(), _v(status="alert", triggered=True)],
            [_e(outside=True), _e(outside=False), _e(outside=True)],
            datasource="demo", generated_at="2026-10-04T00:00:00+00:00")
        md = render_markdown(r, lang="zh")
        assert "## 判定质量报告 — demo" in md
        assert "生成时间: 2026-10-04T00:00:00+00:00" in md
        assert "判定 2 次" in md and "触发率 50.0%" in md
        assert "有效率 66.7%" in md
        assert "| r1@abc |" in md and "判定次数不足" in md

    def test_none_rate_renders_dash_not_zero(self):
        md = render_markdown(build_report([_v()], []), lang="zh")
        assert "有效率 —" in md
        assert "有效率 0.0%" not in md      # 算不出 ≠ 0%(触发率 0.0% 是测到的)
        assert "尚无效果测量" in md

    def test_rev_unknown_note(self):
        md = render_markdown(build_report([_v(rev="")]), lang="zh")
        assert "rev_unknown" in md and "规则版本信息引入之前" in md

    def test_en_rendering(self):
        r = build_report([_v()], [_e(outside=True)], datasource="d")
        md = render_markdown(r, lang="en")
        assert "## Decision quality report — d" in md
        assert "no effect measurements" in md or "too few" in md
        assert "effective rate —" in md           # decided 1 < MIN_EFFECTS

    def test_empty_and_garbage(self):
        md = render_markdown(build_report([]), lang="zh")
        assert "(尚无判定记录)" in md
        assert render_markdown(None) == ""
        assert render_markdown({}) != ""            # 空报告仍渲染骨架

    def test_deterministic(self):
        r = build_report([_v()], [_e()], datasource="d", generated_at="T")
        assert render_markdown(r) == render_markdown(r)
