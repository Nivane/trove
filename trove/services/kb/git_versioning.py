"""Best-effort git versioning of KB YAML source files (semantics-as-code).

Every KB write (``/kb init``, learn, draft approve/reject/auto-apply,
lesson confirm, delete) auto-commits the affected datasource's YAML so
``git log`` is the change audit history — diff, blame and rollback come
free from git itself. The industry pattern is *config-as-code*: the
semantic model lives in the repo, not in a database.

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
from typing import Iterable

logger = logging.getLogger(__name__)

#: 自动 commit 的作者兜底。git 全局/仓库身份缺失时 commit 会失败,这里给一个
#: 可辨识的机器身份而不是让审计历史断掉(可用 TROVE_GIT_KB_AUTHOR 覆盖,
#: 格式 "Name <email>")。
_DEFAULT_AUTHOR = "trove <trove@local>"

_KB_YML = "*.yml"


class GitKb:
    """Auto-commit KB YAML changes to the enclosing git repository.

    Resolves the git work tree root by walking up from ``kb_dir`` (the
    KB may live anywhere, e.g. ``<repo>/.trove/kb`` or an external
    ``--kb-dir``). Commits only the datasource's ``*.yml`` files.
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
               files: Iterable[str] | None = None, deleted: bool = False) -> dict:
        """Stage the datasource's KB YAML files and commit them.

        ``files``: explicit relative filenames (e.g. ``["semantics.yml"]``);
        default = all ``*.yml`` in the datasource dir. ``deleted=True``
        stages the whole datasource dir as removed (``delete_kb``). 
        Best-effort: returns a status dict, never raises. Reasons:
        ``disabled`` / ``no-repo`` / ``nothing-to-commit`` /
        ``commit-failed``.
        """
        if not self.enabled:
            return {"committed": False, "reason": "disabled"}
        repo = self._repo_root()
        if repo is None:
            return {"committed": False, "reason": "no-repo"}

        ds_dir = self.kb_dir / datasource
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

        # 无可提交内容(比如 confirm 前文件已被同步过)→ 空 commit 跳过。
        # 只比较本数据源文件,避免被用户其他暂存改动误判为"有内容"。
        diff = self._run(repo, "diff", "--cached", "--quiet", "--", *rels)
        if diff is None:
            return {"committed": False, "reason": "git-unavailable"}
        if diff.returncode == 0:
            return {"committed": False, "reason": "nothing-to-commit"}

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
                datasource, message,
                result.stderr.strip() if result is not None
                else result.stdout.strip() if result is not None else "git unavailable",
            )
            return {"committed": False, "reason": "commit-failed"}

        logger.info("git_kb: committed %s (%s)", message, datasource)
        return {"committed": True, "reason": "ok"}

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
