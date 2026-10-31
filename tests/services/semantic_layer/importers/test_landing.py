"""导入落点:preview 零写盘 / bootstrap 走 init_semantics / stage 只开审批单。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from trove.services.kb.service import KbService
from trove.services.semantic_layer.changes import ChangeService
from trove.services.semantic_layer.importers import (
    SemanticImportError,
    run_semantic_import,
)
from trove.services.semantic_layer.importers.dbt import parse_dbt
from trove.services.semantic_layer.importers.landing import (
    build_payloads,
    load_document,
)
from trove.services.semantic_layer.ossie import parse_ossie

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "semantic_import"
PROJECT = FIXTURES / "dbt_project"
MANIFEST = PROJECT / "target" / "manifest.json"
CUBE = FIXTURES / "cube_meta.json"
DS = "demo"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _init_repo(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Tester")
    _git(root, "config", "user.email", "tester@local")
    return root


def _boot(root: Path, *, force: bool = False) -> dict:
    return run_semantic_import(
        root, source="dbt", path=PROJECT, datasource=DS,
        mode="bootstrap", manifest=MANIFEST, force=force,
    )


# ── payload / 文档形状(纯) ─────────────────────────────────


def test_build_payloads_uses_apply_draft_payload_keys():
    payloads = build_payloads(parse_dbt(PROJECT, manifest_path=MANIFEST))

    assert [p["kind"] for p in payloads] == (
        ["dataset"] * 2 + ["field"] * 3 + ["metric"] * 4 + ["relationship"])

    orders = next(p for p in payloads if p["kind"] == "dataset" and p["name"] == "orders")
    assert orders["payload"] == {
        "source": "analytics.orders",
        "primary_key": ["order_id"],
        "description": "One row per order",
    }

    field = next(
        p for p in payloads if p["kind"] == "field" and p["name"] == "orders.order_date")
    assert field["payload"] == {"expression": "ordered_at", "is_time": True}

    revenue = next(p for p in payloads if p["kind"] == "metric" and p["name"] == "revenue")
    assert revenue["payload"] == {
        "expression": "SUM(orders.amount)",
        "datasets": ["orders"],
        "definition": "Total revenue",
    }

    rel = next(p for p in payloads if p["kind"] == "relationship")
    assert rel["payload"] == {
        "from": "orders", "to": "customers",
        "from_columns": ["customer_id"], "to_columns": ["customer_id"],
        "cardinality": "1:N",
    }


# ── preview ────────────────────────────────────────────────


async def test_preview_is_zero_write(tmp_path):
    report = await run_semantic_import(
        tmp_path, source="dbt", path=PROJECT, datasource=DS, manifest=MANIFEST)

    assert report["mode"] == "preview"
    assert report["suggestion"] == "bootstrap"
    assert report["blocking"] == []
    assert report["landed"] is None
    assert report["counts"] == {
        "datasets": 2, "fields": 3, "metrics": 4, "relationships": 1,
    }
    assert not (tmp_path / ".trove").exists()


# ── bootstrap ──────────────────────────────────────────────


async def test_bootstrap_writes_through_init_semantics(tmp_path):
    report = await _boot(tmp_path)

    path = tmp_path / ".trove" / "kb" / DS / "semantics.yml"
    assert path.exists()
    parsed = parse_ossie(path.read_text(encoding="utf-8"), preferred_dialect="mysql")
    assert (len(parsed.datasets), len(parsed.metrics), len(parsed.relationships)) == (
        2, 4, 1)
    assert sum(len(d.fields) for d in parsed.datasets) == 3

    landed = report["landed"]
    assert landed["mode"] == "bootstrap"
    assert landed["written"] is True
    assert landed["overwrote"] is False
    assert landed["git"]["committed"] is False  # tmp 目录不是 git 仓库


async def test_bootstrap_commits_with_generator_trailer(tmp_path):
    repo = _init_repo(tmp_path)
    await _boot(repo)

    message = _git(repo, "log", "-1", "--format=%B").stdout
    assert "semantic: import dbt" in message
    assert "Generator: semantic.import" in message
    assert "Source: dbt" in message


async def test_bootstrap_refuses_existing_model_without_force(tmp_path):
    await _boot(tmp_path)
    before = (tmp_path / ".trove/kb" / DS / "semantics.yml").read_bytes()

    with pytest.raises(SemanticImportError, match="已有非空语义模型"):
        await _boot(tmp_path)

    assert (tmp_path / ".trove/kb" / DS / "semantics.yml").read_bytes() == before


async def test_bootstrap_force_overwrites(tmp_path):
    await _boot(tmp_path)
    report = await _boot(tmp_path, force=True)

    assert report["landed"]["written"] is True
    assert report["landed"]["overwrote"] is True
    doc = load_document(tmp_path / ".trove/kb" / DS / "semantics.yml")
    assert [d["name"] for d in doc["datasets"]] == [
        "orders", "customers"]


# ── stage ──────────────────────────────────────────────────


async def test_stage_requires_an_existing_model(tmp_path):
    with pytest.raises(SemanticImportError, match="还没有语义模型"):
        await run_semantic_import(
            tmp_path, source="cube", path=CUBE, datasource=DS, mode="stage")


async def test_stage_opens_change_without_touching_semantics(tmp_path):
    await _boot(tmp_path)
    semantics = tmp_path / ".trove/kb" / DS / "semantics.yml"
    before = semantics.read_bytes()

    report = await run_semantic_import(
        tmp_path, source="cube", path=CUBE, datasource=DS, mode="stage")

    assert semantics.read_bytes() == before
    landed = report["landed"]
    assert landed["written"] is True
    assert landed["duplicate"] is False
    assert landed["change_id"].startswith("chg-")

    records = ChangeService(KbService(tmp_path)).list(DS, status="open")
    assert len(records) == 1
    assert records[0].id == landed["change_id"]
    assert [p["kind"] for p in records[0].payloads] == (
        ["dataset"] * 3 + ["field"] * 5 + ["metric"] * 4 + ["relationship"])
    assert "[fp:" in records[0].note


async def test_stage_is_idempotent_by_payload_fingerprint(tmp_path):
    await _boot(tmp_path)
    first = await run_semantic_import(
        tmp_path, source="cube", path=CUBE, datasource=DS, mode="stage")
    second = await run_semantic_import(
        tmp_path, source="cube", path=CUBE, datasource=DS, mode="stage")

    assert second["landed"]["duplicate"] is True
    assert second["landed"]["written"] is False
    assert second["landed"]["change_id"] == first["landed"]["change_id"]
    assert len(ChangeService(KbService(tmp_path)).list(DS)) == 1


async def test_preview_onto_existing_model_suggests_stage(tmp_path):
    await _boot(tmp_path)
    report = await run_semantic_import(
        tmp_path, source="cube", path=CUBE, datasource=DS)

    assert report["suggestion"] == "stage"
    assert report["blocking"] == []
    assert report["landed"] is None


# ── 参数校验 ───────────────────────────────────────────────


@pytest.mark.parametrize("kwargs,message", [
    ({"source": "looker"}, "--source"),
    ({"mode": "apply"}, "--mode"),
    ({"manifest": MANIFEST}, "--manifest 只适用于"),
    ({"force": True}, "--force 只适用于"),
])
async def test_argument_validation(tmp_path, kwargs, message):
    base = {"source": "cube", "path": CUBE, "datasource": DS}
    base.update(kwargs)
    with pytest.raises(SemanticImportError, match=message):
        await run_semantic_import(tmp_path, **base)


async def test_datasource_name_must_be_path_safe(tmp_path):
    with pytest.raises(SemanticImportError, match="非法数据源名"):
        await run_semantic_import(
            tmp_path, source="cube", path=CUBE, datasource="../escape")
