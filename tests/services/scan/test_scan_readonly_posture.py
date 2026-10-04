"""只读姿态守卫(结构性):``trove/services/scan/`` 只能当**消费方**。

扫描是「读业务库、写草稿」的一层。保证方式是结构性的而不是约定性的:

  - 不 import 数据源适配器 / KB 服务 / jobs —— 执行面只有**注入的
    runner 可调用**(与 ``AnalysisEngine`` 的 HopRunner 同款),包内
    物理上够不到连接器注册表,也就没有"顺手写一笔"的路径;
  - 不 import 图节点(唯一例外见下)—— services 层不被 workflow 反向
    注入,是行动柱守卫立的规矩,这里照抄;
  - ``execute_unsafe`` 保持零调用方(全仓 AST 扫描)。

``analysis`` / ``decision`` 是**允许**的:扫描消费块序列/噪声带/编译 hop
与决策草稿门 —— 那是它的两条公开依赖,不是破防。

新增文件自动纳入扫描;任何一次"顺手拿个 connector"的改动在这里响亮失败,
而不是等人评审时看见。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "trove" / "services" / "scan"

#: 明令禁止的 import 前缀 —— 任一出现即姿态破防。
_FORBIDDEN_PREFIXES = (
    "trove.services.datasource",   # 连接器/适配器/注册表
    "trove.services.kb",           # KbService 持有连接器
    "trove.services.jobs",         # runner 持有连接器
)

#: workflow 包整体禁止(**唯一例外**):自然语言时间窗口的公共解析入口住在
#: ``workflow.nodes.parse_date``,决策规则层也走它(``DecisionService._resolve_window``
#: 的同一处 import)。允许多这一条,是因为窗口必须只有一个解析器 ——
#: 扫描自己造一份,「本月」在规则与扫描里就会是两个跨度。
_ALLOWED_WORKFLOW_IMPORTS = ("trove.workflow.nodes.parse_date",)

#: 名字级禁令:出现在表达式里即视为触达执行面。
_FORBIDDEN_NAMES = ("execute_unsafe", "connectors")


def _sources() -> list[Path]:
    files = sorted(PKG.rglob("*.py"))
    assert files, f"no sources under {PKG}"
    return files


def _imported_modules(tree: ast.Module) -> list[str]:
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
    return mods


def test_no_forbidden_imports():
    offenders: list[str] = []
    for path in _sources():
        for mod in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if mod.startswith(_FORBIDDEN_PREFIXES):
                offenders.append(f"{path.name}: {mod}")
    assert not offenders, f"scan 包不得 import 数据源/KB/jobs: {offenders}"


def test_workflow_imports_are_the_narrow_carve_out():
    offenders: list[str] = []
    for path in _sources():
        for mod in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if mod.startswith("trove.workflow") \
                    and mod not in _ALLOWED_WORKFLOW_IMPORTS:
                offenders.append(f"{path.name}: {mod}")
    assert not offenders, (
        f"scan 包只允许 import {_ALLOWED_WORKFLOW_IMPORTS}(时间窗口解析),"
        f"其余 workflow 面一律禁止: {offenders}")


def test_no_forbidden_names():
    offenders: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for bad in _FORBIDDEN_NAMES:
            if bad in names:
                offenders.append(f"{path.name}: {bad}")
    assert not offenders, f"scan 包不得引用连接器/写路径: {offenders}"


def test_scan_service_takes_a_runner_not_connectors():
    """执行面 = 注入的 runner + 注入的方言解析;构造签名没有连接器入口。"""
    from trove.services.scan.service import ScanService

    params = list(inspect.signature(ScanService.__init__).parameters)
    assert params == ["self", "kb", "semantic_dir", "runner", "dialect_of",
                      "config", "llm", "timeout_ms"], params


def test_execute_unsafe_still_has_zero_callers():
    """行动柱守卫立的同一把尺:``execute_unsafe`` 只被定义,无人调用。"""
    repo = PKG.parents[2]
    callers: list[str] = []
    for path in (repo / "trove").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else "")
            if name == "execute_unsafe":
                callers.append(f"{path.relative_to(repo)}:{node.lineno}")
    assert callers == [], f"execute_unsafe 不得有调用方: {callers}"
