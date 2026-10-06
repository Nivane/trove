"""装前试跑(E3)—— 语料层 + 双态消融,零 LLM 硬门。

分节对应交付面:fixtures 解析(格式错响亮拒掉)/ episodes 适配(没有结果行
就如实 skipped)/ 双态消融(装与不装之差)/ guard 档接缝(E2 接通后走真实
``run_guards``;降级树缺席时整档 skipped,不假绿)/ 退出码三分支(0 无回归 ·
1 有拦截变更或断言失败 · 2 无法试跑 —— 绝不静默 0)。

``_llm_forbidden`` 把「零 LLM」钉成机制:试跑路径上任何一次 LLM 调用都会
让测试直接炸 —— 这不是纪律(靠人记得),是电路(靠 fixture 断)。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from trove.services.extensions import dryrun as dr
from trove.services.extensions.dryrun import (
    SKIP_REASONS,
    CorpusItem,
    DryRunReport,
    JudgmentRow,
    gather_corpus,
    judge_guard,
    judge_validator,
    load_episodes_corpus,
    load_fixtures_file,
    load_guard_tier,
    merge_corpus,
    resolve_fixtures_path,
    run_dryrun,
    validator_specs_for,
)
from trove.services.skills.service import SkillService


@pytest.fixture(autouse=True)
def _llm_forbidden(monkeypatch):
    """零 LLM 硬门:试跑路径上任何一次 LLM 调用 = 测试直接失败。"""
    from trove.llm.gateway import LLMGateway

    def _boom(*args, **kwargs):
        raise AssertionError("dryrun 路径不允许调用 LLM")

    monkeypatch.setattr(LLMGateway, "chat", _boom, raising=False)
    monkeypatch.setattr(LLMGateway, "chat_full", _boom, raising=False)
    monkeypatch.setattr(LLMGateway, "chat_stream", _boom, raising=False)


# ── helpers ──────────────────────────────────────────────

_GOOD_YAML = """
items:
  - question: "How many loans?"
    sql: "SELECT COUNT(*) AS loan_count FROM loan"
    dialect: sqlite
    columns: [loan_count]
    rows:
      - [3]
    verdict: pass
"""


def _write(tmp_path, text: str, name: str = "fixtures.yml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _item(**kw) -> CorpusItem:
    base = dict(question="q", sql="SELECT 1", dialect="sqlite")
    base.update(kw)
    return CorpusItem(**base)


def _result_item(rows=None, columns=None, **kw) -> CorpusItem:
    return _item(
        rows=[[3]] if rows is None else rows,
        columns=["loan_count"] if columns is None else columns,
        **kw,
    )


def _svc(tmp_path, *, config=None) -> SkillService:
    return SkillService(root=tmp_path / ".trove" / "skills", config=config)


def _make_validator(
    svc: SkillService, name="must-catch", *,
    expr="min >= 100", columns=("loan_count",), severity="blocking",
    confirm=True, triggers=None,
):
    """落一份真实 validator 资产(走 create 的写入校验 + 确认门)。"""
    entry = {
        "name": name, "description": f"{name} 检查",
        "tier": "validator", "severity": severity, "targets": ["result"],
        "checks": [{"expr": expr, "columns": list(columns), "message": "违反"}],
        "body": "人类可读说明",
    }
    if triggers is not None:
        entry["triggers"] = triggers
    svc.create(entry)
    if confirm:
        svc.confirm(name)
    return name


def _row(state="covered", *, post_blocked=(), assertion="", tier="validator",
         index=0, post="pass", pre="pass") -> JudgmentRow:
    return JudgmentRow(
        index=index, question_id="abcd1234", question="", source="fixtures:x",
        tier=tier, state=state, pre=pre, post=post,
        post_blocked_by=tuple(post_blocked), assertion=assertion,
    )


class _FakeEpisodes:
    """注入用的假 episode store:记录调用、可注入异常/行。"""

    def __init__(self, rows=None, exc=None):
        self.rows = rows or []
        self.exc = exc
        self.calls: list[tuple] = []
        self.disposed = False

    async def iter_episodes(self, datasource, limit=200):
        self.calls.append((datasource, limit))
        if self.exc is not None:
            raise self.exc
        return list(self.rows)

    async def dispose(self):
        self.disposed = True


# ── fixtures 解析 ─────────────────────────────────────────


def test_fixtures_parses_full_item(tmp_path):
    items, errors = load_fixtures_file(_write(tmp_path, _GOOD_YAML))
    assert errors == []
    assert len(items) == 1
    it = items[0]
    assert it.question == "How many loans?"
    assert it.sql.startswith("SELECT COUNT(*)")
    assert it.dialect == "sqlite"
    assert it.columns == ["loan_count"]
    assert it.rows == [[3]]
    assert it.expected == "pass"
    assert it.source.startswith("fixtures:")


def test_fixtures_minimal_item(tmp_path):
    """只有 question 也合法 —— 语料能力决定可回放面,不决定能否入库。"""
    items, errors = load_fixtures_file(
        _write(tmp_path, 'items:\n  - question: "只有问题"\n'))
    assert errors == []
    assert items[0].has_result is False
    assert items[0].sql == ""
    assert items[0].expected == ""


def test_fixtures_missing_file(tmp_path):
    items, errors = load_fixtures_file(tmp_path / "nope.yml")
    assert items == []
    assert len(errors) == 1 and "不存在" in errors[0]


def test_fixtures_root_must_be_mapping(tmp_path):
    _, errors = load_fixtures_file(_write(tmp_path, "- a\n- b\n"))
    assert errors and "mapping" in errors[0]


def test_fixtures_items_must_be_list(tmp_path):
    _, errors = load_fixtures_file(_write(tmp_path, "items: {a: 1}\n"))
    assert errors and "items" in errors[0]


def test_fixtures_item_must_be_mapping(tmp_path):
    _, errors = load_fixtures_file(_write(tmp_path, 'items:\n  - "just a string"\n'))
    assert errors and "items[0]" in errors[0]


def test_fixtures_question_required(tmp_path):
    _, errors = load_fixtures_file(_write(tmp_path, "items:\n  - sql: SELECT 1\n"))
    assert errors and "question 必填" in errors[0]


def test_fixtures_bad_verdict_lists_closed_set(tmp_path):
    _, errors = load_fixtures_file(
        _write(tmp_path, 'items:\n  - question: q\n    verdict: maybe\n'))
    assert errors and "pass" in errors[0] and "maybe" in errors[0]


def test_fixtures_rows_require_columns(tmp_path):
    _, errors = load_fixtures_file(
        _write(tmp_path, "items:\n  - question: q\n    rows:\n      - [1]\n"))
    assert errors and "成对" in errors[0]


def test_fixtures_columns_require_rows(tmp_path):
    _, errors = load_fixtures_file(
        _write(tmp_path, "items:\n  - question: q\n    columns: [n]\n"))
    assert errors and "成对" in errors[0]


def test_fixtures_columns_must_be_str_list(tmp_path):
    _, errors = load_fixtures_file(_write(
        tmp_path,
        "items:\n  - question: q\n    columns: [1]\n    rows:\n      - [1]\n"))
    assert errors and "columns" in errors[0]


def test_fixtures_rows_must_be_list_of_lists(tmp_path):
    _, errors = load_fixtures_file(_write(
        tmp_path,
        "items:\n  - question: q\n    columns: [n]\n    rows: [1, 2]\n"))
    assert errors and "rows" in errors[0]


def test_fixtures_empty_file_is_loud(tmp_path):
    _, errors = load_fixtures_file(_write(tmp_path, ""))
    assert errors and "为空" in errors[0]


def test_fixtures_invalid_yaml_is_loud(tmp_path):
    _, errors = load_fixtures_file(_write(tmp_path, "items: [\n"))
    assert errors and "无法解析" in errors[0]


def test_fixtures_good_items_survive_bad_ones(tmp_path):
    """格式错不吞好条目 —— 错误照报(退出码 2),但已解析的条目仍然入语料。"""
    items, errors = load_fixtures_file(_write(
        tmp_path,
        'items:\n  - question: good\n    columns: [n]\n    rows:\n      - [1]\n'
        '  - question: bad\n    verdict: nope\n'))
    assert [i.question for i in items] == ["good"]
    assert len(errors) == 1


# ── CorpusItem ───────────────────────────────────────────


def test_question_id_is_stable_hash_not_text():
    it = _item(question="How many loans?")
    assert it.question_id == hashlib.sha256(b"How many loans?").hexdigest()[:8]
    assert it.question_id == _item(question="How many loans?").question_id
    assert it.question_id != _item(question="Different?").question_id
    assert "loan" not in it.question_id


def test_has_result_needs_both_sides():
    assert _item().has_result is False
    assert _item(rows=[[1]]).has_result is False
    assert _item(columns=["n"]).has_result is False
    assert _item(rows=[[1]], columns=["n"]).has_result is True


def test_dedupe_key_normalizes_case_and_spaces():
    a = _item(question="  How many Loans? ", sql="SELECT 1")
    b = _item(question="how many loans?", sql="SELECT 1")
    assert a.dedupe_key == b.dedupe_key


# ── resolve_fixtures_path ────────────────────────────────


def test_fixtures_path_auto(tmp_path):
    p, explicit = resolve_fixtures_path(tmp_path, "demo", "auto")
    assert p == tmp_path / ".trove" / "kb" / "demo" / "fixtures.yml"
    assert explicit is False


def test_fixtures_path_explicit(tmp_path):
    target = tmp_path / "my.yml"
    p, explicit = resolve_fixtures_path(tmp_path, "demo", str(target))
    assert p == target and explicit is True


def test_fixtures_path_no_datasource(tmp_path):
    p, explicit = resolve_fixtures_path(tmp_path, "", "auto")
    assert p is None and explicit is False


# ── episodes 适配 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_episodes_rows_become_corpus_without_results(tmp_path):
    store = _FakeEpisodes(rows=[
        {"question": "How many loans?", "sql": "SELECT 1", "dialect": "sqlite"},
        {"question": "  ", "sql": "SELECT 2"},          # 空问题被丢掉
    ])
    items, summary, errors = await load_episodes_corpus(
        "demo", home_dir=tmp_path, limit=50, store=store)
    assert errors == [] and store.calls == [("demo", 50)]
    assert len(items) == 1
    assert items[0].source == "episodes"
    assert items[0].has_result is False   # 只有 SQL → validator 档判不了
    assert "1 条" in summary


@pytest.mark.asyncio
async def test_episodes_read_failure_is_error_not_empty(tmp_path):
    """读不出的库 ≠ 空库:后者是「无」,前者必须红(否则试跑静默变绿)。"""
    store = _FakeEpisodes(exc=RuntimeError("db locked"))
    items, summary, errors = await load_episodes_corpus(
        "demo", home_dir=tmp_path, store=store)
    assert items == [] and len(errors) == 1 and "db locked" in errors[0]
    assert "读取失败" in summary


@pytest.mark.asyncio
async def test_episodes_missing_db_is_honest_none(tmp_path):
    items, summary, errors = await load_episodes_corpus("demo", home_dir=tmp_path)
    assert items == [] and errors == []
    assert "不存在" in summary


@pytest.mark.asyncio
async def test_episodes_owned_store_is_disposed(tmp_path, monkeypatch):
    """自建 store 用完即 dispose —— aiosqlite worker 不 dispose 进程挂住。"""
    db = tmp_path / "memory" / "episodes.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")   # 只要存在性,真实读取由假 store 承担
    made: list[_FakeEpisodes] = []

    class _Store(_FakeEpisodes):
        def __init__(self, path):
            super().__init__(rows=[{"question": "q", "sql": "SELECT 1"}])
            made.append(self)

    monkeypatch.setattr("trove.services.memory.episode.EpisodeStore", _Store)
    items, _, errors = await load_episodes_corpus("demo", home_dir=tmp_path)
    assert errors == [] and len(items) == 1
    assert made and made[0].disposed is True


@pytest.mark.asyncio
async def test_episodes_real_store_round_trip(tmp_path):
    """真 EpisodeStore:记一条 → 试跑语料读回(无结果行)。"""
    from trove.services.memory.episode import EpisodeStore
    from trove.services.memory.models import MemoryScope

    db = tmp_path / "memory" / "episodes.sqlite"
    db.parent.mkdir(parents=True)
    store = EpisodeStore(db)
    try:
        await store.record(
            MemoryScope(datasource="demo", user_id="u1"),
            question="How many loans?", sql="SELECT COUNT(*) FROM loan",
            dialect="sqlite", verdict="pass",
        )
    finally:
        await store.dispose()

    items, summary, errors = await load_episodes_corpus("demo", home_dir=tmp_path)
    assert errors == []
    assert [(i.question, i.sql) for i in items] == [
        ("How many loans?", "SELECT COUNT(*) FROM loan")]
    assert items[0].has_result is False and "1 条" in summary


# ── merge / gather ───────────────────────────────────────


def test_merge_dedupes_and_fixtures_win():
    fx = _result_item(question="How many Loans?", sql="SELECT 1")
    ep = _item(question="how many loans?", sql="SELECT 1")   # 同一问同一 SQL
    merged, dropped = merge_corpus([fx], [ep])
    assert dropped == 1
    assert merged == [fx] and merged[0].has_result is True


def test_merge_keeps_different_sql():
    fx = _result_item(question="q", sql="SELECT 1")
    ep = _item(question="q", sql="SELECT 2")
    merged, dropped = merge_corpus([fx], [ep])
    assert dropped == 0 and len(merged) == 2


@pytest.mark.asyncio
async def test_gather_no_datasource_is_error(tmp_path):
    corpus = await gather_corpus(
        datasource="", project_root=tmp_path, home_dir=tmp_path)
    assert corpus.items == []
    assert len(corpus.errors) == 1 and "点名数据源" in corpus.errors[0]


@pytest.mark.asyncio
async def test_gather_auto_missing_is_honest_not_error(tmp_path):
    corpus = await gather_corpus(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path)
    assert corpus.errors == []
    assert any(s.startswith("fixtures: 无") for s in corpus.sources)


@pytest.mark.asyncio
async def test_gather_explicit_missing_is_error(tmp_path):
    corpus = await gather_corpus(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(tmp_path / "ghost.yml"))
    assert corpus.errors and "ghost.yml" in corpus.errors[0]


@pytest.mark.asyncio
async def test_gather_limit_truncates_per_source(tmp_path):
    """上限按源生效:fixtures 被截,episodes 不被前者的条数挤掉。"""
    ds_dir = tmp_path / ".trove" / "kb" / "demo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "fixtures.yml").write_text(
        "items:\n"
        '  - question: q1\n    sql: SELECT 1\n'
        '  - question: q2\n    sql: SELECT 2\n'
        '  - question: q3\n    sql: SELECT 3\n', encoding="utf-8")
    store = _FakeEpisodes(rows=[{"question": "ep1", "sql": "SELECT 9"}])
    corpus = await gather_corpus(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        episodes=True, limit=2, episode_store=store)
    assert corpus.errors == []
    assert [i.question for i in corpus.items] == ["q1", "q2", "ep1"]
    assert any("2/3 条" in s for s in corpus.sources)
    assert store.calls == [("demo", 2)]     # episodes 的上限在查询层


@pytest.mark.asyncio
async def test_gather_episodes_disabled_by_default(tmp_path):
    ds_dir = tmp_path / ".trove" / "kb" / "demo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "fixtures.yml").write_text(_GOOD_YAML, encoding="utf-8")
    corpus = await gather_corpus(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path)
    assert any("未启用" in s for s in corpus.sources)
    assert len(corpus.items) == 1


@pytest.mark.asyncio
async def test_gather_merges_and_reports_dedupe(tmp_path):
    ds_dir = tmp_path / ".trove" / "kb" / "demo"
    ds_dir.mkdir(parents=True)
    (ds_dir / "fixtures.yml").write_text(
        'items:\n  - question: How many loans?\n    sql: "SELECT COUNT(*) AS loan_count FROM loan"\n'
        "    columns: [loan_count]\n    rows:\n      - [3]\n", encoding="utf-8")
    store = _FakeEpisodes(rows=[
        {"question": "how many loans?", "sql": "SELECT COUNT(*) AS loan_count FROM loan"}])
    corpus = await gather_corpus(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        episodes=True, episode_store=store)
    assert len(corpus.items) == 1 and corpus.deduped == 1
    assert corpus.items[0].has_result is True   # fixtures 版本胜出


# ── judge_validator(双态消融)─────────────────────────────


def test_validator_without_result_rows_is_skipped():
    j = judge_validator([], _item())
    assert j.state == "skipped" and j.reason == "no_result_rows"
    assert j.pre == "" and j.post == ""     # 没判过就没有「装前/装后」


def test_validator_ablation_must_catch_diff_is_one(tmp_path):
    """核心断言:插一条必然触发的资产 → 装前 pass、装后 violated,点名它。"""
    spec = {"name": "must-catch", "severity": "blocking",
            "checks": [{"expr": "min >= 100", "columns": ["loan_count"]}]}
    j = judge_validator([spec], _result_item())
    assert j.state == "covered"
    assert j.pre == "pass" and j.post == "violated"
    assert j.pre_blocked_by == ()
    assert j.post_blocked_by == ("must-catch",)
    assert j.post_flagged_by == ()


def test_validator_clean_asset_no_diff():
    spec = {"name": "nonneg", "severity": "blocking",
            "checks": [{"expr": "min >= 0", "columns": ["loan_count"]}]}
    j = judge_validator([spec], _result_item())
    assert j.pre == j.post == "pass"
    assert j.post_blocked_by == () and j.post_flagged_by == ()


def test_validator_advisory_violation_flags_but_not_blocks():
    spec = {"name": "soft", "severity": "advisory",
            "checks": [{"expr": "min >= 100", "columns": ["loan_count"]}]}
    j = judge_validator([spec], _result_item())
    assert j.post == "violated"
    assert j.post_blocked_by == ()
    assert j.post_flagged_by == ("soft",)


def test_validator_unjudged_is_not_pass():
    """判不了(None)不许冒充通过:mode: llm 本期不驱动 → unjudged。"""
    spec = {"name": "llm-tier", "severity": "blocking", "mode": "llm",
            "checks": [{"expr": "min >= 0", "columns": ["loan_count"]}]}
    j = judge_validator([spec], _result_item())
    assert j.post == "unjudged"
    assert j.post_blocked_by == () and j.post_flagged_by == ()
    assert j.post_hits[0]["reason"] == "unsupported_mode"


def test_validator_runner_crash_is_errored(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kernel down")

    monkeypatch.setattr(dr, "run_validators", _boom)
    j = judge_validator([{"name": "x"}], _result_item())
    assert j.state == "errored" and j.reason == "runner_error"


def test_validator_names_only_the_triggering_asset():
    specs = [
        {"name": "clean", "severity": "blocking",
         "checks": [{"expr": "min >= 0", "columns": ["loan_count"]}]},
        {"name": "catcher", "severity": "blocking",
         "checks": [{"expr": "max <= 1", "columns": ["loan_count"]}]},
    ]
    j = judge_validator(specs, _result_item())
    assert j.post_blocked_by == ("catcher",)
    assert len(j.post_hits) == 2


def test_validator_pre_pass_is_never_blocked():
    """装前那遍用空资产集 —— 它按定义不会产生任何拦截。"""
    j = judge_validator([], _result_item())
    assert j.state == "covered" and j.pre == "pass" and j.pre_blocked_by == ()


# ── judge_guard(SQL 域,同一条消融纪律)─────────────────────


def _guard_runner(payload=None, errors_on_post=False):
    calls: list[dict] = []

    def runner(specs, *, sql, dialect, lang):
        calls.append({"n": len(specs), "sql": sql, "dialect": dialect, "lang": lang})
        if not specs:
            return []
        if errors_on_post:
            raise RuntimeError("guard crash")
        return list(payload or [])

    return runner, calls


def test_guard_contract_and_ablation():
    runner, calls = _guard_runner(payload=[
        {"name": "no-cartesian", "verdict": False, "severity": "blocking",
         "message": "违反"}])
    j = judge_guard(runner, [{"name": "no-cartesian"}], _item())
    assert j.state == "covered" and j.post == "violated"
    assert j.post_blocked_by == ("no-cartesian",) and j.pre == "pass"
    assert calls[0]["n"] == 0 and calls[1]["n"] == 1        # 装前空集 / 装后带资产
    assert calls[0]["sql"] == "SELECT 1" and calls[0]["dialect"] == "sqlite"
    assert calls[0]["lang"] == "zh"


def test_guard_without_sql_is_skipped():
    j = judge_guard(lambda *a, **k: [], [{"name": "x"}], _item(sql=""))
    assert j.state == "skipped" and j.reason == "no_sql"


def test_guard_hit_triggered_alias_is_read():
    """E2 的守卫若用 triggered 键表达判定,同样读得懂(只认布尔)。"""
    runner, _ = _guard_runner(payload=[
        {"name": "g", "triggered": False, "severity": "advisory"}])
    j = judge_guard(runner, [{"name": "g"}], _item())
    assert j.post == "violated" and j.post_flagged_by == ("g",)


def test_guard_non_bool_verdict_is_unjudged():
    runner, _ = _guard_runner(payload=[{"name": "g", "verdict": "yes"}])
    j = judge_guard(runner, [{"name": "g"}], _item())
    assert j.post == "unjudged"


def test_guard_runner_crash_is_errored():
    runner, _ = _guard_runner(errors_on_post=True)
    j = judge_guard(runner, [{"name": "g"}], _item())
    assert j.state == "errored" and j.reason == "runner_error"


# ── load_guard_tier(E2 接缝)──────────────────────────────


def test_guard_probe_gives_decision_not_exception(tmp_path):
    """真实探测:本树若 E2 未合流 → 整档 unavailable(理由在闭集内);
    合流后 → available + runner。这条不假设分支,只要求「给出结论」。"""
    tier = load_guard_tier(_svc(tmp_path))
    assert tier.available is False or callable(tier.runner)
    if not tier.available:
        assert tier.reason in SKIP_REASONS


def test_guard_absent_module(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "_probe_guards_module",
                        lambda: (None, "guards_module_absent", ""))
    tier = load_guard_tier(_svc(tmp_path))
    assert tier.available is False and tier.reason == "guards_module_absent"
    assert tier.runner is None


def test_guard_import_failure_is_distinguished(tmp_path, monkeypatch):
    """模块在但导入炸 = 坏合流,不是「还没到」—— 理由码必须区分开。"""
    monkeypatch.setattr(dr, "_probe_guards_module",
                        lambda: (None, "guards_module_import_failed", "boom"))
    tier = load_guard_tier(_svc(tmp_path))
    assert tier.available is False
    assert tier.reason == "guards_module_import_failed" and tier.detail == "boom"


def test_guard_module_without_run_guards(tmp_path, monkeypatch):
    class _Half:
        pass

    monkeypatch.setattr(dr, "_probe_guards_module", lambda: (_Half(), "", ""))
    tier = load_guard_tier(_svc(tmp_path))
    assert tier.available is False and tier.reason == "guard_runner_missing"


def test_guard_injected_runner_wins(tmp_path, monkeypatch):
    def _should_not_probe():
        raise AssertionError("注入了 runner 就不该探测模块")

    monkeypatch.setattr(dr, "_probe_guards_module", _should_not_probe)
    runner, _ = _guard_runner()
    tier = load_guard_tier(_svc(tmp_path), runner=runner, specs=[{"name": "g"}])
    assert tier.available is True and tier.runner is runner
    assert tier.specs == [{"name": "g"}]


def test_guard_default_specs_only_confirmed_guard_tier(tmp_path, monkeypatch):
    """资产集合 = 已确认的 tier:guard org 资产(草稿永不进试跑)。"""
    monkeypatch.setattr(dr, "_probe_guards_module", lambda: (None, "", ""))
    svc = _svc(tmp_path)
    entries = [
        {"name": "g1", "tier": "guard", "status": "confirmed"},
        {"name": "g2", "tier": "guard", "status": "pending"},
        {"name": "v1", "tier": "validator", "status": "confirmed"},
    ]
    monkeypatch.setattr(
        svc, "list_org", lambda confirmed_only=False: [
            e for e in entries if not confirmed_only or e["status"] == "confirmed"])
    runner, _ = _guard_runner()

    class _Mod:
        run_guards = staticmethod(runner)

    monkeypatch.setattr(dr, "_probe_guards_module", lambda: (_Mod(), "", ""))
    tier = load_guard_tier(svc)
    assert tier.available is True
    assert [s["name"] for s in tier.specs] == ["g1"]


# ── validator_specs_for ──────────────────────────────────


def test_validator_specs_from_confirmed_asset(tmp_path):
    svc = _svc(tmp_path)
    _make_validator(svc, name="credit-guard")
    specs, not_selected = validator_specs_for(svc, "demo")
    assert [s["name"] for s in specs] == ["credit-guard"]
    assert not_selected == []


def test_pending_validator_never_selected(tmp_path):
    """确认门:草稿不进试跑 —— 「还没装」与「装坏了」必须分得开。"""
    svc = _svc(tmp_path)
    _make_validator(svc, name="drafty", confirm=False)
    specs, not_selected = validator_specs_for(svc, "demo")
    assert specs == [] and not_selected == []


def test_disabled_validator_never_selected(tmp_path):
    """E6 颗粒停用:停用资产不进试跑候选集(与消费面同一份清单)。"""
    svc = _svc(tmp_path)
    _make_validator(svc, name="stopped")
    svc.disable("stopped")
    specs, not_selected = validator_specs_for(svc, "demo")
    assert specs == [] and not_selected == []


def test_disabled_guard_never_selected(tmp_path):
    """guard 档同理(同一条投递路):停用后默认 spec 集合为空。"""
    svc = _svc(tmp_path)
    svc.create({
        "name": "hushed", "description": "d", "tier": "guard",
        "guard": {"targets": ["sql"], "checks": [{
            "name": "c", "severity": "advisory", "expr": "select_star == 0",
            "reason": "r"}]},
        "body": "b",
    })
    svc.confirm("hushed")
    assert [s["name"] for s in load_guard_tier(svc).specs] == ["hushed"]

    svc.disable("hushed")
    assert load_guard_tier(svc).specs == []


def test_trigger_narrowing_is_reported(tmp_path):
    svc = _svc(tmp_path)
    _make_validator(svc, name="elsewhere", triggers={"datasource": "other"})
    specs, not_selected = validator_specs_for(svc, "demo")
    assert specs == []
    assert not_selected == ["elsewhere"]


@pytest.mark.asyncio
async def test_master_switch_off_yields_empty_specs(tmp_path):
    from types import SimpleNamespace

    config = SimpleNamespace(
        extensions=SimpleNamespace(org_extensions_enabled=False))
    svc = _svc(tmp_path, config=config)
    _make_validator(svc, name="gated")
    specs, _ = validator_specs_for(svc, "demo")
    assert specs == []
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), skills=svc)
    assert any("候选 validator 资产为空" in n for n in report.notes)


# ── 退出码三分支 ─────────────────────────────────────────


def test_exit_2_on_errors():
    r = DryRunReport(errors=["语料缺失"])
    r.rows = [_row()]
    assert r.exit_code == 2


def test_exit_2_on_no_rows():
    assert DryRunReport().exit_code == 2


def test_exit_2_when_everything_skipped():
    """全跳过 = 没验,_不许_和验过且干净(0)混为一谈。"""
    r = DryRunReport()
    r.rows = [_row(state="skipped"), _row(state="skipped", tier="guard")]
    assert r.covered == 0 and r.exit_code == 2


def test_exit_2_on_errored():
    r = DryRunReport()
    r.rows = [_row(), _row(state="errored")]
    assert r.exit_code == 2


def test_exit_1_on_newly_blocked():
    r = DryRunReport()
    r.rows = [_row(post_blocked=("must-catch",), pre="pass", post="violated")]
    assert r.newly_blocked and r.exit_code == 1


def test_exit_1_on_assertion_mismatch():
    r = DryRunReport()
    r.rows = [_row(assertion="mismatch")]
    assert r.exit_code == 1


def test_exit_0_clean():
    r = DryRunReport()
    r.rows = [_row(), _row(tier="guard")]
    assert r.exit_code == 0


def test_counts_corpus_vs_judgments():
    r = DryRunReport()
    r.rows = [_row(index=0), _row(index=0, tier="guard"),
              _row(index=1, state="skipped")]
    assert r.corpus_n == 2 and len(r.rows) == 3
    d = r.to_dict()
    assert d["counts"] == {"covered": 2, "skipped": 1, "errored": 0,
                           "judgments": 3, "corpus": 2}


def test_metrics_shape_and_values():
    r = DryRunReport()
    r.rows = [
        _row(index=0, post_blocked=("a",), pre="pass", post="violated"),
        _row(index=1),
        _row(index=2, state="skipped", tier="guard"),
        _row(index=3, assertion="mismatch"),
    ]
    m = r.metrics
    assert m["coverage"] == 0.75
    assert m["blocking_fail_rate"] == 0.3333
    assert m["assert_fail_rate"] == 1.0
    assert m["n"] == 4.0 and m["n_judged"] == 3.0


# ── R5 隐私 / 渲染 / eval_gate 消费面 ─────────────────────


@pytest.mark.asyncio
async def test_report_masks_question_text_by_default(tmp_path):
    text = "Which district has the highest average loan amount?"
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML.replace("How many loans?", text))))
    dumped = json.dumps(report.to_dict(), ensure_ascii=False)
    rendered = report.render()
    assert text not in dumped and "How many loans?" not in dumped
    assert text not in rendered
    assert all("question" not in item for item in report.to_dict()["items"])
    # 短码在:判定的可引用身份没有丢
    qid = hashlib.sha256(text.encode()).hexdigest()[:8]
    assert qid in rendered and qid in dumped


@pytest.mark.asyncio
async def test_include_questions_shows_text(tmp_path):
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), include_questions=True)
    assert "How many loans?" in report.render()
    assert any(i.get("question") == "How many loans?"
               for i in report.to_dict()["items"])


@pytest.mark.asyncio
async def test_render_reports_counts_and_assets(tmp_path):
    svc = _svc(tmp_path)
    _make_validator(svc, name="must-catch", expr="min >= 100",
                    columns=("loan_count",))
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), skills=svc)
    out = report.render()
    assert "covered" in out and "skipped" in out and "errored" in out
    assert "must-catch" in out and "装前" in out and "装后" in out
    assert "退出码 1" in out


@pytest.mark.asyncio
async def test_metrics_feed_eval_gate_score_from_file(tmp_path):
    """报告 --json 的 metrics 段直接喂 eval_gate(scorecard 消费面)。"""
    from trove.eval.gate import compare_metrics, score_from_file

    svc = _svc(tmp_path)
    _make_validator(svc, name="must-catch")
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), skills=svc)
    score = tmp_path / "scorecard.json"
    score.write_text(json.dumps(report.to_dict(), ensure_ascii=False),
                     encoding="utf-8")

    metrics = score_from_file(score)
    assert metrics["coverage"] == report.metrics["coverage"]
    assert set(metrics) == {"coverage", "blocking_fail_rate",
                            "assert_fail_rate", "n", "n_judged"}

    # 同分自比:无回归;覆盖率掉了 / 拦截率涨了 = 门必须红
    same = compare_metrics(metrics, dict(metrics))
    assert same.passed
    worse = compare_metrics(
        metrics,
        {**metrics, "coverage": metrics["coverage"] - 0.25,
         "blocking_fail_rate": metrics["blocking_fail_rate"] + 0.25})
    assert {m.metric for m in worse.regressions} == {
        "coverage", "blocking_fail_rate"}


# ── run_dryrun 端到端 ────────────────────────────────────


@pytest.mark.asyncio
async def test_run_end_to_end_clean_exit_0(tmp_path):
    svc = _svc(tmp_path)
    _make_validator(svc, name="nonneg", expr="min >= 0", columns=("loan_count",))
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), skills=svc)
    assert report.errors == []
    assert report.covered == 2                       # 两档都判过(guard 走真实 run_guards)
    assert report.skipped == 0
    assert report.exit_code == 0
    assert report.tiers["validator"]["specs"] == ["nonneg"]
    assert report.tiers["guard"]["available"] is True


@pytest.mark.asyncio
async def test_run_broken_install_exit_1_names_asset(tmp_path):
    """验收口径:装坏守卫 → exit 1 且点名。fixtures 无 verdict 断言,
    红的来源只能是消融差本身。"""
    svc = _svc(tmp_path)
    _make_validator(svc, name="too-strict", expr="min >= 100",
                    columns=("loan_count",))
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), skills=svc)
    assert report.exit_code == 1
    blocked = report.newly_blocked
    assert len(blocked) == 1
    assert blocked[0].post_blocked_by == ("too-strict",)
    assert blocked[0].tier == "validator"
    assert "too-strict" in report.render()


@pytest.mark.asyncio
async def test_run_assertion_mismatch_exit_1(tmp_path):
    """fixture 声明 pass,但装上资产后判定是 violated → 断言失败退出 1。"""
    svc = _svc(tmp_path)
    _make_validator(svc, name="too-strict", expr="min >= 100",
                    columns=("loan_count",))
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), skills=svc)
    val = [r for r in report.rows if r.tier == "validator"][0]
    assert val.expected == "pass" and val.assertion == "mismatch"
    assert report.mismatches and report.exit_code == 1


@pytest.mark.asyncio
async def test_run_missing_explicit_fixtures_exit_2(tmp_path):
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(tmp_path / "ghost.yml"))
    assert report.errors and report.exit_code == 2


@pytest.mark.asyncio
async def test_run_no_datasource_exit_2(tmp_path):
    report = await run_dryrun(
        datasource="", project_root=tmp_path, home_dir=tmp_path)
    assert report.exit_code == 2
    assert any("点名数据源" in e for e in report.errors)


@pytest.mark.asyncio
async def test_run_episodes_only_all_skipped_exit_2(tmp_path, monkeypatch):
    """episodes 只有 SQL,且树里没有 guards 模块(降级树):两档都判不了
    —— 一条没真跑过 → 2,不是 0(诚实边界的关键用例)。

    E2 合流后 guard 档能真判 episodes 的 SQL,「全跳过」场景只在降级树
    出现,故这里把探测钉到缺席态(本树的真实形状见下条)。"""
    monkeypatch.setattr(dr, "_probe_guards_module",
                        lambda: (None, "guards_module_absent", ""))
    store = _FakeEpisodes(rows=[{"question": "How many loans?",
                                 "sql": "SELECT 1"}])
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        episodes=True, episode_store=store)
    assert report.covered == 0 and report.skipped == 2
    assert report.exit_code == 2
    reasons = {r.reason for r in report.rows if r.state == "skipped"}
    assert reasons == {"no_result_rows", "guards_module_absent"}


@pytest.mark.asyncio
async def test_run_episodes_judged_by_real_guard_tier(tmp_path):
    """E2 接通后的真实形状:episodes 的 SQL 被真实 ``run_guards`` 判过 ——
    validator 档仍如实 skipped(无结果行),guard 档 covered(候选守卫集
    为空 → 无触发)。这条钉住接缝翻转:本树里 SQL-only 语料对 guard 档是
    **真判过**,全跳过只在降级树出现。"""
    store = _FakeEpisodes(rows=[{"question": "How many loans?",
                                 "sql": "SELECT 1"}])
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        episodes=True, episode_store=store)
    assert report.tiers["guard"]["available"] is True
    assert report.covered == 1 and report.skipped == 1
    assert report.exit_code == 0


@pytest.mark.asyncio
async def test_run_empty_corpus_exit_2_not_0(tmp_path):
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path)  # 无 fixtures
    assert report.rows == [] and report.exit_code == 2


@pytest.mark.asyncio
async def test_run_guard_available_via_injection(tmp_path):
    """guard 档显式注入的形状(与真实探测同一条装载路径):两档都 covered。"""
    runner, calls = _guard_runner(payload=[
        {"name": "no-cartesian", "verdict": True, "severity": "advisory"}])
    svc = _svc(tmp_path)
    _make_validator(svc, name="nonneg", expr="min >= 0", columns=("loan_count",))
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), skills=svc,
        guard_runner=runner, guard_specs=[{"name": "no-cartesian"}])
    assert report.exit_code == 0
    assert report.covered == 2 and report.skipped == 0
    guard_row = [r for r in report.rows if r.tier == "guard"][0]
    assert guard_row.pre == "pass" and guard_row.post == "pass"
    assert calls == [
        {"n": 0, "sql": "SELECT COUNT(*) AS loan_count FROM loan",
         "dialect": "sqlite", "lang": "zh"},
        {"n": 1, "sql": "SELECT COUNT(*) AS loan_count FROM loan",
         "dialect": "sqlite", "lang": "zh"},
    ]


@pytest.mark.asyncio
async def test_run_guard_advisory_violation_reported_not_blocking(tmp_path):
    """guard 的 advisory 违反:报告点名,但不拦、不动退出码(fixtures 不断言)。"""
    runner, _ = _guard_runner(payload=[
        {"name": "cartesian", "verdict": False, "severity": "advisory"}])
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, "items:\n  - question: q\n    sql: SELECT 1\n")),
        guard_runner=runner, guard_specs=[{"name": "cartesian"}])
    # 无结果行 → validator 档 skipped;guard 档判过 → 全运行结论仍是 0
    assert report.covered == 1 and report.skipped == 1
    assert report.exit_code == 0
    guard_row = [r for r in report.rows if r.tier == "guard"][0]
    assert guard_row.post == "violated" and guard_row.post_flagged_by == ("cartesian",)
    assert guard_row.newly_blocked is False


@pytest.mark.asyncio
async def test_run_guard_import_failure_exit_2(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "_probe_guards_module",
                        lambda: (None, "guards_module_import_failed", "kaboom"))
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)))
    assert report.exit_code == 2
    assert any("kaboom" in e for e in report.errors)


@pytest.mark.asyncio
async def test_run_json_contains_tiers_and_notes(tmp_path):
    svc = _svc(tmp_path)
    _make_validator(svc, name="elsewhere", triggers={"datasource": "other"})
    report = await run_dryrun(
        datasource="demo", project_root=tmp_path, home_dir=tmp_path,
        fixtures=str(_write(tmp_path, _GOOD_YAML)), skills=svc)
    d = report.to_dict()
    assert d["kind"] == "dryrun" and d["datasource"] == "demo"
    assert set(d["tiers"]) == {"validator", "guard"}
    assert any("未入选" in n for n in d["notes"])
    assert json.dumps(d, ensure_ascii=False)     # 可序列化
