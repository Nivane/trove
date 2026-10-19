"""评测口径纯函数——结果集比较、问题对账键、生成错误分类。

单一来源:``scripts/eval_bird.py`` 与 ``trove/eval/gate.py`` 曾各持一份
副本;副本漂移会让“同一道题”在录制与对账两侧变成两个身份。这里只做
纯函数搬运,行为逐字节不变(``tests/eval/test_text.py`` 钉住)。
"""
from __future__ import annotations

import re
from typing import Any


def normalize_rows(rows: list[list]) -> list[tuple]:
    """Set-comparison: sorted, stringified rows (column order-insensitive)."""
    return sorted(tuple(str(v) for v in row) for row in rows)


def norm_question(value: Any) -> str:
    """配对键:小写 + 去标点 + 空白压缩(``[\\W_]+`` 把下划线也折成空格)。"""
    text = re.sub(r"[\W_]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def classify_pred_error(error: str) -> str:
    """final.error → GENERATION_ERROR | EXECUTION_ERROR(与汇总口径一致)。"""
    return "GENERATION_ERROR" if error and "generation" in error.lower() else "EXECUTION_ERROR"
