"""Result limits plumbing — sync bridge from AgentConfig to pipeline nodes.

The admin-managed settings live in the DB-backed settings system
(``trove/services/settings``) and hot-apply onto the shared ``AgentConfig``.
Workflow nodes (execute_sql / output) are plain closures without a config
handle, so the current limits are mirrored into this module-level registry:
set at boot (after overrides) and re-set on every admin settings update.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_ROWS = 1000
DEFAULT_DISPLAY_ROWS = 50


@dataclass(frozen=True)
class ResultLimits:
    max_rows: int      # 查询结果行数上限(下载/透出)
    display_rows: int  # 答案 markdown 表格单次展示行数


_limits = ResultLimits(max_rows=DEFAULT_MAX_ROWS, display_rows=DEFAULT_DISPLAY_ROWS)


def set_result_limits(max_rows: int, display_rows: int) -> None:
    global _limits
    _limits = ResultLimits(max_rows=max(int(max_rows) if max_rows else DEFAULT_MAX_ROWS, 1),
                           display_rows=max(int(display_rows) if display_rows else DEFAULT_DISPLAY_ROWS, 1))


def get_result_limits() -> ResultLimits:
    return _limits


def reset_result_limits() -> None:
    """恢复默认(测试隔离)."""
    set_result_limits(DEFAULT_MAX_ROWS, DEFAULT_DISPLAY_ROWS)