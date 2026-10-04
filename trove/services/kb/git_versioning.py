"""Best-effort git versioning of file-backed assets (config-as-code).

Born for the KB (every ``/kb init`` / learn / draft approve-reject /
lesson confirm / delete auto-commits the affected datasource's YAML so
``git log`` is the change audit history — diff, blame and rollback come
free from git itself), now also serving any other admin-managed asset
directory whose files are the single source of truth: the org skills
tree (``.trove/skills/<name>/SKILL.md``) is versioned through the same
class via the file-scoped surface at the bottom (``commit_files`` /
``history_files`` / ``rollback_tree``). One implementation means the
guardrails below cannot drift between the two write paths.

Versioning guardrails (beyond plain auto-commit):

- **Pre-commit lint gate** — a ``lint`` callback can refuse the commit
  and roll back staging when the staged semantics carry structural
  issues (bad expressions / duplicate definitions / bad relationships).
  Bad semantics never enter the audit history.
- **Structured metadata** — ``trailers`` append git trailers to the
  commit message (``Generator:``, ``Approved-by:``), so ``git blame``
  can trace who approved and what produced each change.
- **Atomic commits** — each logical write passes its full file list
  (e.g. semantics.yml + semantic_drafts.yml in one commit), so a change
  is reversible as a single unit.
- **history / rollback** — the admin API can list the datasource's
  commits and restore its KB files to any past commit (new commit, the
  history is never rewritten).

Design constraints:

- **Single source of truth stays the YAML** — git only records snapshots
  after the fact; it never participates in reading. This is a pure audit
  trail on top of the existing ``KbService`` write paths.
- **Best-effort, never blocks** — the KB dir not inside a git work tree
  (e.g. ``~/.trove`` outside a repo), ``git`` missing, an empty commit,
  or a failed commit all degrade to a logged no-op. KB writes never fail
  because versioning failed.
- **Scoped staging** — only the datasource's own ``*.yml`` files are
  staged (never ``git add -A``), so unrelated working-tree changes are
  never swept into the audit commit.
- **Config-gated** — disabled when ``git_kb: false`` (default on).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

#: 自动 commit 的作者兜底。git 全局/仓库身份缺失时 commit 会失败,这里给一个
#: 可辨识的机器身份而不是让审计历史断掉(可用 TROVE_GIT_KB_AUTHOR 覆盖,
#: 格式 "Name <email>")。
_DEFAULT_AUTHOR = "trove <trove@local>"

_KB_YML = "*.yml"


def _repos_rel(paths: Iterable[Path], repo: Path) -> list[str]:
    """绝对路径 → 仓库相对路径;仓库外的路径直接丢弃(版本化只覆盖树内)。"""
    out: list[str] = []
    for p in paths:
        try:
            out.append(str(p.resolve().relative_to(repo)))
        except ValueError:
            continue
    return out


class GitVersioning:
    """Auto-commit a tree's own files to the enclosing git repository.

    Resolves the git work tree root by walking up from ``kb_dir`` (the
    tree may live anywhere, e.g. ``<repo>/.trove/kb`` or an external
    ``--kb-dir``). Two surfaces:

    - **datasource-scoped** (``commit`` / ``history`` / ``rollback``): the
      KB's ``<datasource>/*.yml`` files — the original caller;
    - **file-scoped** (``commit_files`` / ``history_files`` /
      ``rollback_tree``): explicit paths under the root — any other asset
      directory (org skills) that must not be modelled as a datasource.

    Both stage **only named paths** (a scoped ``git add -- <path>``, never
    a bare ``git add -A``), so unrelated working-tree changes are never
    swept into an audit commit.
    """

    def __init__(self, kb_dir: str | Path, enabled: bool = True,
                 author: str | None = None) -> None:
        self.kb_dir = Path(kb_dir)
        self.enabled = enabled
        self.author = author or os.environ.get("TROVE_GIT_KB_AUTHOR", _DEFAULT_AUTHOR)

    # ── repo discovery ────────────────────────────────────

    def _repo_root(self) -> Path | None:
        """Nearest ancestor of ``kb_dir`` containing a ``.git``, or None."""
        d = self.kb_dir.resolve()
        while True:
            if (d / ".git").exists():
                return d
            if d.parent == d:
                return None
            d = d.parent

    # ── low-level git ─────────────────────────────────────

    def _run(self, repo: Path, *args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("git_kb: git command failed (%s): %s", " ".join(args), e)
            return None

    # ── public API ────────────────────────────────────────

    def commit(self, datasource: str, message: str,
               files: Iterable[str] | None = None, deleted: bool = False,
               lint: Callable[[list[Path]], list[str]] | None = None,
               trailers: dict[str, str] | None = None) -> dict:
        """Stage the datasource's KB YAML files and commit them.

        ``files``: explicit relative filenames (e.g. ``["semantics.yml"]``);
        default = all ``*.yml`` in the datasource dir. ``deleted=True``
        stages the whole datasource dir as removed (``delete_kb``). 

        ``lint``: optional pre-commit gate — called with the staged file
        paths; if it returns any issue, the commit is **refused** and the
        staging is rolled back (files stay modified on disk, just not
        committed). This keeps bad semantics out of the audit history.

        ``trailers``: structured metadata appended as git trailers to the
        commit message (e.g. ``{"Approved-by": "admin", "Generator": "kb"}``)
        so the audit trail carries who approved / what produced the change.

        Best-effort: returns a status dict, never raises. Reasons:
        ``disabled`` / ``no-repo`` / ``nothing-to-commit`` /
        ``lint-failed`` / ``commit-failed``.
        """
        if not self.enabled:
            return {"committed": False, "reason": "disabled"}
        repo = self._repo_root()
        if repo is None:
            return {"committed": False, "reason": "no-repo"}

        # kb_dir 可能是相对路径(如 KbService('.')),relative_to 需要绝对路径。
        ds_dir = (self.kb_dir / datasource).resolve()
        if deleted:
            rel_dir = str(ds_dir.relative_to(repo))
            rels = [rel_dir]
            # 删除整个数据源目录的暂存用 git add -A(空目录 git 不跟踪,无副作用)。
            staged = self._run(repo, "add", "-A", "--", rel_dir)
        elif files is not None:
            selected = [ds_dir / f for f in files if (ds_dir / f).exists()]
            rels = [str(p.relative_to(repo)) for p in selected]
            staged = self._run(repo, "add", "--", *rels) if rels else None
        else:
            selected = sorted(ds_dir.glob(_KB_YML)) if ds_dir.is_dir() else []
            rels = [str(p.relative_to(repo)) for p in selected]
            staged = self._run(repo, "add", "--", *rels) if rels else None
        if not rels or staged is None:
            return {"committed": False, "reason": "nothing-to-commit"}
        if staged.returncode != 0:
            return {"committed": False, "reason": "nothing-to-commit"}

        # 变更门禁:lint 不过 → 拒绝提交并回滚暂存(文件保留在工作区改动)。
        lint_paths = None
        if lint is not None:
            lint_paths = self._lint_paths(ds_dir, files, deleted)

        return self._commit_rels(repo, rels, message, label=datasource,
                                 lint=lint, lint_paths=lint_paths,
                                 trailers=trailers)

    def commit_files(self, files: Iterable[str | Path], message: str,
                     lint: Callable[[list[Path]], list[str]] | None = None,
                     trailers: dict[str, str] | None = None) -> dict:
        """File-scoped auto-commit: stage exactly ``files`` (existing ones).

        The generic twin of :meth:`commit` for asset trees that are not
        datasource directories (org skills). Same guardrails: only the named
        files are staged (never ``git add -A``), an empty diff skips, and
        every failure degrades to a status dict — versioning never blocks
        the write it is recording.
        """
        if not self.enabled:
            return {"committed": False, "reason": "disabled"}
        repo = self._repo_root()
        if repo is None:
            return {"committed": False, "reason": "no-repo"}

        selected = [Path(p).resolve() for p in files]
        selected = [p for p in selected if p.exists() and p.is_file()]
        if not selected:
            return {"committed": False, "reason": "nothing-to-commit"}
        rels = _repos_rel(selected, repo)
        if not rels:
            # 点名的文件全在仓库外 → 这个树不可版本化(与 no-repo 同义)。
            return {"committed": False, "reason": "no-repo"}
        staged = self._run(repo, "add", "--", *rels)
        if staged is None or staged.returncode != 0:
            return {"committed": False, "reason": "nothing-to-commit"}

        return self._commit_rels(repo, rels, message, label="files",
                                 lint=lint, lint_paths=selected,
                                 trailers=trailers)

    def commit_dir_removed(self, path: str | Path, message: str,
                           trailers: dict[str, str] | None = None) -> dict:
        """Stage ``path``'s tracked files as removed and commit (scoped -A).

        Used when an asset directory itself is deleted (skill reject): the
        deletion belongs in the audit history, and a scoped
        ``git add -A -- <path>`` is the only form that stages it without
        touching anything else.
        """
        if not self.enabled:
            return {"committed": False, "reason": "disabled"}
        repo = self._repo_root()
        if repo is None:
            return {"committed": False, "reason": "no-repo"}
        rels = _repos_rel([Path(path).resolve()], repo)
        if not rels:
            return {"committed": False, "reason": "no-repo"}
        rel_dir = rels[0]
        staged = self._run(repo, "add", "-A", "--", rel_dir)
        if staged is None:
            return {"committed": False, "reason": "git-unavailable"}
        if staged.returncode != 0:
            return {"committed": False, "reason": "nothing-to-commit"}
        return self._commit_rels(repo, [rel_dir], message, label="files",
                                 trailers=trailers)

    def _lint_paths(self, ds_dir: Path, files: Iterable[str] | None,
                    deleted: bool) -> list[Path]:
        """本次操作涉及、且确实存在的待 lint 文件(删除场景无内容可 lint)。"""
        if deleted:
            return []
        if files is not None:
            return [ds_dir / f for f in files if (ds_dir / f).exists()]
        return sorted(ds_dir.glob(_KB_YML)) if ds_dir.is_dir() else []

    def _commit_rels(self, repo: Path, rels: list[str], message: str, *,
                     label: str,
                     lint: Callable[[list[Path]], list[str]] | None = None,
                     lint_paths: list[Path] | None = None,
                     trailers: dict[str, str] | None = None) -> dict:
        """Staging 之后的公共尾巴:空 diff 判定 → lint 门禁 → commit 提交。"""
        # 无可提交内容(比如 confirm 前文件已被同步过)→ 空 commit 跳过。
        # 只比较本次点名的路径,避免被用户其他暂存改动误判为"有内容"。
        diff = self._run(repo, "diff", "--cached", "--quiet", "--", *rels)
        if diff is None:
            return {"committed": False, "reason": "git-unavailable"}
        if diff.returncode == 0:
            return {"committed": False, "reason": "nothing-to-commit"}

        # 变更门禁:lint 不过 → 拒绝提交并回滚暂存(文件保留在工作区改动)。
        if lint is not None:
            issues = self._run_lint(lint, lint_paths or [])
            if issues:
                self._run(repo, "reset", "-q", "--", *rels)
                return {"committed": False, "reason": "lint-failed", "issues": issues}

        message = self._with_trailers(message, trailers)

        # 先按 git 自身配置的身份提交(用户有 identity 就用用户的);失败再兜底
        # 机器身份。兜底只在无全局/仓库 identity 时触发,审计历史不会中断。
        result = self._run(repo, "commit", "-m", message, "--", *rels)
        if result is None:
            return {"committed": False, "reason": "git-unavailable"}
        if result.returncode != 0 and self._needs_identity_fallback(result):
            name, email = self._parse_author()
            result = self._run(
                repo, "-c", f"user.name={name}", "-c", f"user.email={email}",
                "commit", "-m", message, "--", *rels,
            )
        if result is None or result.returncode != 0:
            logger.warning(
                "git_kb: commit failed for %s (%s): %s",
                label, message,
                result.stderr.strip() if result is not None
                else result.stdout.strip() if result is not None else "git unavailable",
            )
            return {"committed": False, "reason": "commit-failed"}

        logger.info("git_kb: committed %s (%s)", message, label)
        return {"committed": True, "reason": "ok"}

    def _run_lint(self, lint: Callable[[list[Path]], list[str]],
                  paths: list[Path]) -> list[str]:
        """Run the pre-commit lint callback (working-tree content).

        lint 回调自身崩了按「无问题」降级 —— 版本化是审计旁路,绝不因为
        门禁实现出错而阻断它本要记录的那次写入。
        """
        try:
            return lint(paths)
        except Exception as e:
            logger.warning("git_kb: lint callback failed: %s", e)
            return []

    def _with_trailers(self, message: str, trailers: dict[str, str] | None) -> str:
        """把结构化元数据以 git trailer 追加到 commit message 正文。

        git log --format='%(trailers)' / %(trailers:unfold) 可直接解析,
        `git show -s --format=%B` 原样可见。
        """
        if not trailers:
            return message
        lines = [f"{k}: {v}" for k, v in trailers.items() if k and v]
        if not lines:
            return message
        return f"{message}\n\n" + "\n".join(lines)

    # ── audit history ─────────────────────────────────────

    def history(self, datasource: str, limit: int = 50) -> list[dict]:
        """该数据源 KB 文件的提交历史(git log,按时间倒序)。

        返回 [{sha, subject, author, date, trailers}] ;非 git 环境 → []。
        """
        repo = self._repo_root()
        if repo is None:
            return []
        ds_dir = (self.kb_dir / datasource).resolve()
        if not ds_dir.is_dir():
            return []
        rels = [str(p.relative_to(repo)) for p in sorted(ds_dir.glob(_KB_YML))]
        if not rels:
            return []
        return self._log_entries(repo, rels, limit)

    def history_files(self, paths: Iterable[str | Path], limit: int = 50) -> list[dict]:
        """指定文件/目录的提交历史(与 :meth:`history` 同形)。

        文件级历史是**回滚点的选择面** —— 组织扩展(org skills)没有"数据源"
        这个概念,回滚一个 skill 之前得先看得见它自己的历代提交。目录已被
        删除也能查:``git log -- <path>`` 认路径,不要求路径当前存在。
        """
        repo = self._repo_root()
        if repo is None:
            return []
        rels = _repos_rel([Path(p) for p in paths], repo)
        if not rels:
            return []
        return self._log_entries(repo, rels, limit)

    def _log_entries(self, repo: Path, rels: list[str], limit: int) -> list[dict]:
        fmt = "%H%x00%an%x00%aI%x00%s%x00%(trailers:unfold)"
        result = self._run(
            repo, "log", f"-{max(1, limit)}", f"--format={fmt}",
            "--date=iso-strict", "--", *rels,
        )
        if result is None or result.returncode != 0:
            return []
        entries: list[dict] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x00", 4)
            if len(parts) != 5:
                continue
            sha, author, date, subject, trailers = parts
            entries.append({
                "sha": sha,
                "author": author,
                "date": date,
                "subject": subject,
                "trailers": trailers,
            })
        return entries

    def rollback(self, datasource: str, sha: str, message: str = "kb rollback",
                 trailers: dict[str, str] | None = None) -> dict:
        """把该数据源 KB 文件回滚到指定 commit 的状态(新建提交,不改写历史)。

        ``sha``: 目标 commit(该数据源文件的任一历史版本)。用 ``git restore
        --source <sha> -- <files>`` 恢复工作区+暂存,再提交一条 rollback。
        Best-effort;返回 status dict,绝不抛异常。
        """
        if not self.enabled:
            return {"rolled_back": False, "reason": "disabled"}
        repo = self._repo_root()
        if repo is None:
            return {"rolled_back": False, "reason": "no-repo"}
        verify = self._run(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
        if verify is None or verify.returncode != 0:
            return {"rolled_back": False, "reason": "bad-sha"}
        ds_dir = (self.kb_dir / datasource).resolve()
        if not ds_dir.is_dir():
            return {"rolled_back": False, "reason": "no-datasource-dir"}
        rels = [str(p.relative_to(repo)) for p in sorted(ds_dir.glob(_KB_YML))]
        if not rels:
            return {"rolled_back": False, "reason": "nothing-to-restore"}

        restored = self._run(repo, "restore", "--source", sha, "--", *rels)
        if restored is None or restored.returncode != 0:
            return {"rolled_back": False, "reason": "restore-failed"}
        # restore 只改工作区;stage 后提交。
        staged = self._run(repo, "add", "--", *rels)
        if staged is None:
            return {"rolled_back": False, "reason": "git-unavailable"}
        return self._rollback_commit(repo, rels, sha, datasource, message, trailers)

    def rollback_tree(self, directory: str | Path, sha: str,
                      message: str = "asset rollback",
                      transform: Callable[[Path], None] | None = None,
                      trailers: dict[str, str] | None = None) -> dict:
        """把 ``directory`` 整棵树回滚到 ``sha`` 时的状态(新建提交,不改写历史)。

        与 :meth:`rollback` 的区别:**目标文件集来自 ``git ls-tree``**(目标
        commit 里该目录下真实存在的文件),而不是当前工作区 —— 后来才新增的
        **已跟踪**文件被删掉、当时存在后来被删的文件被恢复,才算真正"回到
        那个版本"。删除与恢复都只在 ``<directory>`` 作用域内暂存,目录之外
        的工作区改动一律不碰。未跟踪文件不删也不提交(从未进过版本史的内容,
        回滚既无权销毁也无权收编)。

        ``transform``:恢复之后、暂存之前对目录做的最后一次改写 —— org
        skills 用它把 ``version`` 抬到"当前版本 + 1"(回滚是**一次新的修订**
        而不是时间倒流,修订计数必须继续往前走)。回调抛异常则整体放弃,
        返回 ``transform-failed``(工作区已恢复,但不会产生半成品提交)。
        """
        if not self.enabled:
            return {"rolled_back": False, "reason": "disabled"}
        repo = self._repo_root()
        if repo is None:
            return {"rolled_back": False, "reason": "no-repo"}
        verify = self._run(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
        if verify is None or verify.returncode != 0:
            return {"rolled_back": False, "reason": "bad-sha"}
        rels = _repos_rel([Path(directory).resolve()], repo)
        if not rels:
            return {"rolled_back": False, "reason": "no-repo"}
        rel_dir = rels[0]
        listing = self._run(repo, "ls-tree", "-r", "--name-only", sha, "--", rel_dir)
        if listing is None:
            return {"rolled_back": False, "reason": "git-unavailable"}
        targets = [ln for ln in listing.stdout.splitlines() if ln.strip()]
        if not targets:
            return {"rolled_back": False, "reason": "nothing-to-restore"}

        restored = self._run(repo, "restore", "--source", sha, "--", *targets)
        if restored is None or restored.returncode != 0:
            return {"rolled_back": False, "reason": "restore-failed"}
        # restore 只按点名路径恢复,不会动目标版本里不存在的文件 —— 不删
        # 它们,"回到那一版"只回滚了一半。只删**已被跟踪**的:未跟踪文件
        # 从未进过版本史,删掉就再也拿不回来,不是回滚能承担的事(它同样
        # 不该被卷进这条提交,所以下面按 targets/extras 分别暂存,不再用
        # 目录作用域的 add -A)。
        head = self._run(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", rel_dir)
        tracked = (head.stdout.splitlines()
                   if head is not None and head.returncode == 0 else [])
        extras = [f for f in tracked if f.strip() and f not in targets]
        for rel_file in extras:
            try:
                (repo / rel_file).unlink()
            except OSError:
                pass
        if transform is not None:
            try:
                transform(Path(directory).resolve())
            except Exception as e:
                logger.warning("git_kb: rollback transform failed (%s): %s", rel_dir, e)
                return {"rolled_back": False, "reason": "transform-failed"}
        # 暂存:恢复(可能被 transform 改写)的目标文件 + 额外文件的删除。
        staged = self._run(repo, "add", "--", *targets)
        if staged is None or staged.returncode != 0:
            return {"rolled_back": False, "reason": "git-unavailable"}
        if extras:
            unstaged = self._run(repo, "add", "-A", "--", *extras)
            if unstaged is None:
                return {"rolled_back": False, "reason": "git-unavailable"}
        return self._rollback_commit(repo, [rel_dir], sha, rel_dir, message, trailers)

    def _rollback_commit(self, repo: Path, rels: list[str], sha: str,
                         label: str, message: str,
                         trailers: dict[str, str] | None) -> dict:
        """回滚的公共尾巴:提交恢复内容,把 ``_commit_rels`` 的 reason 映射回
        rollback 词汇(空 diff = nothing-to-restore)。"""
        res = self._commit_rels(repo, rels, message, label=f"rollback {label}",
                                trailers=trailers)
        if res.get("committed"):
            logger.info("git_kb: rolled back %s to %s", label, sha)
            return {"rolled_back": True, "reason": "ok"}
        reason = res.get("reason", "commit-failed")
        if reason == "nothing-to-commit":
            reason = "nothing-to-restore"
        return {"rolled_back": False, "reason": reason}

    def _needs_identity_fallback(self, result: subprocess.CompletedProcess) -> bool:
        """git 报「没有 identity」时才需要兜底身份。"""
        msg = (result.stderr or "") + (result.stdout or "")
        return "user.name" in msg or "user.email" in msg or "identity" in msg

    def _parse_author(self) -> tuple[str, str]:
        author = (self.author or _DEFAULT_AUTHOR).strip()
        if "<" in author and author.endswith(">"):
            name = author.split("<", 1)[0].strip()
            email = author.split("<", 1)[1][:-1].strip()
            if name and email:
                return name, email
        return "trove", "trove@local"


# 向后兼容别名:既有导入点(trove/services/kb/service.py 与测试)继续用旧名字。
GitKb = GitVersioning
