"""KB 资产的内容格式版本与来源块(Phase C1)。

YAML 资产是**人可编辑**的真源:它会被复制、进 git、被各种工具重写。格式
一改,磁盘上的旧文件不会自己消失 —— 所以每份生成物都要能回答两个问题:

- **我是哪一版生成的?** → ``format``(内容格式版本)+ ``generator``;
- **生成之后有人动过我吗?** → ``digest``(生成时正文的摘要)。

三个字段各有明确用途,缺一不可:

- ``format`` 是**版本门**的输入。与 :data:`FORMAT_VERSION` 比对,不看
  trove 发行号 —— 一次发行可能改三次格式,也可能一次不改,两者不是一回事。
  比自己新的格式**拒绝**(:func:`check_format`):按旧解释读一份新格式的
  文件,产出的不是"少读了几个字段",而是一个**错的语义模型**,而错的语义
  模型会进编译器、进权威 SQL、进答案。
- ``digest`` 是三方合并(C2)判断"这条是人的编辑还是生成方的更新"的锚。
  摘要覆盖**正文**(不含 `_meta` 自身),所以每次 Trove 重写 `_meta`
  (时间戳每次都新)都不会把自己判成"被人改过"。
- ``generator`` / ``trove`` / ``generated_at`` 是归因与诊断。"这份文件是
  谁在什么时候生成的"是运维第一个要问的问题,而改造前只能靠 ``ls -l``。

**无 `_meta` 的文件是 format 0**:存量库(今天所有已存在的 KB)全部落在这
一侧,按当前解释读 —— 一律拒绝会让升级即全部停摆。删掉 `_meta` 只会降级到
最保守的一侧,不会伪装成新格式。

`_meta` 放在**文件内**而不是旁挂文件(``.meta.json``):资产是可复制、可进
git、可导出的 YAML,来源块跟内容一起走;旁挂文件一复制就丢,而"丢了
provenance"必须退化成一个安全的默认值 —— 上面那条。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import yaml

#: 来源块的顶层键。
META_KEY = "_meta"

#: 当前内容格式版本。改**读法**(而非改生成器)时加一,并在 C2 的迁移表里
#: 登记一条从这里到新版本的合并规则。
FORMAT_VERSION = 1

#: 无 `_meta` 的老文件。语义 = "按当前解释读",不是"另一个老格式"。
FORMAT_LEGACY = 0


@dataclass(frozen=True)
class AssetMeta:
    """一份资产的来源信息。缺省值 = format 0(存量文件)。"""

    format: int = FORMAT_LEGACY
    generator: str = ""
    trove: str = ""
    digest: str = ""
    generated_at: str = ""


def body_digest(doc: Mapping[str, Any]) -> str:
    """正文(不含 ``_meta``)的内容摘要。

    规范化 ::``json.dumps(sort_keys=True, default=str)`` —— 键序、缩进、
    引号风格都不参与(文件被一个会重排 YAML 的编辑器存过一次,不该被当成
    "人改过")。``default=str`` 是必需的:``yaml.safe_load`` 会把裸日期
    字面量还原成 ``date``/``datetime`` 对象,而写入时手里可能是字符串 ——
    不归一的话,人没动过的文件也会判成"改过"。
    """
    body = {k: v for k, v in doc.items() if k != META_KEY}
    blob = json.dumps(
        body, sort_keys=True, ensure_ascii=False, default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _trove_version() -> str:
    try:
        from trove import __version__

        return __version__
    except Exception:  # pragma: no cover - 只可能因打包方式异常
        return ""


def make_meta(doc: Mapping[str, Any], generator: str) -> dict[str, Any]:
    """为**这份正文**构造 `_meta` 块。"""
    return {
        "format": FORMAT_VERSION,
        "generator": generator,
        "trove": _trove_version(),
        "digest": body_digest(doc),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def stamp(doc: Mapping[str, Any], generator: str) -> dict[str, Any]:
    """正文 + `_meta`(不修改入参)。`_meta` 排在最后:内容在前,来源在后。"""
    return {**doc, META_KEY: make_meta(doc, generator)}


def dump_asset(doc: Mapping[str, Any], generator: str) -> str:
    """写盘用的 YAML 文本(已带 `_meta`)。"""
    return yaml.safe_dump(
        stamp(doc, generator),
        default_flow_style=False, allow_unicode=True, sort_keys=False,
    )


def read_meta(doc: Any) -> AssetMeta:
    """从已解析的 YAML 文档读来源块。缺省/损坏 → :data:`FORMAT_LEGACY`。

    损坏的 `_meta` 按最保守的一侧处理(当老文件读),而不是拒绝:它的正文
    此刻就在手上,拒绝等于因为一个附属块丢掉整份资产。**但可解析的数字
    字符串要读**(``"2"`` → 2):那是一个真实的版本号,当成损坏就等于把
    "更新的格式"误判成"老文件"—— 正是这道门要拦的情况。
    """
    raw = doc.get(META_KEY) if isinstance(doc, Mapping) else None
    if not isinstance(raw, Mapping):
        return AssetMeta()
    return AssetMeta(
        format=_read_format(raw.get("format")),
        generator=str(raw.get("generator") or ""),
        trove=str(raw.get("trove") or ""),
        digest=str(raw.get("digest") or ""),
        generated_at=str(raw.get("generated_at") or ""),
    )


def _read_format(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return FORMAT_LEGACY
    try:
        return int(value)
    except (TypeError, ValueError):
        return FORMAT_LEGACY


def body_edited(doc: Any) -> bool | None:
    """生成之后正文是否被人改过。``None`` = 无从判断(没有摘要)。

    ``None`` 与 ``False`` 必须分开:存量文件没有 `_meta`,不能当成"没改过"
    —— 三方合并(C2)对"不知道"要给出不同处置(不猜,让人拍板)。
    """
    digest = read_meta(doc).digest
    if not digest:
        return None
    return digest != body_digest(doc)


def check_format(meta: AssetMeta) -> str | None:
    """版本门:``None`` = 可读;否则是拒绝原因。

    只有"比自己新"才拒绝。老文件是**能读的**(按当前解释读),迁移是人
    显式触发的事(:func:`needs_migration` 负责报告)。
    """
    if meta.format > FORMAT_VERSION:
        return (
            f"asset format {meta.format} is newer than this Trove understands "
            f"(format {FORMAT_VERSION}) — upgrade Trove instead of reading it "
            f"with older rules"
        )
    return None


def needs_migration(meta: AssetMeta) -> bool:
    """是否需要走格式迁移(老于当前,且当前还能读)。"""
    return FORMAT_LEGACY <= meta.format < FORMAT_VERSION
