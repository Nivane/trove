"""导入器包的姿态守卫:零 LLM / 零数据源连接 / 不直接写盘。

导入器只做「读文件 → IR → 既有写入门」,任何直达 LLM、适配器、连接器的
import 都意味着那条写路径绕开了确认门。AST 钉住,不靠自觉。
"""

from __future__ import annotations

import ast
from pathlib import Path

TROVE = Path(__file__).resolve().parents[4] / "trove"
IMPORTERS = TROVE / "services" / "semantic_layer" / "importers"

FORBIDDEN_PREFIXES = (
    "trove.llm",
    "trove.workflow",
    "trove.api",
    "trove.services.datasource.adapters",
    "trove.services.datasource.registry",
    "trove.services.jobs",
    "trove.services.action",
)
FORBIDDEN_EXTERNAL = (
    "litellm",
    "openai",
    "anthropic",
    "httpx",
    "requests",
    "aiohttp",
    "aiomysql",
    "asyncpg",
    "clickhouse_connect",
    "duckdb",
    "psycopg",
    "psycopg2",
    "snowflake",
    "google.cloud",
)

_WRITE_METHODS = {"write_text", "write_bytes"}


def _files() -> list[Path]:
    return sorted(IMPORTERS.glob("*.py"))


def _imported_modules(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module)
    return out


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def test_importers_do_not_reach_llm_connectors_or_graph():
    assert _files(), "importers 包不应为空"
    violations: list[str] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if any(_matches(module, p) for p in FORBIDDEN_PREFIXES):
                violations.append(f"{path.name}: {module}")
            if any(_matches(module, p) for p in FORBIDDEN_EXTERNAL):
                violations.append(f"{path.name}: {module}")
    assert violations == []


def test_importers_never_write_files_directly():
    violations: list[str] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                modes = [a for a in node.args[1:2] if isinstance(a, ast.Constant)]
                modes += [
                    kw.value for kw in node.keywords
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant)
                ]
                if any(isinstance(m.value, str) and set(m.value) & set("wax+")
                       for m in modes):
                    violations.append(f"{path.name}:{node.lineno}: open(...,mode=w)")
            if isinstance(func, ast.Attribute) and func.attr in _WRITE_METHODS:
                violations.append(f"{path.name}:{node.lineno}: {func.attr}")
    assert violations == []


def test_importers_only_import_path_safe_from_datasource():
    """唯一允许触达 datasource 的面是纯字符串校验 ``naming``。"""
    allowed = {"trove.services.datasource.naming"}
    offenders: list[str] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if _matches(module, "trove.services.datasource") and module not in allowed:
                offenders.append(f"{path.name}: {module}")
    assert offenders == []
