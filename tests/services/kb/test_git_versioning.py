"""GitKb — KB 语义文件 git 版本管理(语义即代码)测试。

核心断言:KB 写操作(auto-commit 钩子)在 KB 位于 git 工作树内时产生一条
commit,git log 即审计历史;非 git 仓库 / 关闭 / 无变更时静默 no-op。
"""

import subprocess
from pathlib import Path

import pytest

from trove.services.kb.git_versioning import GitKb
from trove.services.kb.service import KbService


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """一个带身份配置的裸 git 仓库(零网络,纯本地)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@local")
    return repo


def _kb_with_git(repo: Path, kb_sub: str = ".trove/kb") -> KbService:
    kb = KbService(repo, git_kb=True)
    kb.kb_dir.mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
    return kb


# ── GitKb 单元 ───────────────────────────────────────────


def test_commit_creates_audit_commit(git_repo: Path):
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text("semantic_model:\n  - name: demo\n", encoding="utf-8")

    result = GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: init demo")

    assert result["committed"] is True
    log = _git(git_repo, "log", "--oneline", "-1").stdout.strip()
    assert "semantic: init demo" in log
    # 文件已被跟踪
    assert _git(git_repo, "ls-files").stdout.count(".trove/kb/demo/semantics.yml") == 1


def test_commit_not_in_repo_is_noop(tmp_path: Path):
    kb = KbService(tmp_path / "proj")  # tmp 下无 .git
    kb.kb_dir.mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")

    result = GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: x")

    assert result["committed"] is False
    assert result["reason"] == "no-repo"


def test_commit_disabled_is_noop(git_repo: Path):
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")

    result = GitKb(kb.kb_dir, enabled=False).commit("demo", "semantic: x")

    assert result["committed"] is False
    assert result["reason"] == "disabled"


def test_commit_nothing_changed_is_noop(git_repo: Path):
    kb = _kb_with_git(git_repo)
    path = kb.semantics_path("demo")
    path.write_text("semantic_model: []\n", encoding="utf-8")
    GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: init demo")

    # 同一内容再提交 → 无变更
    result = GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: no-op")

    assert result["committed"] is False
    assert result["reason"] == "nothing-to-commit"


def test_commit_explicit_files_only(git_repo: Path):
    """只 stage 指定的文件,不扫进同一数据源的其他 YAML。"""
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")
    (kb.kb_dir / "demo" / "examples.yml").write_text("examples: []\n", encoding="utf-8")

    result = GitKb(kb.kb_dir, enabled=True).commit(
        "demo", "semantic: only semantics", files=["semantics.yml"])

    assert result["committed"] is True
    tracked = _git(git_repo, "ls-files").stdout
    assert "semantics.yml" in tracked
    assert "examples.yml" not in tracked


def test_commit_deleted_datasource(git_repo: Path):
    """delete_kb:整个数据源目录删除也能记录。"""
    kb = _kb_with_git(git_repo)
    path = kb.semantics_path("demo")
    path.write_text("semantic_model: []\n", encoding="utf-8")
    GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: init demo")
    path.unlink()

    result = GitKb(kb.kb_dir, enabled=True).commit("demo", "kb: delete KB", deleted=True)

    assert result["committed"] is True
    assert _git(git_repo, "status", "--porcelain").stdout.strip() == ""


# ── KbService 写操作自动 commit ──────────────────────────


async def test_append_term_auto_commits(git_repo: Path):
    kb = _kb_with_git(git_repo)
    await kb.append_term(
        {"term": "平均贷款", "mapping": "AVG(loan.amount)", "tables": ["loan"]},
        "demo", generator="test",
    )
    log = _git(git_repo, "log", "--oneline", "-1").stdout.strip()
    assert "append term" in log
    assert "平均贷款" in log


async def test_confirm_draft_auto_commits(git_repo: Path):
    kb = _kb_with_git(git_repo)
    from trove.services.semantic_layer.manage import SemanticManager

    manager = SemanticManager(kb)
    await manager.create_draft(
        "demo", "metric", "upsert", "avg_amount",
        {"expression": "AVG(loan.amount)"}, note="test")
    before = _git(git_repo, "log", "--oneline").stdout.count("\n") + 1

    draft = manager.drafts("demo")["pending"][0]
    await manager.confirm_draft("demo", draft["id"])

    # create_draft + confirm_draft 各一条,且 message 标识确认动作
    log = _git(git_repo, "log", "--oneline", "-5").stdout
    assert "confirm metric avg_amount" in log
    assert _git(git_repo, "log", "--oneline").stdout.count("\n") + 1 == before + 1


async def test_auto_apply_commits(git_repo: Path):
    kb = _kb_with_git(git_repo)
    from trove.services.semantic_layer.manage import SemanticManager

    await SemanticManager(kb).auto_apply(
        "demo", "metric", "total_amount", {"expression": "SUM(loan.amount)"})

    log = _git(git_repo, "log", "--oneline", "-1").stdout.strip()
    assert "auto-apply metric total_amount" in log


async def test_reject_draft_commits(git_repo: Path):
    kb = _kb_with_git(git_repo)
    from trove.services.semantic_layer.manage import SemanticManager

    manager = SemanticManager(kb)
    await manager.create_draft(
        "demo", "metric", "upsert", "bad_metric",
        {"expression": "SUM(bogus.col)"}, note="test")
    draft = manager.drafts("demo")["pending"][0]
    await manager.reject_draft("demo", draft["id"])

    log = _git(git_repo, "log", "--oneline", "-1").stdout.strip()
    assert "reject draft" in log


async def test_no_git_repo_does_not_break_writes(tmp_path: Path):
    """KB 在非 git 目录:写操作照常,自动 commit 静默跳过。"""
    kb = KbService(tmp_path / "proj", git_kb=True)
    kb.kb_dir.mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)

    await kb.append_term(
        {"term": "均值", "mapping": "AVG(loan.amount)", "tables": ["loan"]},
        "demo", generator="test",
    )

    path = kb.kb_dir / "demo" / "semantics.yml"
    assert path.exists()
    assert "均值" in path.read_text(encoding="utf-8")


async def test_git_kb_disabled_never_commits(git_repo: Path):
    kb = KbService(git_repo, git_kb=False)
    kb.kb_dir.mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)

    await kb.append_term(
        {"term": "均值", "mapping": "AVG(loan.amount)", "tables": ["loan"]},
        "demo", generator="test",
    )

    assert _git(git_repo, "log", "--oneline").stdout.strip() == ""


async def test_relative_kb_dir_still_commits(git_repo: Path, monkeypatch):
    """kb_dir 以相对路径构造(如 KbService('.') )时也能正确提交。"""
    monkeypatch.chdir(git_repo)
    kb = KbService(".", git_kb=True)  # kb_dir = "./.trove/kb" 相对路径
    kb.kb_dir.mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / "demo").mkdir(parents=True, exist_ok=True)

    await kb.append_term(
        {"term": "相对路径", "mapping": "AVG(loan.amount)", "tables": ["loan"]},
        "demo", generator="test",
    )

    log = _git(git_repo, "log", "--oneline", "-1").stdout.strip()
    assert "append term" in log


def test_commit_falls_back_when_no_identity(tmp_path: Path, monkeypatch):
    """无 git 身份配置时,commit 兜底机器身份,审计历史不断。"""
    # 隔离全局 git 配置(本机有 user.name/email,不清掉测不到兜底路径)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    repo = tmp_path / "noid"
    repo.mkdir()
    _git(repo, "init", "-q")
    kb = _kb_with_git(repo)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")

    result = GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: init demo")

    assert result["committed"] is True
    author = _git(repo, "log", "-1", "--format=%an <%ae>").stdout.strip()
    assert "trove" in author
