"""体检输入解析 —— 粘贴文本与 CSV 上传(纯解析,不落库)。

企业手里的「真实问题」通常是一张导出表或一段聊天记录。本模块只做一件事:
把它们变成**去重、去空、有界**的问题列表,并把「我们丢掉了什么」如实报出来
(编码回退、表头猜错、重复、超长、截断)—— 体检测的是模型覆盖,不是用户
数据的整洁度;一次静默丢行会让覆盖率分母悄悄变小,那是最不该有的失真。

三条边界:

- **不猜列语义**:自动识别不到表头就取第一列并在 warnings 里说清楚;
  显式指定列找不到就是硬错误(``question_column_not_found``)。
- **上限是产品的一部分**:1 MiB / 扫描 2000 行 / 保留 200 题。截断必须
  ``truncated=True`` + warning,绝不静默。
- 不读网络、不碰存储。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

MAX_CSV_BYTES = 1_048_576          # 1 MiB
MAX_ROWS_SCANNED = 2000            # 扫描上限(含被跳过的行)
MAX_QUESTIONS = 200                # 保留上限
MAX_QUESTION_CHARS = 2000          # 单题字符上限(解析期按行拒绝)
MAX_ISSUES = 20                    # issues 条数上限

# 表头自动识别:大小写无关、去空白后整格相等才算。
_HEADER_NAMES = frozenset({
    "question", "questions", "question_text", "query",
    "问题", "问题文本", "用户问题",
})


class CsvInputError(ValueError):
    """解析期的硬错误(携带稳定错误码,API 直接映射)。

    ``code`` 是契约的一部分(前端按码给文案):
    ``file_too_large | empty_file | invalid_csv | question_column_not_found``。
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass
class ParsedQuestions:
    """解析结果:问题列表 + 如实的数据质量报告。"""

    questions: list[str] = field(default_factory=list)
    source_label: str = ""
    header: str = ""
    encoding: str = "text"
    rows_seen: int = 0
    duplicates: int = 0
    skipped_blank: int = 0
    skipped_long: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    issues_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "questions": list(self.questions),
            "question_count": len(self.questions),
            "source_label": self.source_label,
            "header": self.header,
            "encoding": self.encoding,
            "rows_seen": self.rows_seen,
            "duplicates": self.duplicates,
            "skipped_blank": self.skipped_blank,
            "skipped_long": self.skipped_long,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "issues": list(self.issues),
            "issues_truncated": self.issues_truncated,
        }


def normalize_question(text: Any) -> str:
    """折叠所有空白(含全角空格)为单空格;非字符串按空串处理。"""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def dedupe_key(question: str) -> str:
    """去重键:折叠空白 + casefold(「How many loans」与「how many  loans」同题)。"""
    return " ".join(str(question).split()).casefold()


def _issue(result: ParsedQuestions, row: int, reason: str, **extra: Any) -> None:
    if len(result.issues) >= MAX_ISSUES:
        result.issues_truncated = True
        return
    result.issues.append({"row": row, "reason": reason, **extra})


def _add_question(
    result: ParsedQuestions,
    seen: set[str],
    text: str,
    row_number: int,
) -> bool:
    """归一化后加入;返回是否**因保留上限**而停止扫描(调用方 break)。"""
    question = normalize_question(text)
    if not question:
        result.skipped_blank += 1
        return False
    if len(question) > MAX_QUESTION_CHARS:
        result.skipped_long += 1
        _issue(result, row_number, "question_too_long", length=len(question))
        return False
    key = dedupe_key(question)
    if key in seen:
        result.duplicates += 1
        return False
    if len(result.questions) >= MAX_QUESTIONS:
        result.truncated = True
        if "question_cap" not in result.warnings:
            result.warnings.append("question_cap")
        return True
    seen.add(key)
    result.questions.append(question)
    return False


def _decode(data: bytes) -> tuple[str, str, bool]:
    """字节 → 文本;返回 ``(text, encoding_label, replaced)``。

    utf-8-sig 先试(BOM 是 Windows 导出的常态);失败回退 GB18030(中文
    Excel 的默认导出);再失败用替换解码**并标记** —— 一个坏字节不该让
    整张表不可用,但用户必须知道有些字符已经被替换掉。
    """
    try:
        return data.decode("utf-8-sig"), "utf-8-sig", False
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gb18030"), "gb18030", False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8-replace", True


def _resolve_column(
    first_row: list[str],
    question_column: str,
) -> tuple[int, str, bool, str]:
    """决定问题列与表头。

    Returns:
        ``(index, header_label, skip_first_row, warning)``
    """
    cells = [str(c or "").strip() for c in first_row]
    folded = [c.casefold() for c in cells]

    explicit = str(question_column or "").strip()
    if explicit:
        if explicit.isdigit():
            index = int(explicit) - 1
            if index < 0:
                raise CsvInputError(
                    "question_column_not_found", f"column {explicit!r}")
            # 数字列号是 1 起用户口径。首行像是表头(任一格是已知表头名)
            # 就按表头跳过 —— 用户数是按「打开文件看到的列」数的。
            has_header = any(cell in _HEADER_NAMES for cell in folded)
            header = cells[index] if has_header and index < len(cells) else ""
            return index, header, has_header, ""
        want = explicit.casefold()
        for i, cell in enumerate(folded):
            if cell == want:
                return i, cells[i], True, ""
        raise CsvInputError(
            "question_column_not_found", f"column {explicit!r}")

    for i, cell in enumerate(folded):
        if cell in _HEADER_NAMES:
            return i, cells[i], True, ""
    # 没有可识别表头:取第一列,首行按数据处理,并如实警告。
    return 0, "", False, "header_not_found"


def parse_csv(
    data: bytes,
    *,
    question_column: str = "",
    source_label: str = "csv",
) -> ParsedQuestions:
    """CSV 字节 → ParsedQuestions;结构性错误抛 ``CsvInputError``。"""
    if len(data) > MAX_CSV_BYTES:
        raise CsvInputError("file_too_large", f"{len(data)} bytes")
    if not data or not data.strip():
        raise CsvInputError("empty_file", "empty upload")

    text, encoding, replaced = _decode(data)
    result = ParsedQuestions(source_label=source_label, encoding=encoding)
    if replaced:
        result.warnings.append("encoding_replaced")

    if "\x00" in text:
        # 文本 CSV 里出现 NUL 只可能是导出/拼接坏掉;按行继续会产出
        # 看不出来的脏问题,不如整文件拒绝。
        raise CsvInputError("invalid_csv", "NUL byte in input")
    try:
        # strict=True:引号未闭合等畸形结构**报错**而不是静默把后续行
        # 并进同一个字段(那会把多道题变成一道超长题)。
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise CsvInputError("invalid_csv", str(exc)[:200]) from exc
    if not rows:
        raise CsvInputError("empty_file", "no rows")

    index, header, skip_first, warning = _resolve_column(rows[0], question_column)
    result.header = header
    if warning:
        result.warnings.append(warning)

    seen: set[str] = set()
    start = 1 if skip_first else 0
    for offset, row in enumerate(rows[start:]):
        if result.rows_seen >= MAX_ROWS_SCANNED:
            result.truncated = True
            if "scan_capped" not in result.warnings:
                result.warnings.append("scan_capped")
            break
        result.rows_seen += 1
        cell = row[index] if index < len(row) else ""
        if _add_question(result, seen, cell, row_number=offset + start + 1):
            break
    return result


def parse_pasted(
    text: str,
    *,
    source_label: str = "paste",
) -> ParsedQuestions:
    """粘贴文本(一行一题)→ ParsedQuestions。

    空行跳过;行内多余空白折叠。与 CSV 共用同一套去重/上限/issues 语义
    —— 两条入口给出两种口径,用户会先怀疑体检而不是输入。
    """
    result = ParsedQuestions(source_label=source_label, encoding="text")
    seen: set[str] = set()
    for number, line in enumerate(str(text or "").splitlines(), start=1):
        if result.rows_seen >= MAX_ROWS_SCANNED:
            result.truncated = True
            if "scan_capped" not in result.warnings:
                result.warnings.append("scan_capped")
            break
        if not line.strip():
            result.skipped_blank += 1
            continue
        result.rows_seen += 1
        if _add_question(result, seen, line, row_number=number):
            break
    return result


__all__ = [
    "CsvInputError",
    "MAX_CSV_BYTES",
    "MAX_QUESTIONS",
    "MAX_QUESTION_CHARS",
    "MAX_ROWS_SCANNED",
    "ParsedQuestions",
    "dedupe_key",
    "normalize_question",
    "parse_csv",
    "parse_pasted",
]
