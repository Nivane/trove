"""一致性对账(D6)—— 三对「声明副本 ↔ 运行期副本」的正反例。

正例钉在**真实包**上(干净树双向零漂移,任何一侧偷偷漂移即红);
反例全用注入式 roots(tmp 目录)造漂移,不打补丁、不碰真实文件。
"""

from __future__ import annotations

from pathlib import Path

from trove.services.validate.service import (
    parity_decision_functions,
    parity_skill_nodes,
    parity_templates,
)


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _checks(issues, prefix: str) -> list:
    return [i for i in issues if i.check.startswith(prefix)]


# ── ① skill triggers.node 名单 ↔ 图调用点 ────────────────


def test_skill_nodes_clean_on_real_package():
    assert parity_skill_nodes() == []


def test_skill_nodes_missing_call_site_is_error(tmp_path):
    """名单声明了节点、包里没有渲染调用点 = 该节点技能永不注入。"""
    _write(tmp_path, "nodes/only.py",
           'def f():\n    return render_skills("query_sketch")\n')
    issues = parity_skill_nodes(tmp_path)
    hits = _checks(issues, "parity.skill_nodes")
    assert hits and any("gen_sql" in i.message or "insights" in i.message
                        for i in hits)


def test_skill_nodes_extra_call_site_is_error(tmp_path):
    """调用点存在、名单没有 = validate 判定面比运行时窄(更阴险的方向)。"""
    from trove.services.validate.service import SKILL_NODES

    body = "".join(
        f'    render_skills("{n}")\n' for n in SKILL_NODES)
    _write(tmp_path, "nodes/all.py",
           f"def f():\n{body}    render_skills('new_node')\n")
    issues = parity_skill_nodes(tmp_path)
    assert any("new_node" in i.message for i in issues)


def test_decision_host_must_be_constant(tmp_path):
    _write(tmp_path, "nodes/h.py",
           'def f():\n    return validators_for("gen_sql")\n')
    issues = parity_skill_nodes(tmp_path)
    assert any("VALIDATOR_HOST" in i.message for i in issues)


# ── ② 决策 FUNCTIONS 表 ↔ Call.eval 实现 ─────────────────


def test_decision_functions_clean_on_real_expr():
    assert parity_decision_functions() == []


def test_decision_functions_both_directions(tmp_path):
    expr = _write(tmp_path, "expr_fixture.py", """
FUNCTIONS = {"abs": (1, 1), "min": (2, None)}

class Call:
    def eval(self):
        if self.name == "min":
            return 1
        if self.name == "max":
            return 2
        return None
""")
    issues = parity_decision_functions(expr)
    msgs = " | ".join(i.message for i in issues)
    assert "'abs'" in msgs and "无实现" in msgs      # 声明了求值落空
    assert "'max'" in msgs and "死分支" in msgs      # 实现了永远写不出
    assert all(i.severity == "error" for i in issues)


def test_decision_functions_blind_scanner_is_error(tmp_path):
    """两份副本都扫成空 —— 扫描器失明比漂移更糟,必须响亮。"""
    expr = _write(tmp_path, "expr_empty.py", "X = 1\n")
    issues = parity_decision_functions(expr)
    assert issues and "扫描器失明" in issues[0].message


# ── ③ 磁盘模板 ↔ 代码引用 ────────────────────────────────


def test_templates_clean_on_real_package():
    assert parity_templates() == []


def _mini(tmp_path: Path, *, prompts: dict[str, str],
          code: str) -> tuple[Path, Path]:
    proot = tmp_path / "prompts"
    proot.mkdir(exist_ok=True)
    for rel, text in prompts.items():
        _write(proot, rel, text)
    croot = tmp_path / "code"
    _write(croot, "mod.py", code)
    return proot, croot


def test_templates_happy_path(tmp_path):
    proot, croot = _mini(tmp_path, prompts={"gen/system.en.j2": "hi"},
                         code="from trove.prompts import render\n"
                              'render("gen/system")\n')
    assert parity_templates(prompts_root=proot, code_roots=[croot],
                            skill_names=[]) == []


def test_templates_ref_without_file_is_error(tmp_path):
    proot, croot = _mini(tmp_path, prompts={},
                         code="from trove.prompts import render\n"
                              'render("ghost/system")\n')
    issues = parity_templates(prompts_root=proot, code_roots=[croot],
                              skill_names=[])
    hits = _checks(issues, "parity.templates")
    assert hits and "ghost/system" in hits[0].message


def test_templates_file_without_ref_is_error(tmp_path):
    proot, croot = _mini(tmp_path, prompts={"dead/system.en.j2": "hi"},
                         code="from trove.prompts import render\n"
                              'render("gen/system")\n')
    _write(proot, "gen/system.en.j2", "hi")
    issues = parity_templates(prompts_root=proot, code_roots=[croot],
                              skill_names=[])
    assert any("dead/system" in i.message for i in issues)


def test_templates_bad_filename_is_error(tmp_path):
    """文件名不符合 <name>.<lang>.j2 —— 加载器永远找不到它。"""
    proot, croot = _mini(tmp_path, prompts={"weird.j2": "hi"}, code="")
    issues = parity_templates(prompts_root=proot, code_roots=[croot],
                              skill_names=[])
    assert any("weird.j2" in i.message for i in issues)


def test_templates_literal_indirection_is_covered(tmp_path):
    """attribution 的 prompt_name 三元式:名字不在 render 实参里,但在
    代码里以字面量存在 —— 方向 B 必须覆盖,不许误报死模板。"""
    proot, croot = _mini(
        tmp_path, prompts={"attr/user.en.j2": "hi"},
        code="from trove.prompts import render\n"
             'prompt_name = "attr/user"\n'
             "render(prompt_name)\n")
    assert parity_templates(prompts_root=proot, code_roots=[croot],
                            skill_names=[]) == []


def test_templates_skills_dynamic_expansion(tmp_path):
    """render(f"skills/{name}/system") + manifest 名单 → 磁盘闭合。"""
    code = ("from trove.prompts import render\n"
            "def f(name):\n"
            '    return render(f"skills/{name}/system")\n')
    proot, croot = _mini(tmp_path, prompts={"skills/alpha/system.en.j2": "x"},
                         code=code)
    assert parity_templates(prompts_root=proot, code_roots=[croot],
                            skill_names=["alpha"]) == []


def test_templates_skill_without_system_template_is_error(tmp_path):
    code = ("from trove.prompts import render\n"
            "def f(name):\n"
            '    return render(f"skills/{name}/system")\n')
    proot, croot = _mini(tmp_path, prompts={}, code=code)
    issues = parity_templates(prompts_root=proot, code_roots=[croot],
                              skill_names=["alpha"])
    assert any("alpha" in i.message and "system 模板" in i.message
               for i in issues)


def test_templates_render_without_import_is_not_counted(tmp_path):
    """没 import render 的文件里的同名调用(如 report.render())不是引用。"""
    proot, croot = _mini(tmp_path, prompts={"gen/system.en.j2": "hi"},
                         code='render("gen/system")\n')
    issues = parity_templates(prompts_root=proot, code_roots=[croot],
                              skill_names=[])
    # 磁盘文件因此"无人引用";但绝不能报"引用了不存在的模板"。
    assert not any("不存在的模板" in i.message for i in issues)


def test_templates_bilingual_single_side_ok(tmp_path):
    """只有 .zh(无 .en)也算存在:render(name, lang="zh") 找得到它。"""
    proot, croot = _mini(tmp_path, prompts={"gen/system.zh.j2": "hi"},
                         code="from trove.prompts import render\n"
                              'render("gen/system")\n')
    assert parity_templates(prompts_root=proot, code_roots=[croot],
                            skill_names=[]) == []
