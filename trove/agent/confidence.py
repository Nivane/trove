"""答案级置信度披露(设计 2026-09-30)。

**决策一:只披露,不改行为。** 这两个分数不进任何条件分支 —— 判据是开/关两跑
``sql``/``rows``/``row_count``/``verdict``/``answer_source`` 逐字节相同(I7)。

**判定只有这一份。** 与 ``agent/answer_source.py`` 同构:两个分数都在 ``output``
一处算,SSE / 前端 / 评测 / markdown 同源同上文。分两处判迟早出现「行说 82%、
字段说 55%」——那比不披露更坏。

**开关为什么是进程级模块变量。** ``output`` 是模块级普通函数(不是
``make_xxx(...)`` 工厂),拿不到 ``AgentConfig``。仓库对这件事的既有答案是
``services/limits.py`` 的进程级镜像(它的 docstring 写明了理由),本模块沿用同一
范式:启动时与 admin 热更新时各同步一次。
"""

from __future__ import annotations

_enabled = True


def set_confidence_enabled(flag: bool) -> None:
    global _enabled
    _enabled = bool(flag)


def confidence_enabled() -> bool:
    return _enabled


def reset_confidence_flag() -> None:
    """恢复默认(测试隔离)。"""
    global _enabled
    _enabled = True
