"""只读姿态守卫(结构性):``trove/services/action/`` 物理上够不到业务库。

行动柱 = **外送提案层**:模板 → 提案 → 审批 → webhook → 回执,绝不写回
业务库。保证方式是结构性的而不是约定性的 —— 服务/分发器构造签名不收
connector 注册表,整个包不 import 数据源注册表 / 适配器 / SQL guard,
``execute_unsafe`` 保持零调用方。任何一次"顺手拿个 connector"的改动都在
这里响亮失败(新增文件自动纳入扫描),而不是等到评审时被看见。

``trove.storage`` 是**允许**的:那是 Trove 自己的内部状态库(提案/审批/
回执),不是用户业务数据源。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "trove" / "services" / "action"

#: 明令禁止的 import 前缀 —— 任一出现即姿态破防。
_FORBIDDEN_PREFIXES = (
    "trove.services.datasource",     # 连接器/适配器/注册表
    "trove.services.kb",             # KbService 持有连接器
    "trove.services.semantic_layer", # 编译产物可执行
    "trove.services.jobs",           # runner 持有连接器
    "trove.workflow",                # 图节点反向注入 services
    "trove.services.sql",            # SQL guard / validator
)

#: 名字级禁令:出现在表达式里即视为触达写路径。
_FORBIDDEN_NAMES = ("execute_unsafe", "connectors")


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
    assert not offenders, f"action 包不得 import 数据源/写路径: {offenders}"


def test_no_forbidden_names():
    offenders: list[str] = []
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for bad in _FORBIDDEN_NAMES:
            if bad in names:
                offenders.append(f"{path.name}: {bad}")
    assert not offenders, f"action 包不得引用连接器/写路径: {offenders}"


def test_action_service_takes_no_connectors():
    """构造签名 = (store, templates, dispatcher) + 配置标量,没有连接器入口。"""
    from trove.services.action.service import ActionService

    params = list(inspect.signature(ActionService.__init__).parameters)
    assert params == ["self", "store", "templates", "dispatcher", "enabled",
                      "approval_ttl_hours", "max_payload_bytes", "max_attempts",
                      "lang"]


def test_dispatcher_takes_channels_not_connectors():
    """分发器的全部输入是命名通道 + 可注入 transport。"""
    from trove.services.action.dispatcher import ActionDispatcher

    params = list(inspect.signature(ActionDispatcher.__init__).parameters)
    assert params == ["self", "channels", "timeout_s", "transport"]


def test_execute_unsafe_still_has_zero_callers():
    """行动是 ``execute_unsafe`` 的新邻居 —— 它必须仍然**只被定义**,无人调用。

    只认 AST 里的**调用**(docstring 里提一句不算调用方);定义处
    (``datasource/registry.py``)自然不在调用方之列。
    """
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
