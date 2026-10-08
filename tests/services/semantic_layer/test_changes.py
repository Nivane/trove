"""变更服务 —— 记录层/open/reject + （Task 6 起）merge 与并发。

零 LLM、零网络：KbService 指向 tmp 目录；需要 git 的用例自建本地 repos
（与 tests/services/kb/test_git_versioning.py 同款）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from trove.core.config import ChangesConfig
from trove.services.kb.service import KbService
from trove.services.semantic_layer.changes import (
    ChangeError, ChangeNotFound, ChangeService, ChangeStale,
)
from tests.helpers.kb import ossie_semantics_yaml

DS = "demo"


def _git(repo: Path, *args: str):
    import os
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=30,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


@pytest.fixture
def kb(tmp_path: Path) -> KbService:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@local")
    kb = KbService(repo, git_kb=True)
    ds_dir = kb.kb_dir / DS
    ds_dir.mkdir(parents=True, exist_ok=True)
    kb.semantics_path(DS).write_text(
        ossie_semantics_yaml([{"term": "total_loan", "mapping": "SUM(loan.amount)",
                               "tables": ["loan"], "definition": "贷款总额"}]),
        encoding="utf-8")
    return kb


def _svc(kb: KbService, **kw) -> ChangeService:
    return ChangeService(kb, **kw)


PAYLOAD = {"kind": "metric", "action": "upsert", "name": "refund_rate",
           "payload": {"expression": "SUM(loan.refund) / SUM(loan.amount)",
                       "description": "退款率"}}


async def test_open_writes_staging_and_record_without_touching_mainline(kb: KbService):
    before = kb.semantics_path(DS).read_bytes()
    rec = await _svc(kb).open(DS, origin="manual", payloads=[PAYLOAD], note="新增退款率")
    assert rec["status"] == "open"
    assert rec["origin"] == "manual"
    assert rec["subjects"] == [{"kind": "metric", "name": "refund_rate"}]
    assert rec["base_digest"].startswith("sha256:")
    assert kb.semantics_path(DS).read_bytes() == before  # I2
    staged = Path(kb.kb_dir) / DS / ".staging" / rec["id"]
    assert (staged / "base.semantics.yml").read_text(encoding="utf-8") == before.decode()
    after_text = (staged / "after.semantics.yml").read_text(encoding="utf-8")
    assert "refund_rate" in after_text


async def test_open_auto_verify_by_origin(kb: KbService):
    """sandbox_by_origin 含 manual 时 open 后 verification.json 落盘（配置消费侧）。"""
    svc = _svc(kb, config=ChangesConfig(sandbox_by_origin=["manual"]))
    rec = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    v = svc.detail(DS, rec["id"]).get("verification")
    assert v is not None
    assert v["verdict"] in ("neutral", "not_applicable", "improves")


async def test_list_filters_by_status(kb: KbService):
    svc = _svc(kb)
    a = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    await svc.reject(DS, a["id"], by="admin", reason="口径不对")
    b = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    assert [r.id for r in svc.list(DS, status="open")] == [b["id"]]
    assert [r.id for r in svc.list(DS, status="rejected")] == [a["id"]]
    assert len(svc.list(DS)) == 2


async def test_reject_requires_reason_and_marks_record(kb: KbService):
    svc = _svc(kb)
    rec = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    out = await svc.reject(DS, rec["id"], by="admin", reason="口径含冲正")
    assert out["status"] == "rejected"
    assert out["reject_reason"] == "口径含冲正"
    assert out["resolved_by"] == "admin"


async def test_get_unknown_raises_not_found(kb: KbService):
    with pytest.raises(ChangeNotFound):
        _svc(kb).get(DS, "chg-nope")


async def test_drift_gate_warns_without_blocking(kb: KbService):
    """开着漂移主体 → open 不阻断;merge 响应带 warnings（merge 在 Task 6 断言）。"""
    svc = _svc(kb)
    rec = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    assert rec["status"] == "open"  # 漂移面有数据/没数据都不拦 open


async def test_open_rejects_non_dict_payload_entries(kb: KbService):
    """非 dict 的条目/载荷在开单时就被挡下 —— 不是留到 merge/HTTP 层炸 500。"""
    svc = _svc(kb)
    with pytest.raises(ChangeError):
        await svc.open(DS, origin="manual", payloads=["不是对象"])
    with pytest.raises(ChangeError):
        await svc.open(DS, origin="manual",
                       payloads=[{"kind": "metric", "action": "upsert",
                                  "name": "x", "payload": "不是对象"}])


async def test_detail_marks_missing_snapshot_as_degraded(kb: KbService):
    """快照半损时 diff 算不了 —— 如实进 degraded,不静默成「还没算」。"""
    svc = _svc(kb)
    rec = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    (svc._staging.root(rec["id"]) / "after.semantics.yml").unlink()
    out = svc.detail(DS, rec["id"])
    assert out["diff"] is None
    assert "snapshot_missing" in out["degraded"]


async def test_drift_unavailable_is_degraded_not_empty(kb: KbService):
    """I8：存储读不到 → degraded 如实,绝不洗成「无漂移」。"""
    class _Boom:
        async def open_subjects(self, ds):
            raise RuntimeError("store offline")

    svc = _svc(kb, drift_store=_Boom())
    warnings, degraded = await svc.drift_warnings(DS, [{"kind": "metric", "name": "refund_rate"}])
    assert warnings == []
    assert degraded == ["drift_unavailable"]


async def test_drift_hit_produces_warning(kb: KbService):
    class _Hit:
        async def open_subjects(self, ds):
            return {"loan.amount"}

    svc = _svc(kb, drift_store=_Hit())
    warnings, degraded = await svc.drift_warnings(DS, [{"kind": "field", "name": "loan.amount"}])
    assert degraded == []
    assert warnings == [{"code": "open_drift", "subjects": ["loan.amount"]}]


# ── merge / 并发 / 包装 ────────────────────────────────


async def test_merge_applies_and_flips_record_single_commit(kb: KbService):
    svc = _svc(kb)
    rec = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    before_count = int(_git(Path(kb.kb_dir).parent, "rev-list", "--count", "HEAD").stdout)
    out = await svc.merge(DS, rec["id"], by="admin")
    assert out["status"] == "merged"
    text = kb.semantics_path(DS).read_text(encoding="utf-8")
    assert "refund_rate" in text
    after_count = int(_git(Path(kb.kb_dir).parent, "rev-list", "--count", "HEAD").stdout)
    assert after_count == before_count + 1
    log = _git(Path(kb.kb_dir).parent, "log", "-1", "--format=%s%n%(trailers:unfold)").stdout
    assert "semantic: merge change" in log


async def test_merge_stale_refuses_and_writes_nothing(kb: KbService):
    """并发窗口：两份变更同基线,先到者写盘,后者 409 且三文件全不动。"""
    svc = _svc(kb)
    a = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    b = await svc.open(DS, origin="manual",
                       payloads=[{"kind": "metric", "action": "upsert", "name": "avg2",
                                  "payload": {"expression": "AVG(loan.amount)"}}])
    await svc.merge(DS, a["id"], by="admin")
    sem_before = kb.semantics_path(DS).read_bytes()
    chg_before = svc.changes_path(DS).read_bytes()
    with pytest.raises(ChangeStale):
        await svc.merge(DS, b["id"], by="admin")
    assert kb.semantics_path(DS).read_bytes() == sem_before
    assert svc.changes_path(DS).read_bytes() == chg_before
    assert svc.get(DS, b["id"]).status == "open"


async def test_merge_first_creation_path(kb: KbService, tmp_path: Path):
    """Review Focus 1：semantics.yml 不存在时 merge 成功且补 version。"""
    repo2 = tmp_path / "repo2"
    repo2.mkdir()
    _git(repo2, "init", "-q")
    _git(repo2, "config", "user.name", "Tester")
    _git(repo2, "config", "user.email", "tester@local")
    kb2 = KbService(repo2, git_kb=True)
    (kb2.kb_dir / DS).mkdir(parents=True)
    svc = _svc(kb2)
    rec = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    out = await svc.merge(DS, rec["id"], by="admin")
    assert out["status"] == "merged"
    doc = yaml.safe_load(kb2.semantics_path(DS).read_text(encoding="utf-8"))
    assert doc["version"] == "0.2.0.dev0"
    assert doc["semantic_model"][0]["metrics"][0]["name"] == "refund_rate"


async def test_merge_invalid_leaves_mainline_and_staging(kb: KbService):
    """I5：写前门禁不过 → 主线不动、记录仍 open、快照保留（可重开）。"""
    svc = _svc(kb)
    bad = {"kind": "metric", "action": "upsert", "name": "broken",
           "payload": {"expression": "SELEC broken("}}
    # 开单干跑就应拦下坏表达式
    with pytest.raises(ChangeError):
        await svc.open(DS, origin="manual", payloads=[bad])
    rec = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    before = kb.semantics_path(DS).read_bytes()
    staged = svc._staging.root(rec["id"])
    snapshot_before = (staged / "after.semantics.yml").read_bytes()
    stored = svc.get(DS, rec["id"])   # get() 返回 ChangeRecord；open() 返回 dict
    stored.payloads = [bad]           # 模拟记录被外部改坏（门禁必须兜住）
    svc._save(DS, [stored])
    from trove.services.semantic_layer.changes import ChangeInvalid
    with pytest.raises(ChangeInvalid):
        await svc.merge(DS, rec["id"], by="admin")
    assert kb.semantics_path(DS).read_bytes() == before
    assert svc.get(DS, rec["id"]).status == "open"
    # 名字承诺的另一半：失败不吞快照 —— 合并全程不碰隔离区（可重开、可复验）
    assert (staged / "after.semantics.yml").read_bytes() == snapshot_before
    assert (staged / "base.semantics.yml").read_bytes() == before


async def test_merge_missing_snapshot_is_loud(kb: KbService):
    """Review Focus 2：快照半损 → verify 明确报错（不 500、不静默当空快照）。

    merge 本身不读快照（它是 payload 重放 + digest 双检），所以快照缺失只
    影响 verify/detail —— 这正是把快照定位成「验证与审计的锚」而非「合并
    的唯一真相」的原因。
    """
    svc = _svc(kb)
    rec = await svc.open(DS, origin="manual", payloads=[PAYLOAD])
    (svc._staging.root(rec["id"]) / "after.semantics.yml").unlink()
    with pytest.raises(ChangeError):
        svc.verify(DS, rec["id"])
    assert svc.detail(DS, rec["id"])["diff"] is None  # 详情同样如实缺席,不伪造
    assert "snapshot_missing" in svc.detail(DS, rec["id"])["degraded"]


async def test_wrapper_commit_counts_unchanged(kb: KbService):
    """A9：create+confirm = 2 提交;auto_apply = 1 提交。"""
    from trove.services.semantic_layer.manage import SemanticManager

    mgr = SemanticManager(kb)
    repo = Path(kb.kb_dir).parent
    n0 = int(_git(repo, "rev-list", "--count", "HEAD").stdout or 0)
    draft = await mgr.create_draft(DS, "metric", "upsert", "avg_loan",
                                   {"expression": "AVG(loan.amount)"}, "平均")
    n1 = int(_git(repo, "rev-list", "--count", "HEAD").stdout)
    assert n1 == n0 + 1
    out = await mgr.confirm_draft(DS, draft["id"], dialect="sqlite", actor="admin")
    n2 = int(_git(repo, "rev-list", "--count", "HEAD").stdout)
    assert n2 == n1 + 1                      # create + confirm = 2
    assert out["status"] == "applied" and out["change_id"].startswith("chg-")
    n3 = n2
    # field 载荷必带 expression（`_apply_field` 的硬校验:字段表达式必填）——
    # brief 里的 None 在这里不是「零提交」而是直接报错,故按 A 档真实形状给载荷。
    entry = await mgr.auto_apply(DS, "field", "loan.note", {"expression": "note"})
    n4 = int(_git(repo, "rev-list", "--count", "HEAD").stdout or 0)
    assert entry["status"] == "applied"
    assert n4 == n3 + 1                      # auto_apply 恰好 1 提交
    # （`>=` 会漏掉 A9 真正要防的方向：一次 merge 落两次 git_commit 也通过）
    log = _git(repo, "log", "-1", "--format=%(trailers:unfold)").stdout
    assert "Auto-approved-by: deterministic-gate" in log


async def test_conflict_draft_cannot_confirm_nor_merge(kb: KbService):
    """A11：冲突草稿确认被拒（守卫不搬家：仍在合并入口）。"""
    from trove.services.semantic_layer.manage import SemanticManager

    mgr = SemanticManager(kb)
    draft = await mgr.create_draft(DS, "metric", "upsert", "dup",
                                   {"expression": "AVG(loan.amount)"},
                                   conflict={"code": "name_taken", "message": "重名"})
    with pytest.raises(ValueError):
        await mgr.confirm_draft(DS, draft["id"], dialect="sqlite", actor="admin")


async def test_merge_records_frozen_diff_and_auto_trailer(kb: KbService):
    from trove.services.semantic_layer.manage import SemanticManager

    mgr = SemanticManager(kb)
    # auto 记录不带 draft_id（`merge_auto` 合成 ChangeRecord 时就没有这个字段）,
    # 选择器只看 origin —— 原先的 `r.draft_id == entry["id"]` 是死分支。
    await mgr.auto_apply(DS, "metric", "auto_metric",
                         {"expression": "SUM(loan.amount)"})
    svc = _svc(kb)
    rec = [r for r in svc.list(DS) if r.origin == "auto_apply"][-1]
    assert rec.status == "merged" and rec.auto is True
    assert rec.diff is not None and "metrics" in rec.diff["entities"]
    assert rec.diff["entities"]["metrics"]["added"] == ["auto_metric"]
