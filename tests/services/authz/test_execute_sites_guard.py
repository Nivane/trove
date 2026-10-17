"""取数入口守卫(设计 §2.3):枚举仓库里所有「registry 风格」的 execute 调用点。

为什么是枚举而不是逐个断言:行/列授权最容易出的不是**判错**,而是
**新入口忘了判** —— 一个新增的 ``connectors.execute`` 调用点在功能上完全
正常,在安全上是一条绕过执行前 scope 的旁路。所以这里冻结一份调用点
清单:``scoped`` 的文件必须真的 import 了 scope 引擎;不认识的入口一律红,
提交人要么接线、要么把它登记为「系统/管理通道」并写明理由。

非目标(v1 明确不接,理由见 ``trove/services/authz/scope.py`` docstring):
decision/scan 系统任务、KB init/profiling/合成试跑、管理端语义预览、
metadata/目录。它们出现在清单里是**登记**,不是豁免理由的复用 —— 新入口
不能靠把它归到这些类别下混过去。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

#: registry 风格的接收者(``connectors.execute`` / ``registry.execute`` /
#: ``adapter.execute`` / ``self.connectors.execute``)。
_RECEIVERS = {
    "connectors", "registry", "adapter",
    "self.connectors", "self.registry", "self.adapter",
}

#: 调用点清单:相对路径 → 处置。
#:
#: - ``scoped``:用户可见取数路径,调用前过 Authorizer + 行/列 scope
#:   (直接或经 ``authorize_and_scope`` / ``apply_scope``)。
#: - ``system``:管理员/系统只读通道,不套个人 grant(决策 4)。
#: - ``infra``:数据源自身/健康探测/建库灌数,不是查询管线。
_SITES: dict[str, tuple[str, str]] = {
    # ── 用户可见取数路径(必须 scope)──────────────────────────
    "trove/workflow/nodes/execute_sql.py": (
        "scoped", "主图执行门:Authorizer A1–A3 + apply_scope,拒绝即终态"),
    "trove/workflow/nodes/gen_sql.py": (
        "scoped", "probe_query / check_result / search_values 执行前过同一道门"),
    "trove/workflow/nodes/select.py": (
        "scoped", "候选投票执行:scope 后执行,原串留给 execute_sql 唯一改写"),
    "trove/workflow/nodes/attribution.py": (
        "scoped", "归因 hop:引擎经编译器产出(声明 RLS 已注入),叠加主体 grant"),
    "trove/workflow/nodes/refuse.py": (
        "scoped", "自动确认试跑:越权 = 试跑失败 → pending 人工"),
    # ── 系统 / 管理通道(非目标,v1 不接)──────────────────────
    "trove/services/decision/service.py": (
        "system", "判定引擎:admin 配置的只读通道,只接 admin owner"),
    "trove/services/kb/enum_probe.py": (
        "system", "KB 枚举探测:管理员建模行为"),
    "trove/services/kb/profiling.py": (
        "system", "KB Top-K 值剖析:管理员建模行为"),
    "trove/services/kb/live_lint.py": (
        "system", "KB 活体 lint:管理员建模行为"),
    "trove/services/kb/synthetic.py": (
        "system", "合成示例试跑:管理员建模行为"),
    "trove/services/semantic_layer/verify.py": (
        "system", "语义声明验证:管理端校验面"),
    # ── 基础设施 ────────────────────────────────────────────
    "trove/services/datasource/registry.py": (
        "infra", "ConnectorRegistry 本体:适配器调用的唯一收口"),
    "trove/demo.py": (
        "infra", "内置 demo 建库灌数:安装期写入,不是查询"),
    "trove/api/app.py": (
        "infra", "健康探测 SELECT 1:零业务数据"),
    "trove/api/routers/overview.py": (
        "infra", "健康探测 SELECT 1:零业务数据"),
}


def _receiver(call: ast.Call) -> str:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "execute":
        return ""
    return ast.unparse(func.value)


def _call_sites() -> dict[str, list[int]]:
    """相对路径 → 行号列表(所有 registry 风格 execute 调用)。"""
    found: dict[str, list[int]] = {}
    for path in sorted((ROOT / "trove").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # 非本守卫的职责
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _receiver(node) in _RECEIVERS:
                rel = str(path.relative_to(ROOT))
                found.setdefault(rel, []).append(node.lineno)
    return found


def _scope_imported(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == (
                "trove.services.authz.scope"):
            names = {a.name for a in node.names}
            if names & {"apply_scope", "authorize_and_scope"}:
                return True
    return False


def test_every_execute_site_is_registered():
    """新取数入口必须先登记 —— 未登记 = 可能是绕过 scope 的旁路。"""
    sites = _call_sites()
    unknown = sorted(set(sites) - set(_SITES))
    assert not unknown, (
        "发现未登记的 registry.execute 调用点:\n  "
        + "\n  ".join(f"{p} (lines {sites[p]})" for p in unknown)
        + "\n用户可见取数入口必须复用 trove/services/authz/scope.py;"
        "系统/管理通道请登记进 _SITES 并写明理由(见模块 docstring)。"
    )


def test_registered_sites_still_exist():
    """清单不能变成化石:登记了却没有调用的路径要清理。"""
    sites = _call_sites()
    stale = sorted(set(_SITES) - set(sites))
    assert not stale, (
        "清单里的路径不再有 execute 调用(文件改名/调用移除后请同步清理):\n  "
        + "\n  ".join(stale)
    )


@pytest.mark.parametrize("rel", sorted(
    rel for rel, (kind, _why) in _SITES.items() if kind == "scoped"))
def test_scoped_sites_import_the_scope_engine(rel):
    """``scoped`` 不是自我声明 —— 文件里必须真的 import 了 scope 引擎。"""
    assert _scope_imported(ROOT / rel), (
        f"{rel} 被登记为 scoped,但没有 import "
        "apply_scope / authorize_and_scope"
    )
