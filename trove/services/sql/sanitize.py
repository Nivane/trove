"""错误信息脱敏 — 防止连接串/密码经 SQL 报错路径泄露给 LLM 或日志。

应用位置:probe/check/search/explain 工具的错误折叠路径与执行器错误
消息。LLM 通过报错信息学习修复,但报错可能携带 DSN/密码——统一在此
剥除,不依赖各数据库驱动的自我克制。
"""

from __future__ import annotations

import re

# URL 凭据:scheme://user:pass@host — 密码贪婪吞到最后一个 @(密码可含 @)
_URL_CRED_RE = re.compile(r"([a-zA-Z][\w+.-]*://[^/@\s]*:)[^@]*@")
# 键值形式:password=xxx / pwd=xxx / passwd=xxx(值到空白或分号结束)
_KWARG_SECRET_RE = re.compile(r"(?i)(password|passwd|pwd)(\s*=\s*)[^\s;]+")


def sanitize_error_text(text: str | None) -> str:
    """剥除文本中的连接凭据;无匹配时原样返回。"""
    if not text:
        return ""
    out = _URL_CRED_RE.sub(r"\1***@", text)
    out = _KWARG_SECRET_RE.sub(r"\1\2***", out)
    return out
