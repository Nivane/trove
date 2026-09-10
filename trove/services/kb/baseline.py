"""生成基线 ``.generated/`` —— 三方合并的"祖先"(Phase C2)。

``.trove/kb/<datasource>/.generated/<file>`` 存**生成方上一次的输出**。

**为什么必须是"生成方的输出"而不是"盘上那份文件"**:基线的唯一职责是回答
"这条是人改的还是生成方写的"。把盘上的文件当基线,等于宣布"这里每一个字都是
生成方写的" —— 下一轮生成就会名正言顺地覆盖掉人的编辑,而这正是本设计要
阻止的事。因此基线只在**完整重新生成**时写(``_init_file`` / ``_init_doc``),
增量写入器(``draft_example`` / ``append_term`` / ``confirm_*``)不写:它们的
输出里混着人的既有编辑,当不了祖先。

**为什么是点目录**:读循环按 ``*.yml`` 取文件,``.generated`` 是目录,天然
不参与(与 ``rules.yml``、``_meta`` 一起,构成"读路径只认白名单"的第二层)。
文件名与资产同名,是为了让人一眼能对上,``.generated/examples.yml`` 就是
``examples.yml`` 的祖先。

**基线坏了怎么办**:当作没有基线。没有基线就拒绝合并(见 ``KbService`` 的
写路径)—— 绝不退化成"猜一个祖先"。
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from trove.core.logging import get_logger
from trove.services.kb.provenance import META_KEY

logger = get_logger(__name__)

#: 基线目录(与资产同级的点目录)。
BASELINE_DIRNAME = ".generated"

#: 覆盖写之前的备份目录(``.generated/backup/<时间戳>/``)。
BACKUP_DIRNAME = "backup"

#: 保留的备份代数。合并按构造不会丢人的编辑,备份是防"合并本身写错"的保险。
KEEP_BACKUPS = 5


def baseline_path(asset_path: Path) -> Path:
    return asset_path.parent / BASELINE_DIRNAME / asset_path.name


def read_baseline(asset_path: Path) -> dict[str, Any] | None:
    """读祖先;没有/读不了 → ``None``(= 不能自动合并)。"""
    path = baseline_path(asset_path)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        logger.warning("Baseline %s is unreadable (%s); treating as absent", path, e)
        return None
    if not isinstance(doc, Mapping):
        return None
    return {k: v for k, v in doc.items() if k != META_KEY}


def write_baseline(asset_path: Path, doc: Mapping[str, Any]) -> None:
    """把**生成方的输出**留一份祖先。

    不写 ``_meta``:来源块描述的是盘上那份文件(谁写的、什么时候、有没有被
    改过),而这里是它的祖先副本 —— 给副本盖章只会造出一个"看起来也是生成
    物"的混淆源。
    """
    path = baseline_path(asset_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in doc.items() if k != META_KEY}
    path.write_text(
        yaml.safe_dump(body, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def backup_asset(asset_path: Path, stamp: str | None = None) -> Path | None:
    """覆盖写之前留一份原文件。返回备份路径(源文件不在 → ``None``)。

    只保留最近 :data:`KEEP_BACKUPS` 代:每次重新生成都会覆盖三个文件,不设上限
    的话 ``.generated/`` 会随时间无限长。
    """
    if not asset_path.exists():
        return None
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dest_dir = asset_path.parent / BASELINE_DIRNAME / BACKUP_DIRNAME / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / asset_path.name
    shutil.copy2(asset_path, dest)
    _prune_backups(asset_path.parent / BASELINE_DIRNAME / BACKUP_DIRNAME)
    return dest


def _prune_backups(backup_root: Path) -> None:
    try:
        generations = sorted(
            (p for p in backup_root.iterdir() if p.is_dir()), key=lambda p: p.name,
        )
    except OSError:
        return
    for stale in generations[:-KEEP_BACKUPS]:
        shutil.rmtree(stale, ignore_errors=True)
