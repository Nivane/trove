"""I1 写入收口守卫（结构性,AST 扫描）——「agent 不直写主线」的 CI 执行机制。

规则：「写语义文档的函数」= 函数体内同时出现语义目标引用（``semantics_path``
属性或 ``"semantics.yml"`` 字面量;**docstring 节点不算**——散文提及不是写入
目标,``kb/service.py::save_decisions`` 的 docstring 解释「只暂存 decisions.yml」
时会提一句 semantics.yml,不排除它守卫就会把一个 decisions.yml 写入点误报成
语义写入路径)与写入调用（``_dump_yaml`` / ``_write_doc``
/ ``_write_init`` / ``_init_write`` / ``_init_doc`` / ``write_text``）。每个
命中归位到 ``(文件, 外层函数)``，白名单之外即失败。

**棘轮只缩不涨**：白名单是 spec §4 I1 的两处豁免（人工命令 append_term、
初始化 init_terms/init_semantics）与唯一收口点（changes.py merge 家族）。
今日 ``manage.py`` 的 confirm/auto_apply 两个站点在收口后必须从检出中消失。

**诚实边界**：这是防手滑的棘轮,不是沙箱 —— 用 ``write_text`` 且路径不含
字面量的绕过测不到；它挡的是「又开了一条正经的写入路径」（见 §9.3）。

**白名单也不等于「全部写者清单」**：``trove/services/kb/git_versioning.py``
的 ``git restore --source <sha>``（KB 回滚 ``rollback`` / ``rollback_tree``）
同样会改写 ``semantics.yml``，但**构造上看不见** —— 它不写文件,是把 git 当
写者,AST 规则里没有任何写入调用可命。回滚是**恢复到已被提交的历史版本**,与
「agent 新写一份语义」是两回事,刻意不纳入规则;但读者不可因此把白名单当作
写者全集。
"""
from __future__ import annotations

import ast
from pathlib import Path

TROVE = Path(__file__).resolve().parents[3] / "trove"
_WHITELIST = {
    ("services/semantic_layer/changes.py", "merge"),
    ("services/semantic_layer/changes.py", "_write_merge"),
    ("services/semantic_layer/staging.py", "stage"),
    ("services/kb/service.py", "append_term"),
    ("services/kb/service.py", "init_terms"),
    ("services/kb/service.py", "init_semantics"),
}
_WRITE_CALLS = {"_dump_yaml", "_write_doc", "_write_init", "_init_write",
                "_init_doc", "write_text"}


def _callee_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _docstring_nodes(func: ast.AST) -> set[int]:
    """函数（含嵌套定义的函数/类）的 docstring 常量节点 —— 散文不算写入目标。

    只排除每个定义体首句的常量表达式:函数体内任何真正的字面量（含赋值给
    局部变量再拼路径的写法）照旧检出。
    """
    out: set[int] = set()
    for n in ast.walk(func):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and n.body and isinstance(n.body[0], ast.Expr) \
                and isinstance(n.body[0].value, ast.Constant):
            out.add(id(n.body[0].value))
    return out


def _violations(source: str, rel: str) -> list[str]:
    tree = ast.parse(source)
    hits: list[str] = []
    for func in [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        docstrings = _docstring_nodes(func)
        touches_target = False
        writes = False
        for node in ast.walk(func):
            if isinstance(node, ast.Attribute) and "semantics_path" in node.attr:
                touches_target = True
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and "semantics.yml" in node.value \
                    and id(node) not in docstrings:
                touches_target = True
            if isinstance(node, ast.Call) and _callee_name(node) in _WRITE_CALLS:
                # write_text 只在语义目标已出现时算 —— 避免把一切文本写当违例
                writes = True
        if touches_target and writes:
            if (rel, func.name) not in _WHITELIST:
                hits.append(f"{rel}::{func.name}")
    return hits


def test_checker_catches_a_synthetic_violation():
    """自证机制：新开一条写入路径必须被检出（守卫本身也要有测试）。"""
    bad = (
        "from pathlib import Path\n"
        "def sneaky(kb, ds, data):\n"
        "    path = kb.kb_dir / ds / 'semantics.yml'\n"
        "    path.write_text('x')\n"
    )
    assert _violations(bad, "somewhere.py") == ["somewhere.py::sneaky"]


def test_checker_ignores_read_only_consumers():
    ok = (
        "def read_it(kb, ds):\n"
        "    return kb.semantics_path(ds).read_text()\n"
    )
    assert _violations(ok, "somewhere.py") == []


def test_checker_ignores_docstring_mentions():
    """docstring 提一句 semantics.yml 不算写入目标（save_decisions 的假阳源）。

    同一个函数若在**体内**出现该字面量,仍必须检出（排除只针对 docstring）。
    """
    prose_only = (
        "def save_something(kb, ds, doc):\n"
        "    \"\"\"写回整份 decisions.yml(semantics.yml 不受影响)。\"\"\"\n"
        "    _write_doc(kb.decisions_path(ds), doc)\n"
    )
    assert _violations(prose_only, "somewhere.py") == []
    real_literal = (
        "def sneaky(kb, ds, doc):\n"
        "    \"\"\"无害的说明。\"\"\"\n"
        "    target = kb.kb_dir / ds / 'semantics.yml'\n"
        "    _write_doc(target, doc)\n"
    )
    assert _violations(real_literal, "somewhere.py") == ["somewhere.py::sneaky"]


def test_no_unwhitelisted_semantics_writers():
    offenders: list[str] = []
    for path in sorted(TROVE.rglob("*.py")):
        rel = str(path.relative_to(TROVE))
        offenders += _violations(path.read_text(encoding="utf-8"), rel)
    assert offenders == [], (
        "semantics.yml 出现了白名单之外的写入路径 —— agent 不直写主线（I1）："
        f"{offenders}")


def test_manage_py_write_sites_are_gone():
    """棘轮的机械证据：收口后 manage.py 不再命中。"""
    path = TROVE / "services" / "semantic_layer" / "manage.py"
    assert _violations(path.read_text(encoding="utf-8"),
                       "services/semantic_layer/manage.py") == []
