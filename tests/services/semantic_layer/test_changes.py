"""变更服务 —— 记录层/open/reject + （Task 6 起）merge 与并发。

零 LLM、零网络：KbService 指向 tmp 目录；需要 git 的用例自建本地 repos
（与 tests/services/kb/test_git_versioning.py 同款）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from trove.core.config import ChangesConfig
from trove.services.kb.service import KbService
# ChangeStale 由 Task 6 的 merge 用例引入;本任务不用就不 import
# (ruff 的 F401 是 CI 硬门,空 import 留在这里只会让本任务的验证变红)。
from trove.services.semantic_layer.changes import (
    ChangeError, ChangeNotFound, ChangeService,
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
