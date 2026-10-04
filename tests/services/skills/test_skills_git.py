"""Org skills 的写路径自动版本化 —— 与 KB 同一套 GitVersioning。

P2(生效/回滚/停用)的回滚半边:``.trove/skills/<name>/`` 此前不在任何版本
控制之下,一次写坏就只剩手工恢复。现在每条治理写路径(create / confirm /
reject / tier / body / rollback)都自动 commit,``git log`` 即变更审计史,
回滚 = 从历史里挑一个 commit 恢复成**一条新 commit**(历史不改写)。

守卫与 KB 写路径同源(同一个 ``GitVersioning`` 实现):只暂存点名文件、
绝不 ``git add -A``、没仓库/没改动/失败一律降级 no-op —— 版本化绝不阻断
它要记录的那次写入。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from trove.services.skills.service import SkillService


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """带身份配置的本地 git 仓库(零网络)。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@local")
    return repo


def _svc(repo: Path) -> SkillService:
    return SkillService(root=repo / ".trove" / "skills")


def _log(repo: Path) -> str:
    return _git(repo, "log", "--format=%s").stdout


def _draft(svc: SkillService, name: str = "s1", **overrides) -> dict:
    payload = {"name": name, "description": "对账口径", "body": "v1 body"}
    payload.update(overrides)
    return svc.create(payload)


class TestMutationsCommit:
    def test_each_governance_mutation_is_one_commit_with_version(
            self, git_repo: Path):
        """create v1 → confirm v2 → tier v3 → body v4:一条变更 = 一条 commit,
        subject 带当时的修订号(=== 盘上的 version)。"""
        svc = _svc(git_repo)
        _draft(svc)
        svc.confirm("s1", actor="alice")
        svc.set_tier("s1", "required", actor="bob")
        svc.update_body("s1", "v2 body — rewritten", actor="bob")

        subjects = _log(git_repo).splitlines()
        assert subjects == [
            "skills: body s1 v4",
            "skills: tier s1 v3",
            "skills: confirm s1 v2",
            "skills: create s1 v1",
        ]
        assert svc.read_skill("s1")["version"] == 4

    def test_commit_trailers_carry_generator_and_actor(self, git_repo: Path):
        """``Generator`` / ``Approved-by`` 进 git trailer:审计能分清谁批的、
        哪条路径写的(与 KB 的 semantic.confirm 同一形状)。"""
        svc = _svc(git_repo)
        _draft(svc)
        svc.confirm("s1", actor="alice")

        body = _git(git_repo, "log", "-1", "--format=%B").stdout
        assert "Generator: skills.confirm" in body
        assert "Approved-by: alice" in body
        trailers = _git(git_repo, "log", "-1", "--format=%(trailers)").stdout
        assert "Generator:" in trailers and "Approved-by:" in trailers

    def test_reject_commits_the_deletion(self, git_repo: Path):
        """reject 删目录 —— 删除也进审计史(目录作用域,不碰目录之外)。"""
        svc = _svc(git_repo)
        _draft(svc)
        svc.reject("s1", actor="alice")

        assert _log(git_repo).splitlines()[0] == "skills: reject s1"
        assert (git_repo / ".trove" / "skills" / "s1").exists() is False
        # 删除被记录:HEAD 里那个目录确实没了,且工作区干净
        tree = _git(git_repo, "ls-tree", "-r", "--name-only", "HEAD").stdout
        assert "s1/SKILL.md" not in tree
        assert _git(git_repo, "status", "--porcelain").stdout.strip() == ""

    def test_commit_stages_only_the_named_skill(self, git_repo: Path):
        """守卫:只暂存点名文件,绝不 ``git add -A``。

        工作区里另一个未跟踪的脏文件(用户在同一个仓库里做别的事)不得被
        卷进这条治理 commit —— 版本化是审计旁路,不是"顺手提交一切"。
        """
        svc = _svc(git_repo)
        _draft(svc)
        (git_repo / "unrelated.txt").write_text("dirty", encoding="utf-8")
        svc.update_body("s1", "v2 body", actor="dave")

        stat = _git(git_repo, "show", "--stat", "--format=", "HEAD").stdout
        assert "unrelated.txt" not in stat
        assert "SKILL.md" in stat
        assert "unrelated.txt" in _git(git_repo, "status", "--porcelain").stdout

    def test_no_repo_degrades_to_noop(self, tmp_path: Path):
        """KB/技能目录不在 git 工作树里(dev 常见)→ 写入照常,版本化静默 no-op。"""
        svc = SkillService(root=tmp_path / ".trove" / "skills")
        entry = _draft(svc)
        assert entry["status"] == "pending"          # 写入成功
        svc.confirm("s1")
        assert svc.read_skill("s1")["status"] == "confirmed"
        assert svc.history("s1") == []               # 没有历史可看,但不报错

    def test_git_disabled_is_a_clean_noop(self, git_repo: Path):
        svc = SkillService(root=git_repo / ".trove" / "skills", git_enabled=False)
        assert svc.git is None
        _draft(svc)
        svc.confirm("s1")
        assert svc.history("s1") == []
        r = svc.rollback("s1", "0" * 40)
        assert r == {"rolled_back": False, "reason": "disabled"}


class TestHistoryAndRollback:
    def test_history_lists_the_skill_commits(self, git_repo: Path):
        svc = _svc(git_repo)
        _draft(svc)
        svc.confirm("s1", actor="alice")

        history = svc.history("s1")
        assert [h["subject"] for h in history] == [
            "skills: confirm s1 v2", "skills: create s1 v1",
        ]
        assert history[0]["sha"] and history[0]["author"]
        assert "skills.confirm" in history[0]["trailers"]

    def test_history_of_a_missing_skill_raises(self, git_repo: Path):
        with pytest.raises(KeyError):
            _svc(git_repo).history("ghost")

    def test_rollback_restores_content_as_a_new_commit(self, git_repo: Path):
        """回滚 = 恢复旧内容 + **一条新 commit**;修订号继续前进。

        版本计数单调是刻意的:回滚是一次新的修订,不是时间倒流 —— 否则
        "回滚到 v2"与"v2 本身"在审计史里无法区分。
        """
        svc = _svc(git_repo)
        _draft(svc, body="v1 body")
        v1 = svc.history("s1")[0]["sha"]
        svc.confirm("s1")
        svc.update_body("s1", "v2 body", actor="alice")
        assert svc.read_skill("s1")["version"] == 3

        result = svc.rollback("s1", v1, actor="carol")

        assert result["rolled_back"] is True
        entry = result["entry"]
        assert entry["body"] == "v1 body"           # 内容回到目标版本
        assert entry["status"] == "pending"         # 连同状态一起回滚
        assert entry["version"] == 4                # max(3, 1) + 1 —— 继续前进
        # 历史不改写:旧 commit 全在,回滚自己也是一条 commit
        subjects = _log(git_repo).splitlines()
        assert subjects[0].startswith("skills: rollback s1 to ")
        assert "skills: body s1 v3" in subjects
        assert "skills: create s1 v1" in subjects
        body = _git(git_repo, "log", "-1", "--format=%B").stdout
        assert "Generator: skills.rollback" in body
        assert "Approved-by: carol" in body

    def test_rollback_removes_files_absent_at_the_target(self, git_repo: Path):
        """目标版本里没有的**已跟踪**文件要删掉 —— 否则"回到那一版"只回滚
        了一半;未跟踪文件既不删也不卷进提交(从未进过版本史的内容,回滚
        无权销毁也无权收编)。"""
        svc = _svc(git_repo)
        _draft(svc)
        v1 = svc.history("s1")[0]["sha"]
        override = svc.skill_dir("s1") / "SKILL.zh.md"
        override.write_text("中文覆盖正文\n", encoding="utf-8")
        svc.update_body("s1", "v2 body", actor="alice")  # 把覆盖文件一起提交
        assert override.exists()
        stray = svc.skill_dir("s1") / "notes.txt"
        stray.write_text("scratch, never committed\n", encoding="utf-8")

        result = svc.rollback("s1", v1, actor="carol")

        assert result["rolled_back"] is True
        assert not override.exists()
        assert svc.get_body("s1", "zh") == "v1 body"
        assert stray.exists()                       # 未跟踪 → 不删
        tree = _git(git_repo, "ls-tree", "-r", "--name-only", "HEAD").stdout
        assert "notes.txt" not in tree              # 也不进提交

    def test_rollback_bad_sha_fails(self, git_repo: Path):
        svc = _svc(git_repo)
        _draft(svc)
        r = svc.rollback("s1", "deadbeef" * 5)
        assert r == {"rolled_back": False, "reason": "bad-sha"}

    def test_rollback_of_missing_skill_raises(self, git_repo: Path):
        with pytest.raises(KeyError):
            _svc(git_repo).rollback("ghost", "0" * 40)
