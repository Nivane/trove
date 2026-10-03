"""只读姿态守卫:``trove/services/analysis/`` 结构性不可写业务库。

分析柱的全部 I/O 经注入的 runner(图内由 attribution 节点接
``connectors.execute``,只读)。包本身不得 import 数据源注册表 /
连接器 / 适配器,更不得触达 ``execute_unsafe`` —— 与行动柱同级的
结构性保证,靠源码断言钉死(新增文件自动纳入扫描)。
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "trove" / "services" / "analysis"

#: 明令禁止的 import 前缀(任何一处出现即姿态破防)
_FORBIDDEN_PREFIXES = (
    "trove.services.datasource",
    "trove.storage",
    "trove.services.kb",          # KB 服务持有连接器
    "trove.workflow",             # 图节点可触达 services(节点反向注入)
    "trove.services.jobs",
)
_FORBIDDEN_NAMES = ("execute_unsafe",)


def _sources() -> list[Path]:
    files = sorted(PKG.rglob("*.py"))
    assert files, f"no sources under {PKG}"
    return files


def test_no_forbidden_imports():
    offenders: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for mod in mods:
                if mod.startswith(_FORBIDDEN_PREFIXES):
                    offenders.append(f"{path.name}: {mod}")
    assert not offenders, f"analysis 包不得 import 数据源/存储层: {offenders}"


def test_no_execute_unsafe_reference():
    offenders: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for bad in _FORBIDDEN_NAMES:
            if bad in names:
                offenders.append(f"{path.name}: {bad}")
    assert not offenders, f"analysis 包不得引用写路径: {offenders}"


def test_engine_takes_runner_not_connectors():
    """引擎构造签名只有 (semantic_layer, runner, limits) —— 无连接器入口。"""
    import inspect

    from trove.services.analysis.engine import AnalysisEngine

    params = list(inspect.signature(AnalysisEngine.__init__).parameters)
    assert params == ["self", "semantic_layer", "runner", "limits"]
