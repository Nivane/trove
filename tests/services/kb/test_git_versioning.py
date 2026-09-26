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


# ── 优化 1: commit 前 lint 门禁 ──────────────────────────


def test_commit_refused_by_lint_gate(git_repo: Path):
    """lint 发现问题 → commit 拒绝,暂存回滚,文件保留在工作区改动。"""
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text(
        "semantic_model:\n  - name: demo\n    datasets:\n"
        "      - name: loan\n        fields:\n"
        "          - name: amount\n"
        "            expression: {dialects: [{dialect: ANSI_SQL, expression: amount}]}\n",
        encoding="utf-8")

    def bad_lint(paths):
        return ["指标「x」重复定义"]  # 永远报错

    result = GitKb(kb.kb_dir, enabled=True).commit(
        "demo", "semantic: broken", lint=bad_lint)

    assert result["committed"] is False
    assert result["reason"] == "lint-failed"
    assert "重复定义" in result["issues"][0]
    # 暂存已回滚 → 没有 commit,但文件仍修改于工作区
    assert _git(git_repo, "log", "--oneline").stdout.strip() == ""
    status = _git(git_repo, "status", "--porcelain", "--untracked-files=all").stdout
    assert "semantics.yml" in status


def test_commit_passes_clean_lint(git_repo: Path):
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")

    result = GitKb(kb.kb_dir, enabled=True).commit(
        "demo", "semantic: ok", lint=lambda paths: [])

    assert result["committed"] is True


async def test_confirm_draft_goes_through_semantics_lint(git_repo: Path):
    """confirm 提交前跑真实 semantics lint:坏语义文件提交被拒。"""
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text(
        "semantic_model: []\n", encoding="utf-8")
    GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: init")

    from trove.services.semantic_layer.manage import SemanticManager
    manager = SemanticManager(kb)
    # 走 confirm 全链路:正常 metric 确认成功(有 lint 也不误伤)
    await manager.create_draft(
        "demo", "metric", "upsert", "good_metric",
        {"expression": "COUNT(loan.loan_id)"}, note="test")
    draft = manager.drafts("demo")["pending"][0]
    await manager.confirm_draft("demo", draft["id"], dialect="sqlite")
    assert "confirm metric good_metric" in _git(git_repo, "log", "--oneline").stdout

    # 现在写一份真正坏的文件(重复指标定义)→ semantics_lint 拒绝入库
    import yaml
    bad = {
        "semantic_model": [{
            "name": "demo",
            "metrics": [
                {"name": "m", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]}},
                {"name": "m", "expression": {"dialects": [
                    {"dialect": "ANSI_SQL", "expression": "COUNT(loan.loan_id)"}]}},
            ],
        }],
    }
    kb.semantics_path("demo").write_text(
        yaml.safe_dump(bad, allow_unicode=True, sort_keys=False), encoding="utf-8")

    issues = kb.semantics_lint("demo", dialect="sqlite")([kb.semantics_path("demo")])
    assert any("重复定义" in i for i in issues)

    result = GitKb(kb.kb_dir, enabled=True).commit(
        "demo", "semantic: should-be-refused",
        lint=kb.semantics_lint("demo", dialect="sqlite"))
    assert result["committed"] is False
    assert result["reason"] == "lint-failed"
    assert "should-be-refused" not in _git(git_repo, "log", "--oneline").stdout


# ── 优化 5: 结构化元数据 trailers ────────────────────────


def test_commit_carries_trailers(git_repo: Path):
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")

    result = GitKb(kb.kb_dir, enabled=True).commit(
        "demo", "semantic: confirm metric x",
        trailers={"Generator": "semantic.confirm", "Approved-by": "admin@x"})

    assert result["committed"] is True
    body = _git(git_repo, "log", "-1", "--format=%B").stdout
    assert "Generator: semantic.confirm" in body
    assert "Approved-by: admin@x" in body
    trailers = _git(git_repo, "log", "-1", "--format=%(trailers)").stdout
    assert "Generator:" in trailers and "Approved-by:" in trailers


async def test_confirm_draft_records_actor(git_repo: Path):
    kb = _kb_with_git(git_repo)
    from trove.services.semantic_layer.manage import SemanticManager

    manager = SemanticManager(kb)
    await manager.create_draft(
        "demo", "metric", "upsert", "avg_amount",
        {"expression": "AVG(loan.amount)"}, note="test")
    draft = manager.drafts("demo")["pending"][0]
    await manager.confirm_draft("demo", draft["id"], dialect="sqlite", actor="alice")

    body = _git(git_repo, "log", "-1", "--format=%B").stdout
    assert "Approved-by: alice" in body


# ── 优化 3: history / rollback ───────────────────────────


async def test_history_lists_commits(git_repo: Path):
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")
    GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: v1")
    kb.semantics_path("demo").write_text(
        "semantic_model:\n  - name: demo\n", encoding="utf-8")
    GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: v2")

    history = await kb.git_history("demo")

    assert len(history) >= 2
    assert history[0]["subject"] == "semantic: v2"
    assert history[1]["subject"] == "semantic: v1"
    assert history[0]["sha"] and history[0]["author"]


async def test_rollback_restores_previous_content(git_repo: Path):
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")
    GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: v1")
    v1 = _git(git_repo, "log", "-1", "--format=%H").stdout.strip()
    kb.semantics_path("demo").write_text(
        "semantic_model:\n  - name: demo\n    metrics:\n      - name: m\n",
        encoding="utf-8")
    GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: v2")

    result = await kb.git_rollback("demo", v1, trailers={"Generator": "test"})

    assert result["rolled_back"] is True
    content = kb.semantics_path("demo").read_text(encoding="utf-8")
    assert "metrics" not in content  # 回到 v1 状态
    assert _git(git_repo, "log", "-1", "--format=%B").stdout.strip().startswith("kb rollback")


async def test_rollback_bad_sha_fails(git_repo: Path):
    kb = _kb_with_git(git_repo)
    kb.semantics_path("demo").write_text("semantic_model: []\n", encoding="utf-8")
    GitKb(kb.kb_dir, enabled=True).commit("demo", "semantic: v1")

    result = await kb.git_rollback("demo", "deadbeef" * 5)

    assert result["rolled_back"] is False
    assert result["reason"] == "bad-sha"


# ── 优化 4: 原子提交(一次逻辑变更 = 一条 commit) ────────


async def test_confirm_is_single_atomic_commit(git_repo: Path):
    """confirm 同时写 semantics.yml + semantic_drafts.yml → 一条 commit。"""
    kb = _kb_with_git(git_repo)
    from trove.services.semantic_layer.manage import SemanticManager

    manager = SemanticManager(kb)
    await manager.create_draft(
        "demo", "metric", "upsert", "atomic_m",
        {"expression": "SUM(loan.amount)"}, note="test")
    before = _git(git_repo, "log", "--oneline").stdout.count("\n") + 1
    draft = manager.drafts("demo")["pending"][0]
    await manager.confirm_draft("demo", draft["id"], dialect="sqlite")

    after = _git(git_repo, "log", "--oneline").stdout.count("\n") + 1
    assert after == before + 1
    # 一条 commit 同时包含两个文件
    files = _git(git_repo, "log", "-1", "--name-only", "--format=").stdout.split()
    assert any(f.endswith("semantics.yml") for f in files)
    assert any(f.endswith("semantic_drafts.yml") for f in files)


# ── 门禁前移:坏语义在写盘前被拒(不只是拒绝进 git) ──────


def _seed_empty_semantics(kb: KbService) -> str:
    ds_dir = kb.kb_dir / "demo"
    ds_dir.mkdir(parents=True, exist_ok=True)
    path = kb.semantics_path("demo")
    path.write_text("semantic_model: []\n", encoding="utf-8")
    return path.read_text(encoding="utf-8")


async def test_confirm_draft_rejects_bad_semantics_before_write(tmp_path: Path):
    """lint 不过的草稿在 confirm 时被拒:不写盘、不刷新镜像、草稿仍 pending。"""
    from trove.services.semantic_layer.manage import SemanticManager

    kb = KbService(tmp_path / "proj")
    before = _seed_empty_semantics(kb)
    manager = SemanticManager(kb)
    # unique_keys 引用未声明的列 → lint_semantics 报错,但 _apply_draft 不拦。
    draft = await manager.create_draft(
        "demo", "dataset", "upsert", "courses",
        {"source": "courses", "unique_keys": [["ghost_col"]]})

    with pytest.raises(ValueError, match="拒绝写入"):
        await manager.confirm_draft("demo", draft["id"], dialect="sqlite")

    # 磁盘未被改写;草稿未被标记 applied
    assert kb.semantics_path("demo").read_text(encoding="utf-8") == before
    assert manager.drafts("demo")["pending"][0]["id"] == draft["id"]
    assert manager.drafts("demo")["applied"] == []


async def test_auto_apply_rejects_bad_semantics_before_write(tmp_path: Path):
    """auto_apply 同样在写盘前拦截坏语义(refuse 节点据此退回 pending 草稿)。"""
    from trove.services.semantic_layer.manage import SemanticManager

    kb = KbService(tmp_path / "proj")
    before = _seed_empty_semantics(kb)

    with pytest.raises(ValueError, match="拒绝写入"):
        await SemanticManager(kb).auto_apply(
            "demo", "dataset", "courses",
            {"source": "courses", "unique_keys": [["ghost_col"]]})

    assert kb.semantics_path("demo").read_text(encoding="utf-8") == before


async def test_confirm_draft_clean_semantics_still_writes(tmp_path: Path):
    """门禁不误伤:干净草稿照常写盘并进 git 审计历史。"""
    from trove.services.semantic_layer.manage import SemanticManager

    kb = KbService(tmp_path / "proj")
    _seed_empty_semantics(kb)
    manager = SemanticManager(kb)
    draft = await manager.create_draft(
        "demo", "metric", "upsert", "avg_amount",
        {"expression": "AVG(loan.amount)", "datasets": ["loan"]})

    await manager.confirm_draft("demo", draft["id"], dialect="sqlite")

    assert "avg_amount" in kb.semantics_path("demo").read_text(encoding="utf-8")
    assert manager.drafts("demo")["applied"][0]["name"] == "avg_amount"
