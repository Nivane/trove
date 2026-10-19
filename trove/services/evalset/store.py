"""``evalset.yml`` 的原子存储 —— 读路径容错,写路径全或无。

一个数据源恰好一份资产:``<root>/.trove/evalsets/<datasource>/evalset.yml``。
目录名就是数据源身份,只接受 ``[A-Za-z0-9][A-Za-z0-9._-]*`` —— 数据源名会
被拼进路径,不校验就等于把路径穿越写进 API。

写:先写同目录 ``evalset.yml.tmp``,``os.replace`` 原子换名。读者永远看不到
半份文件;进程在写一半时死掉,``evalset.yml`` 仍是上一个完整版本。

读:文件缺失**不是错误**(``exists=False``);内容读不出/解析抛错降级为
``evalset.unreadable`` issue + 空文档 + ``exists=True`` —— 「读不到」与
「不存在」是两种事实,后者可能只是还没建过验收集。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from trove.services.evalset.models import (
    EvalsetDoc,
    EvalsetError,
    EvalsetIssue,
    dump_evalset,
    now_iso,
    parse_evalset,
)

#: 数据源名 = 路径段:必须字母数字开头,其余只允许 ``.``/``_``/``-``。
_SAFE_DATASOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: 资产文件名(单数:一个数据源一份)。
_EVALSET_FILE = "evalset.yml"


def _safe_datasource(datasource: str) -> str:
    """校验数据源名可以安全地作为一个路径段;不合法 → ``EvalsetError``。"""
    if not isinstance(datasource, str) or not _SAFE_DATASOURCE.fullmatch(datasource):
        raise EvalsetError(
            "evalset.invalid_datasource",
            f"invalid datasource name {datasource!r}",
        )
    return datasource


def evalset_dir(root: Path, datasource: str) -> Path:
    """``<root>/.trove/evalsets/<datasource>``。"""
    return Path(root) / ".trove" / "evalsets" / _safe_datasource(datasource)


def evalset_path(root: Path, datasource: str) -> Path:
    """``<root>/.trove/evalsets/<datasource>/evalset.yml``。"""
    return evalset_dir(root, datasource) / _EVALSET_FILE


def load_evalset(
    root: Path, datasource: str,
) -> tuple[EvalsetDoc, list[EvalsetIssue], bool]:
    """容错读取资产;第三项 ``exists`` 区分「空资产」与「还没有资产」。

    解析出的 issues 原样返回(调用方决定:列表照常展示,写/运行按 issue
    拒绝启动——见设计 §4.3)。文件存在但读不出 = 一条 ``unreadable`` issue,
    绝不把它洗成空文档(空文档会让下一次保存静默覆盖坏文件)。
    """
    path = evalset_path(root, datasource)
    if not path.is_file():
        return EvalsetDoc(datasource=datasource), [], False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc, issues = parse_evalset(data, datasource=datasource)
    except Exception as exc:  # noqa: BLE001 — 坏文件任何一种失败都是 unreadable
        return (
            EvalsetDoc(datasource=datasource),
            [EvalsetIssue(str(path), "evalset.unreadable", str(exc))],
            True,
        )
    return doc, issues, True


def save_evalset(root: Path, doc: EvalsetDoc) -> Path:
    """原子写入资产并盖章 ``updated_at``;返回写入路径。

    先写同目录临时文件再 ``os.replace``:同一文件系统内的 replace 是原子的,
    并发读者要么看到旧版本、要么看到新版本。``updated_at`` 每次保存现算,
    是**文档级**时间戳(条目级的 ``created_at``/``confirmed_at`` 不在此处动)。
    """
    path = evalset_path(root, doc.datasource)
    doc.updated_at = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(dump_evalset(doc), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        # 半写的临时文件不留在资产目录里(下一次保存会覆盖,但它不是资产)。
        tmp.unlink(missing_ok=True)
        raise
    return path


__all__ = [
    "evalset_dir",
    "evalset_path",
    "load_evalset",
    "save_evalset",
]
