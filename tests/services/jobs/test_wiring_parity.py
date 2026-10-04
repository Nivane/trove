"""装配 parity:两个 SchedulerRunner 构造点必须注入**同一组件集**。

装配点有两处:

- ``trove/main.py`` —— ``create_app_components``,serve 的 lifespan 用它;
- ``trove/cli/schedule_cmds.py`` —— ``trove schedule --daemon`` 自己再建一次。

历史上第二处漏了 ``decision`` / ``verdicts`` / ``actions`` / ``subscriptions``
四个注入:daemon 跑起来"看着正常",但决策任务只能报未接线、触发型规则静默
不提案、订阅不投递 —— 属于最难发现的一类缺陷(没有异常,只有永远不发生的事)。

这里用 AST(零运行时、零网络)钉住三件事:

1. 两处的 ``SchedulerRunner(...)`` 关键字参数集合**完全相同**;
2. daemon 侧的组件一律来自 ``components[...]``(共享构造点取件,不就地 new);
3. ``create_app_components`` 返回的 dict 真的带着每个被注入的组件名。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MAIN = REPO / "trove" / "main.py"
SCHEDULE = REPO / "trove" / "cli" / "schedule_cmds.py"

#: 不是组件、而是标量的注入(两侧都直接传,不查 components)。
_SCALAR_KWARGS = {"lang"}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _runner_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else \
            func.id if isinstance(func, ast.Name) else ""
        if name == "SchedulerRunner":
            calls.append(node)
    return calls


def _kwargs(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _single_runner_call(path: Path) -> ast.Call:
    calls = _runner_calls(_parse(path))
    assert len(calls) == 1, (
        f"{path.name}: 期望恰好一个 SchedulerRunner 构造点,实际 {len(calls)} 个"
        " —— 多出来的构造点就是 parity 会漂的地方")
    return calls[0]


def test_both_sites_inject_the_same_component_set():
    serve = _kwargs(_single_runner_call(MAIN))
    daemon = _kwargs(_single_runner_call(SCHEDULE))
    assert set(serve) == set(daemon), (
        "serve 与 daemon 的注入集必须一致;"
        f"只在 serve: {sorted(set(serve) - set(daemon))};"
        f"只在 daemon: {sorted(set(daemon) - set(serve))}")
    assert "decision" in serve, "decision 是四件必注入之一(缺它决策任务只能报未接线)"


def test_daemon_takes_components_from_the_shared_factory():
    """daemon 侧一律 ``components["<name>"]`` 取件 —— 就地 new 就是本缺陷成因。"""
    daemon = _kwargs(_single_runner_call(SCHEDULE))
    for name, value in daemon.items():
        if name in _SCALAR_KWARGS:
            continue
        assert isinstance(value, ast.Subscript), (
            f"daemon 的 {name} 不是 components[...] 取件: {ast.dump(value)[:80]}")
        assert isinstance(value.value, ast.Name) and value.value.id == "components", (
            f"daemon 的 {name} 必须从 create_app_components 的返回值取件")
        key = value.slice
        assert isinstance(key, ast.Constant) and key.value == name, (
            f"daemon 的 {name} 取的却是 components[{getattr(key, 'value', '?')!r}]")


def test_shared_factory_exports_every_injected_component():
    """components 字典必须带着每个被注入的名字(否则 daemon 侧取到 None)。"""
    tree = _parse(MAIN)
    factory = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "create_app_components")
    keys: set[str] = set()
    for node in ast.walk(factory):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
    assert keys, "create_app_components 返回的 dict 没找到(形状变了?)"

    serve = _kwargs(_single_runner_call(MAIN))
    for name in serve:
        if name in _SCALAR_KWARGS:
            continue
        assert name in keys, f"components 缺 {name!r}: daemon 侧会取到 None"
