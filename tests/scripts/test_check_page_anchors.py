"""``check_page_anchors.py`` 的守卫测试 —— 钉住「锚点写错了会被发现」。

和 ``test_check_drift_exit_code.py`` 一样,这个脚本此前也没有测试,而它即将
成为 20+ 页文档的**唯一**校验手段:文档站把每条能力锚到源码的具体行,行号飘了
比没有锚点更糟——读者点过去看到的是别的代码,而且没人会发现。

三件事必须钉住:

1. **存疑检查真的会响**。基准比对只能发现「漂移」;新写的锚点若基准与当前一致,
   比对恒等、永远报告干净——错的行号会一直错下去。冷启动时有 100+ 个新锚点,
   这是唯一能拦住它们的检查。
2. **--write 必须同步 anchor-base**。只改行号不改基准的话,下次再跑会拿已经改好
   的行号去旧基准里取原文,把锚点改到**完全错误**的位置——比不改更糟。
3. **同一行被引用多次要逐处改写**。按 (path, line) 去重后 str.replace 只匹配
   完全相同的标签串,可见文字不同的第二处会被漏掉。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_script():
    """按路径加载 scripts/check_page_anchors.py(它不是包的一部分)。"""
    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "check_page_anchors.py"
    )
    spec = importlib.util.spec_from_file_location("check_anchors_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ca():
    return _load_script()


# ── 迷你仓库 ────────────────────────────────────────────────────────
# 需要一个真的 git 仓库:脚本靠 `git show <rev>:<path>` 取基准原文。
# 这仍然是零网络的(仓库在 tmp_path 里)。

_SRC_V1 = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / "pkg").mkdir()
    (r / "pkg" / "mod.py").write_text(_SRC_V1, encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short", "main")


def _page(repo: Path, body: str, base: str | None = None) -> Path:
    """写一个文档页。base=None 表示**不写** anchor-base。"""
    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    tag = f"<code hidden>anchor-base: {base}</code>" if base else ""
    doc = docs / "page.html"
    doc.write_text(f"<!doctype html><html><body>{tag}{body}</body></html>", "utf-8")
    return doc


def _ref(path: str, line: int, text: str, sym: str) -> str:
    return (
        f'<a class="ref" href="https://github.com/Nivane/trove/blob/main/'
        f'{path}#L{line}">{text} <span class="ref-line">{sym}</span></a>'
    )


NO_COUNTS = {"nodes": None, "adapters": None}


def _run(ca, doc: Path, repo: Path, *, at="main", write=False):
    return ca.check_file(doc, repo, at, write, dict(NO_COUNTS))


# ── 存疑:出生即写错的锚点 ──────────────────────────────────────────

def test_suspect_flags_symbol_at_wrong_line(ca, repo):
    """核心回归:ref-line 里的标识符不在所引的那一行 —— 必须报出来。

    这一条基准比对是瞎的:页面基准与比对目标同为 main,比对恒等,
    drifted 永远是空的。
    """
    doc = _page(repo, _ref("pkg/mod.py", 5, "mod.py:5", "alpha"), _head(repo))
    drifted, unresolved, _counts, _dead, suspect, _out = _run(ca, doc, repo)

    assert not drifted, "同版本比对本就不该报漂移"
    assert not unresolved
    assert [s[2] for s in suspect] == ["alpha"]


def test_suspect_silent_when_symbol_is_actually_there(ca, repo):
    """写得对就不该响 —— 否则这个检查会被当成噪音关掉。"""
    doc = _page(repo, _ref("pkg/mod.py", 1, "mod.py:1", "alpha"), _head(repo))
    *_, suspect, _out = _run(ca, doc, repo)
    assert suspect == []


def test_suspect_skips_chinese_descriptions(ca, repo):
    """中文描述无法自动判断,跳过而不是猜。"""
    doc = _page(repo, _ref("pkg/mod.py", 5, "mod.py:5", "主图装配"), _head(repo))
    *_, suspect, _out = _run(ca, doc, repo)
    assert suspect == []


def test_suspect_ignores_out_of_range_line(ca, repo):
    """行号越界交给基准比对去报失效,这里不重复报。"""
    doc = _page(repo, _ref("pkg/mod.py", 999, "mod.py:999", "alpha"), _head(repo))
    *_, suspect, _out = _run(ca, doc, repo)
    assert suspect == []


# ── 基准:缺 anchor-base 不猜 ────────────────────────────────────────

def test_missing_anchor_base_reports_unresolved_not_guess(ca, repo):
    """有锚点却没写基准 —— 报失效,而不是默认拿某个版本去比。

    猜一个基准的后果是静默改写到错误位置,那比报错难查得多。
    """
    doc = _page(repo, _ref("pkg/mod.py", 1, "mod.py:1", "alpha"), base=None)
    drifted, unresolved, _c, _d, _s, out = _run(ca, doc, repo)

    assert drifted == []
    assert len(unresolved) == 1 and "(整页)" in unresolved[0][0]
    assert out is None


# ── 漂移与改写 ──────────────────────────────────────────────────────

def _shift_source(repo: Path) -> None:
    """在文件头部插两行,让 alpha 从 :1 漂到 :3。"""
    (repo / "pkg" / "mod.py").write_text(
        "# 新加的注释\n# 还是注释\n" + _SRC_V1, encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "shift")


def test_drift_then_write_syncs_base_in_one_step(ca, repo):
    """--write 必须**同一步**改行号并推进 anchor-base。

    只改行号不推进基准,下次再跑就会拿「已经改好的行号」去旧基准里取原文,
    把锚点改到完全错误的位置 —— 这个坑真踩过。
    """
    old = _head(repo)
    doc = _page(repo, _ref("pkg/mod.py", 1, "mod.py:1", "alpha"), old)
    _shift_source(repo)
    new = _head(repo)
    assert new != old

    drifted, *_rest, out = _run(ca, doc, repo, write=True)
    assert [d[2] for d in drifted] == [3]
    assert out is not None
    assert "#L3" in out and "mod.py:3" in out
    assert f"anchor-base: {new}" in out, "基准必须同步推进"


def test_write_is_idempotent(ca, repo):
    """写第二遍必须零字节变化。

    不幂等就是上面那个坑的另一面:改写把自己写成了新的输入。
    """
    doc = _page(repo, _ref("pkg/mod.py", 1, "mod.py:1", "alpha"), _head(repo))
    _shift_source(repo)

    *_r, out1 = _run(ca, doc, repo, write=True)
    doc.write_text(out1, encoding="utf-8")

    drifted2, unresolved2, _c2, _dead2, suspect2, out2 = _run(
        ca, doc, repo, write=True
    )
    assert drifted2 == [] and unresolved2 == [] and suspect2 == []
    assert out2 is None, "稳定状态下不该再产生改写"


def test_same_line_cited_twice_is_rewritten_twice(ca, repo):
    """同一 (path, line) 被两处引用、可见文字不同 —— 两处都要改。

    此前按 (path, line) 去重后 str.replace 只匹配完全相同的标签串,
    第二处被漏掉、留下陈旧锚点,恰恰是这个脚本要防的东西。
    """
    doc = _page(
        repo,
        _ref("pkg/mod.py", 1, "mod.py:1", "alpha")
        + _ref("pkg/mod.py", 1, "见 mod.py:1 这一行", "alpha"),
        _head(repo),
    )
    _shift_source(repo)

    _d, _u, _c, _dead, _s, out = _run(ca, doc, repo, write=True)
    assert out.count("#L3") == 2
    assert "#L1" not in out
    assert out.count("mod.py:3") == 2


def test_same_text_elsewhere_is_not_drift(ca, repo):
    """同文行不止一处时,不能只按「第一处」判定 —— 那是假漂移。

    真事:``graphs.py`` 的 ``tool_timeout_s=20.0,`` 在 :275 与 :980 各有一份
    (缩进不同、strip 后一致),而锚点引的是 :980。取首处会把一个**正确**的
    锚点报成漂移,而且 --write 会照着这个假答案把它改到错的位置。
    """
    (repo / "pkg" / "mod.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dup")
    doc = _page(repo, _ref("pkg/mod.py", 6, "mod.py:6", "beta"), _head(repo))

    drifted, unresolved, *_ = _run(ca, doc, repo)
    assert drifted == [], "所引行文字仍对得上,不该报漂移"
    assert unresolved == []


def test_ambiguous_move_reports_rather_than_guesses(ca, repo):
    """文字搬家了、且目标里同文多行 —— 报歧义,不要猜一个位置。"""
    (repo / "pkg" / "mod.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "dup")
    # 引 :2（`    return 1`），之后把它从原处删掉、只留 :6 那份
    doc = _page(repo, _ref("pkg/mod.py", 2, "mod.py:2", "return"), _head(repo))
    (repo / "pkg" / "mod.py").write_text(
        "def alpha():\n    pass\n\n\ndef beta():\n    return 1\n    return 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "move")

    drifted, unresolved, *_ = _run(ca, doc, repo)
    assert drifted == []
    assert len(unresolved) == 1 and "同文" in unresolved[0][2]


# ── 失效 ────────────────────────────────────────────────────────────

def test_removed_code_reports_unresolved(ca, repo):
    """基准里那一行的原文在目标里找不到 —— 报失效,不猜一个位置。"""
    doc = _page(repo, _ref("pkg/mod.py", 5, "mod.py:5", "beta"), _head(repo))
    (repo / "pkg" / "mod.py").write_text("def alpha():\n    return 1\n", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop beta")

    _d, unresolved, _c, _dead, _s, _out = _run(ca, doc, repo)
    assert len(unresolved) == 1
    assert "def beta():" in unresolved[0][2]


# ── 站内链接 ────────────────────────────────────────────────────────

def test_check_links_flags_missing_target(ca, repo):
    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "here.html").write_text("x", encoding="utf-8")

    bad = ca.check_links(
        docs / "here.html", '<a href="gone.html">x</a><a href="here.html">y</a>'
    )
    assert [b[0] for b in bad] == ["gone.html"]


def test_check_links_ignores_external_and_anchor(ca, repo):
    """外链与纯锚点不由本仓库保证,不该报。"""
    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    doc = docs / "here.html"
    doc.write_text("x", encoding="utf-8")

    html = (
        '<a href="https://example.com/nope">a</a>'
        '<a href="#section">b</a>'
        '<a href="mailto:x@y.z">c</a>'
        '<a href="//cdn.example.com/x.js">d</a>'
    )
    assert ca.check_links(doc, html) == []


def test_check_links_sees_script_and_img(ca, repo):
    """src 和 href 一样会腐坏 —— 只查 href 会漏掉资源。"""
    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    doc = docs / "here.html"
    doc.write_text("x", encoding="utf-8")

    bad = ca.check_links(doc, '<script src="assets/nav.js"></script>')
    assert [b[0] for b in bad] == ["assets/nav.js"]


# ── main() ──────────────────────────────────────────────────────────

def test_main_exit_codes(ca, repo, monkeypatch, capsys):
    """退出码是外部的判断依据:干净 0、有问题 1。"""
    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "clean.html").write_text("<p>无锚点无链接</p>", encoding="utf-8")

    argv = ["check_page_anchors.py", "--docs-dir", str(docs), "--repo", str(repo)]
    monkeypatch.setattr(sys, "argv", argv)
    assert ca.main() == 0

    (docs / "broken.html").write_text('<a href="nope.html">x</a>', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", argv)
    assert ca.main() == 1
    assert "死链" in capsys.readouterr().out
