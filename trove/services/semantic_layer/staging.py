"""语义变更隔离区 —— ``.trove/kb/<ds>/.staging/<change_id>/``（设计 §5.1）。

只放**必须与 semantics.yml 同构**的东西（沙箱直接拿快照当模型加载）与可
重算缓存，整体不进 git（根 .gitignore 的 ``.trove/kb/*/.staging/`` 条目）。

I3：隔离区绝不参与编译与检索 —— 消费面只有 ChangeService 与沙箱；检索/
编译读的是 ``KbService.semantics_path`` 单文件白名单。半损（目录在、文件
缺）如实返回 ``None``：「读不到快照」与「快照是一份空文档」是两件事。
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_STAGING = ".staging"


class StagingArea:
    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = Path(kb_dir)

    def root(self, change_id: str) -> Path:
        # change_id 由 ChangeService 生成（chg-YYYYMMDD-<hex>），仍是外部
        # 输入（URL 路径）—— 在这里拒绝路径分隔符，够了。
        if "/" in change_id or "\\" in change_id or change_id in ("", ".", ".."):
            raise ValueError(f"非法变更 id: {change_id!r}")
        return self._kb_dir / _STAGING / change_id

    def stage(self, change_id: str, *, base_text: str, after_text: str) -> Path:
        d = self.root(change_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "base.semantics.yml").write_text(base_text, encoding="utf-8")
        (d / "after.semantics.yml").write_text(after_text, encoding="utf-8")
        return d

    def read_base(self, change_id: str) -> str | None:
        return self._read(change_id, "base.semantics.yml")

    def read_after(self, change_id: str) -> str | None:
        return self._read(change_id, "after.semantics.yml")

    def _read(self, change_id: str, name: str) -> str | None:
        path = self.root(change_id) / name
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def write_verification(self, change_id: str, payload: dict) -> None:
        d = self.root(change_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "verification.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")

    def read_verification(self, change_id: str) -> dict | None:
        path = self.root(change_id) / "verification.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("verification.json unreadable for %s", change_id)
            return None
        return data if isinstance(data, dict) else None

    def prune(self, retain_days: int, *, now: datetime | None = None) -> int:
        """清理超过保留期的快照目录；``<= 0`` 不清理。返回清理数。"""
        if retain_days <= 0:
            return 0
        base = self._kb_dir / _STAGING
        if not base.is_dir():
            return 0
        cutoff = (now or datetime.now(timezone.utc)).timestamp() - retain_days * 86400
        removed = 0
        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                if child.stat().st_mtime < cutoff:
                    shutil.rmtree(child)
                    removed += 1
            except OSError as e:  # 清理是增强,失败只记不抛
                logger.debug("staging prune skipped %s: %s", child, e)
        return removed
