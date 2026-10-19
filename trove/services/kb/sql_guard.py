"""SQL 护栏:SQLGlot 语法 + 试执行(`LIMIT 1`,10s 超时)。

从 synthetic.py 提取,供 evalset 生成(以及后续语义枚举)复用:只有
"能解析且能在目标库上跑起来"的 SQL 才算可用。试执行是**只读**的
(`SELECT * FROM (<sql>) t LIMIT 1` 包裹,复用调用方连接器,不新建连接),
任何异常/超时一律 False,绝不向上抛。
"""

from __future__ import annotations

import asyncio
from typing import Any

TRIAL_TIMEOUT_S = 10       # 单条试执行超时(慢查询静默丢弃)


def sqlglot_ok(sql: str) -> bool:
    """SQLGlot 语法校验:解析失败(缺括号/错 token)直接丢弃。"""
    if not sql.strip():
        return False
    try:
        import sqlglot
        sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
        return True
    except Exception:
        return False


async def trial_ok(registry: Any, datasource: str | None, sql: str) -> bool:
    """试执行护栏:`SELECT * FROM (<sql>) t LIMIT 1` 只取 1 行。

    表名/列名错误、方言不兼容、超时都会让这条被丢弃——只有真能在
    目标库跑起来的 SQL 才可用。
    """
    wrapped = f"SELECT * FROM ({sql}) t LIMIT 1"
    try:
        await asyncio.wait_for(
            registry.execute(wrapped, datasource), timeout=TRIAL_TIMEOUT_S,
        )
        return True
    except Exception:
        return False
