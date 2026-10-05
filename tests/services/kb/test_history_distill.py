"""历史蒸馏(确定性核心)测试 —— 零 LLM、零网络、tmp SQLite。

覆盖:
- 判定映射(is_success / is_lesson_material / history_lesson_evidence);
- 输入路径白名单(反作弊:``eval/`` 硬拒);
- SQL → 语义候选规格(表驱动,N 类形态;复用 candidates 的保守规则);
- collect_history(合并 / episodes 优先 / 权重排序 / 上限 / since / 空输入);
- run_distill(真实 KB 目录上的落库:pending 门、幂等、上限、dry-run、
  被拒绝的候选不回灌、semantics.yml 逐字节不变)。

服务层测试刻意**不注入任何 gateway**:``history_distill`` 构造上不接收
LLM(判定/蒸馏核心零 LLM 红线的同一条落法,见模块头),StructuralTests
把这件事钉在签名与 import 面上。
"""

from __future__ import annotations

import yaml
import pytest

from trove.services.auth.store import AppDbStore
from trove.services.kb.history_distill import (
    MAX_DISTILL_PER_RUN,
    HistoryDistillError,
    HistoryRecord,
    collect_history,
    history_lesson_evidence,
    is_lesson_material,
    is_success,
    run_distill,
    sql_candidate_specs,
    validate_source_path,
)
from trove.services.kb.service import KbService
from trove.services.lineage.service import LineageService
from trove.services.memory.episode import EpisodeStore, now_iso
from trove.services.memory.models import MemoryScope
from trove.services.semantic_layer.manage import SemanticManager
from trove.services.semantic_layer.ossie import parse_ossie


# 数据集名 ≠ 物理表名(loans ← loan):钉住「表名归一为数据集名」这步历史特化。
def _doc(metrics: list[dict] | None = None) -> dict:
    return {
        "version": "0.2.0.dev0",
        "semantic_model": [{
            "name": "demo",
            "datasets": [
                {
                    "name": "loans", "source": "loan",
                    "primary_key": ["loan_id"],
                    "fields": [
                        {"name": "loan_id", "datatype": "Integer",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "loan_id"}]}},
                        {"name": "amount", "datatype": "Decimal",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "amount"}]}},
                    ],
                },
                {
                    "name": "districts", "source": "district",
                    "primary_key": ["A1"],
                    "fields": [
                        {"name": "A3", "datatype": "String",
                         "expression": {"dialects": [{
                             "dialect": "ANSI_SQL", "expression": "A3"}]}},
                    ],
                },
            ],
            "metrics": metrics or [],
        }],
    }


def _model(metrics: list[dict] | None = None):
    return parse_ossie(yaml.safe_dump(_doc(metrics), allow_unicode=True))


@pytest.fixture
def kb(tmp_path):
    k = KbService(tmp_path / "proj")
    ds_dir = k.kb_dir / "demo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "semantics.yml").write_text(
        yaml.safe_dump(_doc(), allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return k


# ── 判定映射(纯函数,表驱动) ─────────────────────────────


class TestVerdictMapping:
    @pytest.mark.parametrize("verdict,error,corrections,expected", [
        ("OK", "", [], True),
        ("OK: 3 rows", "", [], True),          # 首词口径:OK 前缀都算交付
        ("EMPTY", "", [], True),
        ("", "", [], True),                     # 空判定 + 无错无修正 = 交付过
        ("", "boom", [], False),
        ("", "", ["把 status 当数字"], False),
        ("RETRY: validation failed", "", [], False),
        ("NO_SQL", "", [], False),
        ("REFUSED", "", [], False),
    ])
    def test_is_success(self, verdict, error, corrections, expected):
        rec = HistoryRecord(question="q", sql="SELECT 1", verdict=verdict,
                            error=error, corrections=corrections)
        assert is_success(rec) is expected

    @pytest.mark.parametrize("verdict,corrections,expected", [
        ("OK", [], False),                      # 干净成功:不是教训材料
        ("OK", ["修正过"], True),                # 成功但反复纠正 → 值得沉淀
        ("RETRY: 空结果", [], True),
        ("REFUSED", [], True),
    ])
    def test_is_lesson_material(self, verdict, corrections, expected):
        rec = HistoryRecord(question="q", sql="SELECT 1", verdict=verdict,
                            corrections=corrections)
        assert is_lesson_material(rec) is expected

    def test_evidence_maps_corrections_as_failure_context(self):
        rec = HistoryRecord(
            question="贷款余额为负的客户", sql="SELECT 1",
            verdict="RETRY: 空结果",
            corrections=["第一次把 status 当数字比较", "第二次漏了 join 条件"],
        )
        ev = history_lesson_evidence(rec)
        assert ev["question"] == "贷款余额为负的客户"
        assert ev["pred_sql"] == "SELECT 1"
        assert ev["gold_sql"] == ""             # 历史里没有 gold,不如实造
        assert ev["evidence"] == "第一次把 status 当数字比较\n第二次漏了 join 条件"
        assert ev["error"] == "RETRY: 空结果"    # 无 error 时回落 verdict

    def test_evidence_falls_back_to_error_without_corrections(self):
        rec = HistoryRecord(question="q", sql="SELECT 2",
                            verdict="", error="no such table: loans")
        ev = history_lesson_evidence(rec)
        assert ev["evidence"] == "no such table: loans"
        assert ev["error"] == "no such table: loans"


# ── 输入路径白名单(反作弊红线) ───────────────────────────


class TestSourcePathWhitelist:
    def test_allows_three_behavior_roots(self, tmp_path):
        home = tmp_path / "home"
        proj = tmp_path / "proj"
        for p in (
            home / "memory" / "episodes.sqlite",
            home / "memory" / "nested" / "any.db",
            home / "memory",
            home / "app.db",
            proj / ".trove" / "lineage" / "lineage.sqlite",
            proj / ".trove" / "lineage",
        ):
            assert validate_source_path(
                p, home_dir=home, project_root=proj) == p.resolve()

    def test_rejects_eval_products(self, tmp_path):
        """KB 反作弊的入口闸:评测产物绝不进蒸馏(不是文档里的君子协定)。"""
        home = tmp_path / "home"
        proj = tmp_path / "proj"
        for p in (
            proj / ".trove" / "eval" / "results.jsonl",
            proj / ".trove" / "eval",
            proj / "eval" / "results.jsonl",
        ):
            with pytest.raises(HistoryDistillError, match="eval/"):
                validate_source_path(p, home_dir=home, project_root=proj)

    def test_rejects_outside_and_prefix_traps(self, tmp_path):
        home = tmp_path / "home"
        proj = tmp_path / "proj"
        for p in (
            tmp_path / "elsewhere" / "app.db",
            home / "memory_evil" / "x.db",       # 前缀相似 ≠ 在白名单内
            home / "app.db.bak",
            proj / ".trove" / "lineage_secret" / "x.db",
            proj / ".trove" / "jobs" / "jobs.sqlite",
        ):
            with pytest.raises(HistoryDistillError):
                validate_source_path(p, home_dir=home, project_root=proj)


# ── SQL → 语义候选规格(纯函数,表驱动) ───────────────────


class TestSQLCandidateSpecs:
    @pytest.mark.parametrize("sql,expected", [
        # 表别名限定 → 按别名解析到基表,再归一到数据集名
        ("SELECT AVG(l.amount) FROM loan l", ["avg_amount"]),
        # 物理表名限定(数据集名 ≠ 表名)→ 按 source 映射
        ("SELECT SUM(loan.amount) FROM loan", ["sum_amount"]),
        # 未声明字段:metric 候选 + field 候选(metric 优先)
        ("SELECT COUNT(l.loan_id) FROM loan l WHERE l.status = 'A'",
         ["count_loan_id", "loans.status"]),
        # 已声明字段不重复起草
        ("SELECT l.amount FROM loan l", []),
        # CTE 遮蔽数据集名 → 不锚(不猜)
        ("WITH loans AS (SELECT 1 AS amount) "
         "SELECT AVG(loans.amount) FROM loans", []),
        # 裸列(无非限定引用可锚定)→ 不猜
        ("SELECT SUM(amount) FROM loan", []),
        # 多聚合算式(比值型):名字推导有歧义 → 交人工
        ("SELECT SUM(l.amount) / COUNT(l.loan_id) FROM loan l", []),
        # 未声明数据集 → 锚不住
        ("SELECT AVG(g.amount) FROM ghost g", []),
        # 别名歧义(同名别名指两张表)→ 别名整体作废
        ("SELECT AVG(a.amount) FROM loan a JOIN district a ON a.A3 = a.A3", []),
        # COUNT(*) 无列可锚
        ("SELECT COUNT(*) FROM loan", []),
        # 同投影重复 → (kind, name) 去重只留一条
        ("SELECT AVG(l.amount), AVG(l.amount) FROM loan l", ["avg_amount"]),
    ])
    def test_forms(self, sql, expected):
        specs = sql_candidate_specs(sql, _model())
        assert [s["name"] for s in specs] == expected

    def test_window_expression_is_not_a_metric(self):
        """窗口算式不是度量声明(口径归人工),但其列引用仍可出 field 候选。"""
        specs = sql_candidate_specs(
            "SELECT SUM(l.amount) OVER (PARTITION BY l.status) AS s FROM loan l",
            _model())
        assert [s["name"] for s in specs] == ["loans.status"]

    def test_spec_shape_and_history_reasons(self):
        """payload 是可应用的声明(数据集限定表达式),reason 如实标来路。"""
        specs = sql_candidate_specs("SELECT AVG(l.amount) FROM loan l", _model())
        assert specs == [{
            "kind": "metric", "name": "avg_amount",
            "payload": {"expression": "AVG(loans.amount)", "datasets": ["loans"]},
            "reason": "history_metric_usage",
        }]
        fields = sql_candidate_specs(
            "SELECT COUNT(l.loan_id) FROM loan l WHERE l.status = 'A'", _model())
        assert fields[1] == {
            "kind": "field", "name": "loans.status",
            "payload": {"expression": "loans.status"},
            "reason": "history_field_usage",
        }

    def test_taken_name_never_reproposed(self):
        model = _model(metrics=[{
            "name": "avg_amount",
            "expression": {"dialects": [{
                "dialect": "ANSI_SQL", "expression": "AVG(loans.amount)"}]},
        }])
        assert sql_candidate_specs(
            "SELECT AVG(l.amount) FROM loan l", model) == []

    def test_unparseable_or_missing_input(self):
        assert sql_candidate_specs("SELEC * FORM loan", _model()) == []
        assert sql_candidate_specs("", _model()) == []
        assert sql_candidate_specs("SELECT AVG(l.amount) FROM loan l", None) == []


# ── collect_history(合并 / 权重 / 上限 / since / 空) ───────


class TestCollectHistory:
    @staticmethod
    async def _seed_episode(home, question, sql, *, user="alice",
                            datasource="demo", verdict="OK", corrections=None):
        store = EpisodeStore(home / "memory" / "episodes.sqlite")
        await store.record(
            MemoryScope(datasource=datasource, user_id=user),
            question=question, sql=sql, verdict=verdict,
            correction_history=corrections or [],
        )

    @staticmethod
    async def _seed_audit(auth, question, sql, *, datasource="demo",
                          verdict="OK", error="", times=1):
        for _ in range(times):
            await auth.append_audit(
                ts=now_iso(), user_id=None, username="bob",
                action="query.execute",
                details={"question": question, "sql": sql, "verdict": verdict,
                         "error": error, "datasource": datasource},
            )

    async def test_merge_episodes_first_and_audit_fallback(self, tmp_path):
        home, proj = tmp_path / "home", tmp_path / "proj"
        proj.mkdir()
        await self._seed_episode(home, "q1", "SELECT 1", corrections=["fix"])
        auth = AppDbStore(home / "app.db")
        await self._seed_audit(auth, "q1", "SELECT 1")          # 与 episode 重复
        await self._seed_audit(auth, "q2", "SELECT 2", times=2)  # 只在审计里
        await self._seed_audit(auth, "q3", "SELECT 3", datasource="other")

        records = await collect_history(
            "demo", home_dir=home, project_root=proj, auth_store=auth)
        assert sorted(r.question for r in records) == ["q1", "q2"]
        q1 = next(r for r in records if r.question == "q1")
        assert q1.source == "episode"                    # episodes 优先(信息更多)
        assert q1.corrections == ["fix"]
        q2 = next(r for r in records if r.question == "q2")
        assert q2.source == "audit"
        assert q2.runs == 1                              # 审计窗口内 seen=2 → 权重 1

    async def test_lineage_runs_are_the_ranking_weight(self, tmp_path):
        home, proj = tmp_path / "home", tmp_path / "proj"
        proj.mkdir()
        await self._seed_episode(home, "冷问题", "SELECT amount FROM loan")
        await self._seed_episode(home, "热问题", "SELECT A3 FROM district")
        lineage = LineageService(proj)
        for _ in range(3):
            await lineage.record_query("SELECT A3 FROM district", "demo",
                                       dialect="sqlite")

        records = await collect_history("demo", home_dir=home, project_root=proj)
        assert [r.question for r in records] == ["热问题", "冷问题"]  # 高频先保住
        assert records[0].runs == 3
        assert records[1].runs == 0

    async def test_limit_caps_and_clamps(self, tmp_path):
        home, proj = tmp_path / "home", tmp_path / "proj"
        proj.mkdir()
        store = EpisodeStore(home / "memory" / "episodes.sqlite")
        scope = MemoryScope(datasource="demo", user_id="alice")
        for i in range(60):
            await store.record(scope, question=f"q{i}", sql=f"SELECT {i}",
                               verdict="OK")
        records = await collect_history("demo", home_dir=home,
                                        project_root=proj, limit=50)
        assert len(records) == 50
        # 传超过 MAX 的值也被收敛到 MAX_DISTILL_PER_RUN(显式上限)
        assert len(await collect_history(
            "demo", home_dir=home, project_root=proj,
            limit=MAX_DISTILL_PER_RUN * 20)) == MAX_DISTILL_PER_RUN

    async def test_since_filters_and_empty_is_empty(self, tmp_path):
        home, proj = tmp_path / "home", tmp_path / "proj"
        proj.mkdir()
        # 空输入:不造文件、不报成功(上层 CLI 显式报「无历史」)
        assert await collect_history("demo", home_dir=home,
                                     project_root=proj) == []
        await self._seed_episode(home, "q1", "SELECT 1")
        future = "2999-01-01T00:00:00+00:00"
        assert await collect_history("demo", home_dir=home, project_root=proj,
                                     since=future) == []
        assert len(await collect_history("demo", home_dir=home,
                                         project_root=proj)) == 1

    async def test_blank_datasource_or_zero_limit(self, tmp_path):
        home, proj = tmp_path / "home", tmp_path / "proj"
        proj.mkdir()
        await self._seed_episode(home, "q1", "SELECT 1")
        assert await collect_history("", home_dir=home, project_root=proj) == []
        assert await collect_history("demo", home_dir=home, project_root=proj,
                                     limit=0) == []


# ── run_distill(真实 KB 目录) ─────────────────────────────


def _rec(question, sql, verdict="OK", **kw):
    return HistoryRecord(question=question, sql=sql, verdict=verdict, **kw)


TOP_REGION_SQL = ("SELECT l.district_id, AVG(l.amount) FROM loan l "
                  "GROUP BY l.district_id")


class TestRunDistill:
    async def test_pending_only_gates_and_semantics_untouched(self, kb):
        sem_path = kb.kb_dir / "demo" / "semantics.yml"
        before = sem_path.read_text(encoding="utf-8")

        summary = await run_distill(kb, "demo", [
            _rec("贷款金额最高的地区?", TOP_REGION_SQL),
        ])
        assert summary["examples"] == 1
        assert summary["candidates"] == 2
        assert sorted(i["name"] for i in summary["candidate_items"]) == [
            "avg_amount", "loans.district_id"]

        # 门位 1:示例落 pending、不进检索;来路标记在案
        pending = await kb.list_pending_examples("demo")
        assert [ex["question"] for ex in pending] == ["贷款金额最高的地区?"]
        assert pending[0]["tags"] == ["history"]
        assert await kb.search_examples("贷款金额最高的地区?", "demo", limit=5) == []

        # 门位 2:语义候选只进草案队列,semantics.yml 逐字节不变
        drafts = SemanticManager(kb).drafts("demo")
        assert len(drafts["pending"]) == 2
        assert drafts["applied"] == [] and drafts["rejected"] == []
        assert all(d["status"] == "pending" for d in drafts["pending"])
        assert any(str(d["note"]).startswith(
            "auto:history_metric_usage:") for d in drafts["pending"])
        assert sem_path.read_text(encoding="utf-8") == before
        assert SemanticManager(kb).model("demo").metrics == []

        # 正面:确认后真的进检索 / 真的长进模型(payload 是可应用声明)
        manager = SemanticManager(kb)
        for entry in drafts["pending"]:
            await manager.confirm_draft("demo", entry["id"], dialect="sqlite")
        assert await kb.confirm_pending_examples("demo", actor="admin") == 1
        hits = await kb.search_examples("贷款金额最高的地区?", "demo", limit=5)
        assert [h.question for h in hits] == ["贷款金额最高的地区?"]
        model = manager.model("demo")
        assert "avg_amount" in {m.name for m in model.metrics}
        loan = next(d for d in model.datasets if d.name == "loans")
        assert "district_id" in {f.name for f in loan.fields}

    async def test_rerun_is_idempotent(self, kb):
        records = [
            _rec("贷款金额最高的地区?", TOP_REGION_SQL),
            _rec("有多少笔贷款?", "SELECT COUNT(l.loan_id) FROM loan l"),
        ]
        first = await run_distill(kb, "demo", records)
        assert first["examples"] == 2 and first["candidates"] == 3
        examples_yml = (kb.kb_dir / "demo" / "examples.yml").read_text(
            encoding="utf-8")
        drafts_yml = (kb.kb_dir / "demo" / "semantic_drafts.yml").read_text(
            encoding="utf-8")

        second = await run_distill(kb, "demo", records)
        assert second["examples"] == 0 and second["examples_skipped"] == 2
        assert second["candidates"] == 0 and second["candidates_skipped"] == 3
        assert (kb.kb_dir / "demo" / "examples.yml").read_text(
            encoding="utf-8") == examples_yml
        assert (kb.kb_dir / "demo" / "semantic_drafts.yml").read_text(
            encoding="utf-8") == drafts_yml

    async def test_rejected_candidate_never_reproposed(self, kb):
        records = [_rec("贷款金额最高的地区?", TOP_REGION_SQL)]
        assert (await run_distill(kb, "demo", records))["candidates"] == 2

        manager = SemanticManager(kb)
        field_draft = next(d for d in manager.drafts("demo")["pending"]
                           if d["kind"] == "field")
        await manager.reject_draft("demo", field_draft["id"])

        again = await run_distill(kb, "demo", records)
        # metric 因 pending 跳过、field 因 rejected 跳过 —— 拒绝不会是回灌的缺口
        assert again["candidates"] == 0 and again["candidates_skipped"] == 2
        assert len(manager.drafts("demo")["rejected"]) == 1

    async def test_dry_run_writes_nothing(self, kb):
        summary = await run_distill(
            kb, "demo", [_rec("贷款金额最高的地区?", TOP_REGION_SQL)],
            dry_run=True)
        assert summary["examples"] == 1 and summary["candidates"] == 2
        assert summary["dry_run"] is True
        demo = kb.kb_dir / "demo"
        assert not (demo / "examples.yml").exists()
        assert not (demo / "semantic_drafts.yml").exists()
        # 空库强制同步镜像后检索:确实什么都没进检索
        await kb.force_sync("demo")
        assert await kb.search_examples("贷款金额最高的地区?", "demo") == []

    async def test_max_items_caps_examples_and_candidates(self, kb):
        records = [
            _rec("q1", "SELECT SUM(l.amount) FROM loan l"),
            _rec("q2", "SELECT AVG(l.amount) FROM loan l"),
            _rec("q3", "SELECT MAX(l.amount) FROM loan l"),
            _rec("q4", "SELECT MIN(l.amount) FROM loan l"),
            _rec("q5", "SELECT COUNT(l.loan_id) FROM loan l"),
        ]
        summary = await run_distill(kb, "demo", records, max_items=2)
        assert summary["examples"] == 2
        assert summary["candidates"] == 2

    async def test_noop_without_kb_records_or_model(self, kb, tmp_path):
        assert (await run_distill(None, "demo", [_rec("q", "SELECT 1")]))[
            "examples"] == 0
        assert (await run_distill(kb, "demo", []))["examples"] == 0
        # 无语义模型的库:示例照落(不依赖模型),候选为零(无从锚定)
        bare = KbService(tmp_path / "bare")
        summary = await run_distill(bare, "demo", [_rec("q", "SELECT 1")])
        assert summary["examples"] == 1 and summary["candidates"] == 0

    async def test_failed_records_never_become_examples(self, kb):
        summary = await run_distill(kb, "demo", [
            _rec("坏问题", "SELECT 1", verdict="RETRY: 空结果"),
            _rec("无 SQL", "", verdict=""),
        ])
        assert summary["examples"] == 0
        assert await kb.list_pending_examples("demo") == []

    async def test_lesson_material_lands_pending_and_out_of_ranking(self, kb):
        """CLI 教训段的确定性半边:材料筛选 → append_lesson → 排序门。"""
        rec = _rec("贷款余额为负的客户", "SELECT 1", verdict="RETRY: 空结果",
                   corrections=["第一次把 status 当数字比较"])
        assert is_lesson_material(rec)
        ev = history_lesson_evidence(rec)
        await kb.append_lesson(
            {"pattern": "status 是字符串枚举,比较要用字符串字面量",
             "note": "correction_history 里反复出现的错",
             "evidence": ev["evidence"],
             "confirmed": False},
            "demo", generator="history_distill",
        )
        query = "status 是字符串枚举,比较要用字符串字面量"
        # 门位 3:pending 教训不进检索/不进排序
        assert await kb.search_lessons(query, "demo") == []
        assert await kb.list_lessons("demo") == []
        assert len(await kb.list_lessons("demo", confirmed_only=False)) == 1
        # 确认后进检索
        assert await kb.confirm_lesson("demo", "status 是字符串枚举,比较要用字符串字面量")
        hits = await kb.search_lessons(query, "demo")
        assert len(hits) == 1


# ── 教训提炼(注入 fake llm;CLI 与管理端共用管线) ────────


def _lesson_json(pattern: str, note: str) -> str:
    import json
    return json.dumps({"pattern": pattern, "note": note,
                       "sql_snippet": "SELECT 1"}, ensure_ascii=False)


class _FakeLLM:
    """脚本化网关替身:按序吐响应;Exception 项 = 该次调用抛错。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        r = self.responses.pop(0) if self.responses else ""
        if isinstance(r, Exception):
            raise r
        return r


class TestRunLessonDistill:
    """run_lesson_distill:材料筛选 → 解析/噪声过滤 → pattern 去重 → pending 落库。

    零网络:llm 是注入的脚本化替身(模块自身不 import 网关,见下方结构测试)。
    """

    async def _run(self, kb, records, responses, **kw):
        from trove.services.kb.history_distill import run_lesson_distill

        llm = _FakeLLM(responses)
        out = await run_lesson_distill(
            kb, "demo", records, llm=llm, model="draft/model", **kw)
        return out, llm

    async def test_lands_pending_lessons_out_of_ranking(self, kb):
        rec = _rec("贷款余额为负的客户有哪些?", "SELECT 1",
                   verdict="RETRY: 空结果", corrections=["把 status 当数字比较"])
        out, llm = await self._run(kb, [rec], [
            _lesson_json("贷款余额为负的客户", "status 是字符串枚举,比较用字符串字面量"),
        ])
        assert out["error"] == ""
        assert out["material"] == 1 and out["lessons"] == 1
        assert out["duplicates"] == 0 and out["items"][0]["pattern"] == "贷款余额为负的客户"
        assert len(llm.calls) == 1
        assert llm.calls[0]["model"] == "draft/model"
        # 提示词走既有管线:user 消息带问题原文
        assert "贷款余额为负的客户有哪些?" in llm.calls[0]["messages"][1]["content"]
        # 门位:pending 不进检索;confirmed_only=False 可见且 confirmed=False
        all_lessons = await kb.list_lessons("demo", confirmed_only=False)
        assert len(all_lessons) == 1 and all_lessons[0]["confirmed"] is False
        assert await kb.list_lessons("demo") == []
        assert await kb.search_lessons("贷款余额为负的客户", "demo") == []

    async def test_parse_fail_and_noise_are_counted_skips(self, kb):
        recs = [
            _rec("问题一怎么了", "SELECT 1", verdict="RETRY: 空结果"),
            _rec("问题二怎么了", "SELECT 1", verdict="RETRY: 空结果"),
            _rec("问题三怎么了", "SELECT 1", verdict="RETRY: 空结果"),
        ]
        out, _ = await self._run(kb, recs, [
            "not json at all",                      # parse 失败
            _lesson_json("完全不在这句里出现", "x"),   # pattern 不在问题原文 → 噪声
            _lesson_json("问题三怎么了", "答案三"),
        ])
        assert out["parse_failed"] == 1 and out["noise"] == 1
        assert out["lessons"] == 1 and len(out["items"]) == 1

    async def test_rerun_is_idempotent(self, kb):
        rec = _rec("重复蒸馏的问题", "SELECT 1", verdict="RETRY: 空结果")
        resp = _lesson_json("重复蒸馏的问题", "只该落一次")
        out1, _ = await self._run(kb, [rec], [resp])
        assert len(out1["items"]) == 1
        out2, _ = await self._run(kb, [rec], [resp])
        assert out2["lessons"] == 1 and out2["duplicates"] == 1
        assert out2["items"] == []
        assert len(await kb.list_lessons("demo", confirmed_only=False)) == 1

    async def test_llm_failure_writes_nothing_and_reports(self, kb):
        recs = [
            _rec("第一条失败记录", "SELECT 1", verdict="RETRY: 空结果"),
            _rec("第二条失败记录", "SELECT 1", verdict="RETRY: 空结果"),
        ]
        out, _ = await self._run(kb, recs, [
            _lesson_json("第一条失败记录", "先成功"),
            RuntimeError("model down"),
        ])
        # 写入只在全部提炼完成后发生:报错时磁盘零改动(不半写)
        assert out["error"] == "model down"
        assert out["items"] == []
        # 先同步镜像再读(镜像表由 sync 建立;若 lessons.yml 真被写过,
        # sync 会把它读进来 → 空读 = 磁盘上确实没有)
        await kb.ensure_synced("demo")
        assert await kb.list_lessons("demo", confirmed_only=False) == []

    async def test_dry_run_lists_but_writes_nothing(self, kb):
        rec = _rec("演练的问题", "SELECT 1", verdict="RETRY: 空结果")
        out, _ = await self._run(
            kb, [rec], [_lesson_json("演练的问题", "不该落盘")], dry_run=True)
        assert out["dry_run"] is True and len(out["items"]) == 1
        assert await kb.list_lessons("demo", confirmed_only=False) == []
        assert not (kb.kb_dir / "demo" / "lessons.yml").exists()

    async def test_success_records_are_not_material(self, kb):
        out, llm = await self._run(
            kb, [_rec("干净成功", "SELECT 1", verdict="OK")], [])
        assert out["material"] == 0 and llm.calls == []

    async def test_max_items_caps_material(self, kb):
        recs = [_rec(f"失败问题{i}", "SELECT 1", verdict="RETRY: 空结果")
                for i in range(5)]
        out, llm = await self._run(
            kb, recs, [_lesson_json(f"失败问题{i}", f"教训{i}") for i in range(2)],
            max_items=2)
        assert out["material"] == 2 and len(llm.calls) == 2

    async def test_missing_llm_is_an_error_not_a_silent_pass(self, kb):
        from trove.services.kb.history_distill import run_lesson_distill

        out = await run_lesson_distill(
            kb, "demo", [_rec("q", "SELECT 1", verdict="RETRY: x")],
            llm=None, model="m")
        assert out["error"]


# ── 结构性约束:核心不可能调 LLM ──────────────────────────


class TestNoLLMStructural:
    def test_module_does_not_import_the_gateway(self):
        import inspect

        from trove.services.kb import history_distill as hd
        src = inspect.getsource(hd)
        assert "trove.llm" not in src
        assert "LLMGateway" not in src

    def test_entry_points_take_no_llm_parameters(self):
        import inspect

        from trove.services.kb import history_distill as hd
        for fn in (hd.run_distill, hd.collect_history, hd.sql_candidate_specs):
            params = set(inspect.signature(fn).parameters)
            assert not {p for p in params
                        if "llm" in p.lower() or "gateway" in p.lower()}, fn
