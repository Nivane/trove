"""外部语义层导入(dbt / Cube)的统一入口。

    from trove.services.semantic_layer.importers import run_semantic_import
    report = await run_semantic_import(
        project_root, source="dbt", path="path/to/dbt", datasource="demo",
        mode="preview",
    )

纯解析 + 确定性落地:零 LLM / 零网络 / 零数据源连接。写盘只走两条既有
白名单路径 —— ``KbService.init_semantics``(bootstrap)与
``ChangeService.open``(stage,仅开审批单)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from trove.services.semantic_layer.importers.base import (
    ImportedSemantic,
    SemanticImportError,
)
from trove.services.semantic_layer.importers.landing import (
    land_bootstrap,
    land_stage,
    load_document,
    preview_report,
)

MODES = ("preview", "bootstrap", "stage")
SOURCES = ("dbt", "cube")


async def run_semantic_import(
    project_root: str | Path,
    *,
    source: str,
    path: str | Path,
    datasource: str,
    mode: str = "preview",
    manifest: str | Path | None = None,
    note: str = "",
    force: bool = False,
    dialect: str = "mysql",
) -> dict[str, Any]:
    """解析 → 预检 → 按模式落点;返回统一报告 dict。"""
    if source not in SOURCES:
        raise SemanticImportError(f"--source 必须是 {SOURCES} 之一")
    if mode not in MODES:
        raise SemanticImportError(f"--mode 必须是 {MODES} 之一")
    datasource = str(datasource or "").strip()
    if not datasource:
        raise SemanticImportError("--datasource 必填")
    from trove.services.datasource.naming import is_path_safe

    if not is_path_safe(datasource):
        raise SemanticImportError(f"非法数据源名: {datasource!r}")
    if manifest is not None and source != "dbt":
        raise SemanticImportError("--manifest 只适用于 --source dbt")
    if force and mode != "bootstrap":
        raise SemanticImportError("--force 只适用于 --mode bootstrap")

    imported = _parse(source, path, manifest)

    from trove.services.kb.service import KbService

    kb = KbService(Path(project_root))
    existing = load_document(kb.semantics_path(datasource))
    report = preview_report(
        imported, existing, mode=mode, datasource=datasource, dialect=dialect)

    if mode == "preview":
        return report

    if mode == "bootstrap":
        landed = await land_bootstrap(
            kb, datasource, imported, force=force, dialect=dialect)
    else:
        landed = await land_stage(
            kb, datasource, imported, note=note, dialect=dialect)
    report["landed"] = landed
    report["mode"] = mode
    return report


def _parse(
    source: str,
    path: str | Path,
    manifest: str | Path | None,
) -> ImportedSemantic:
    if source == "dbt":
        from trove.services.semantic_layer.importers.dbt import parse_dbt

        return parse_dbt(path, manifest)
    from trove.services.semantic_layer.importers.cube import parse_cube

    return parse_cube(path)


__all__ = [
    "ImportedSemantic",
    "MODES",
    "SOURCES",
    "SemanticImportError",
    "run_semantic_import",
]
