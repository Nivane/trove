"""直驱图的脚本必须携带主体 —— authz A1 缺主体即拒绝(2026-09-29 c77b434)。

三个脚本(eval_bird / offline_eval / lang_ab)绕过 SessionManager 的按会话
主体现算自己构造 WorkflowState,A1 落地时没有同步更新 —— 于 2026-10-02 的
P1 评测点火上暴露:每一题都在 execute_sql 被 AUTHZ_NO_PRINCIPAL 拒绝,判定
全部落成 EXECUTION_ERROR,而脚本本身一路"正常"跑完(静默失效,不是崩溃)。

这条测试用 AST 静态钉住:scripts/ 下任何 ``WorkflowState(...)`` 构造都必须
显式给出 ``principal`` 关键字。修法(与 SessionManager 的 no-auth 分支同口径)
是 ``principal=principal_to_wire(Policy.local_admin())`` —— 这里只钉"有没有",
不钉"是哪个",主体选择仍由各脚本自己决定。
"""

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
# 已知的直驱图脚本。数量下限防"glob 没扫到 → 空集恒过"的假绿。
KNOWN_DIRECT_DRIVERS = {"eval_bird.py", "offline_eval.py", "lang_ab.py"}


def _state_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name == "WorkflowState":
            calls.append(node)
    return calls


def test_direct_graph_scripts_present_principal():
    offenders: list[str] = []
    seen_files: set[str] = set()
    for path in sorted(SCRIPTS.glob("*.py")):
        for call in _state_calls(ast.parse(path.read_text(encoding="utf-8"))):
            seen_files.add(path.name)
            if not any(kw.arg == "principal" for kw in call.keywords):
                offenders.append(f"{path.name}:{call.lineno}")

    assert not offenders, (
        "直驱图的脚本构造 WorkflowState 必须带 principal="
        "principal_to_wire(Policy.local_admin()) —— authz A1 缺主体即拒绝,"
        f"漏了会整轮静默失效: {', '.join(offenders)}"
    )
    missing = KNOWN_DIRECT_DRIVERS - seen_files
    assert not missing, (
        f"探针失效:已知直驱脚本没扫到 WorkflowState 构造点 {sorted(missing)}"
        " —— 脚本改名/移动时同步更新本清单"
    )
