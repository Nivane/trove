"""Estimate vs actual token calibration (per model + dialect).

The context budget counts with the real tokenizer (count_tokens), but
the tokenizer is only an approximation of a provider's actual
tokenization — per-model BPE, message-formatting overhead, tool
definitions, per-provider punctuation rules all shift the real count.
This module records the measured ratio ``actual_input / estimated`` for
each (model, dialect) and exposes a factor that scales budget estimates
— a feedback loop that tightens the budget as a systematic undercount
is observed, instead of silently blowing the real context window.

Process-level global (mirrors token_accounting / tracing.local);
conftest resets it so tests start unconfigured.
"""

from __future__ import annotations

# EMA 平滑系数:新观测占比 0.3(快速适应,但不受单次抖动支配)
_ALPHA = 0.3
_stats: dict[tuple[str, str], float] = {}


def record(model: str, dialect: str, estimated: int, actual: int) -> None:
    """记录一次估算 vs 实测比例(actual/estimated)到 (model, dialect)。

    任一非正则跳过(无有效信号)。
    """
    if estimated <= 0 or actual <= 0:
        return
    ratio = actual / estimated
    key = (model or "", dialect or "")
    prev = _stats.get(key)
    _stats[key] = ratio if prev is None else _ALPHA * ratio + (1 - _ALPHA) * prev


def factor(model: str, dialect: str) -> float:
    """校准因子:>1 = 实测普遍高于估算(成本放大/预算收紧)。

    冷启动无观测 → 1.0(不干预)。
    """
    return _stats.get((model or "", dialect or ""), 1.0)


def reset() -> None:
    """清空校准表(测试隔离)。"""
    _stats.clear()
