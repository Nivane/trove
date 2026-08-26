"""Prompt-injection 内容隔离 — 工具返回的外部内容在回喂模型前做扫描与隔离。

对应"Prompt Injection / Sandbox"(T2 安全)的内容隔离 + 权限最小化:
数据库单元格/检索值是**外部不可信内容**,可能携带恶意指令(如 "ignore
previous instructions...")。扫描命中即把该值替换成中性标记——模型只看到
"这是数据",无法把它当指令执行。隔离是主动防御(命中即失效),命中计数
仅作可观测性(注入本身可能被绕过,所以隔离才是主防线)。

只扫"外部内容"(probe/search 返回的单元值);LLM 自生成或 KB 管理端确认
的内容不在此列。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

ISOLATED_MARKER = "[data: content isolated]"

# (pattern 名, 编译正则)——双语短语级匹配(多字符上下文,避免误伤业务数据)。
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 英文:指令覆写
    ("ignore_previous", re.compile(
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|above|prior|earlier)\b",
        re.I,
    )),
    ("disregard_prior", re.compile(
        r"\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|above|prior|earlier)\b",
        re.I,
    )),
    ("forget_instructions", re.compile(
        r"\bforget\s+(?:your|all|the)\s+(?:instructions?|rules?|guidelines?|prompt)\b",
        re.I,
    )),
    ("role_switch", re.compile(
        r"\b(?:you\s+are\s+now|now\s+you\s+are|act\s+as|pretend\s+to\s+be|"
        r"from\s+now\s+on\s+you\b)\b",
        re.I,
    )),
    ("system_prompt", re.compile(
        r"\bsystem\s+prompt\b|\bprompt\s+injection\b|\bjailbreak\b",
        re.I,
    )),
    # 中文:指令覆写
    ("zh_ignore", re.compile(
        r"(?:忽略|无视|不要理会|别管|不理会|不用遵守|不需要遵循|不需要遵守)"
        r"(?:\s*(?:前面|之前|上面|以上|所有|一切|我|你|的))*"
        r"\s*(?:指令|指示|规则|提示|要求|内容)",
    )),
    ("zh_override", re.compile(
        r"(?:你现在是|你现在的(?:角色|任务|身份)|扮演|假扮|作为系统|"
        r"系统提示词|越狱|绕过|覆盖系统|隐藏指令)",
    )),
]


def scan_injection(text: Any) -> list[str]:
    """扫描文本,返回命中的注入模式名列表(空 = 干净)。"""
    s = str(text or "")
    if not s:
        return []
    return [name for name, rx in _PATTERNS if rx.search(s)]


def isolate_cells(values: Iterable[Any]) -> tuple[list[str], int]:
    """批量隔离:命中注入模式的单元值替换为中性标记。

    Returns:
        (隔离后值列表, 命中数)。命中计数用于工具载荷的可观测性字段。
    """
    out: list[str] = []
    flagged = 0
    for v in values:
        s = str(v)
        if scan_injection(s):
            out.append(ISOLATED_MARKER)
            flagged += 1
        else:
            out.append(s)
    return out, flagged
