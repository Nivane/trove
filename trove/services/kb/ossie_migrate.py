"""v0.1 ``semantic_model`` 包装 → OSSIE 0.2 扁平根(纯转换,零 IO/LLM)。

读端对旧包装是**硬拒绝** —— 迁移因此必须有一个确定性、可审查的实现,
而不是人工手改(手改会把注释、键序、缩进这些"没有语义但影响 diff"
的东西一起改掉,而 KB 文件是 git 版本化的人工资产)。

转换是**行式**的:

- 删掉根级 ``semantic_model:`` 行,把单模型块整体去缩进一级;
- 根级 ``version``(通常在文件末尾)移到文件最顶并加引号;
- 注释、键序、缩进风格保持不变;
- 只接受**单模型**;空包装 / 多模型 / 非映射 / 坏 YAML / 更新版本一律拒绝。

转换后做两道回环:

1. **YAML 数据等价** —— ``safe_load(new)`` 必须等于"旧模型 + 目标版本"
   (语义上没有丢任何键);
2. **解析回环** —— ``parse_ossie(new)`` 必须成功,且数据集/指标/关系/
   主题/层级的名字集合与旧模型一致(没有"YAML 等价但解析器读成另一份
   模型"的缝隙)。

本模块不读盘、不写盘;落盘与退出码在 ``scripts/migrate_semantics_v02.py``。
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from trove.services.semantic_layer.ossie import (
    OSSIE_VERSION,
    OssieFormatError,
    parse_ossie,
    stamp_ossie_version,
)

_LEGACY_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)semantic_model:\s*(?:#.*)?$")
_ROOT_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][\w.-]*):(?:\s|$)")
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)-\s")
_ITEM_CONTENT_RE = re.compile(
    r"^(?P<indent>[ \t]*)-(?P<gap>[ \t]*)(?P<rest>.*)$")


class MigrationError(ValueError):
    """这份文件不能由确定性转换器安全迁移(原因可直接展示给用户)。"""


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _is_item(line: str) -> bool:
    return bool(_LIST_ITEM_RE.match(line))


def _root_key(line: str) -> str | None:
    if _is_item(line):
        return None
    m = _ROOT_KEY_RE.match(line)
    return m.group("key") if m else None


def _model_names(model) -> dict[str, set[str]]:
    return {
        "datasets": {d.name for d in model.datasets},
        "metrics": {m.name for m in model.metrics},
        "relationships": {r.name for r in model.relationships},
        "topics": {t.name for t in model.topics},
        "hierarchies": {h.name for h in model.hierarchies},
    }


def _expected_names(entry: dict[str, Any]) -> dict[str, set[str]]:
    def names(key: str, name_key: str = "name") -> set[str]:
        values = entry.get(key) or []
        if not isinstance(values, list):
            return set()
        return {
            str(v.get(name_key) or "")
            for v in values
            if isinstance(v, dict) and str(v.get(name_key) or "")
        }

    return {
        "datasets": names("datasets"),
        "metrics": names("metrics"),
        "relationships": names("relationships"),
        "topics": names("topics"),
        "hierarchies": names("hierarchies"),
    }


def _quote_version_line(target: str) -> str:
    return f'version: "{target}"\n'


def _convert_legacy(text: str, target_version: str) -> tuple[str, bool]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise MigrationError("OSSIE 文档根必须是映射（mapping）")
    models = data.get("semantic_model")
    if not isinstance(models, list) or not models:
        raise MigrationError("semantic_model 为空,没有可迁移的模型")
    if len(models) > 1:
        raise MigrationError(
            f"semantic_model 有 {len(models)} 个模型;迁移器只处理单模型文件")
    entry = models[0]
    if not isinstance(entry, dict):
        raise MigrationError("semantic_model 条目必须是映射（mapping）")
    unknown = sorted(set(data) - {"semantic_model", "version"})
    if unknown:
        raise MigrationError(
            f"旧包装在根上有未知键 {unknown};迁移器不猜它们的归属")

    declared = data.get("version", entry.get("version"))
    payload = {k: v for k, v in entry.items() if k != "version"}
    if declared is not None:
        payload = {"version": declared, **payload}
    try:
        expected = stamp_ossie_version(payload)
    except OssieFormatError as e:
        raise MigrationError(str(e)) from e
    # 目标版本由调用方钉住(默认当前 OSSIE_VERSION);更新版本在这里已拒绝。
    expected["version"] = target_version

    lines = _lines(text)
    legacy_idx = next(
        (i for i, line in enumerate(lines) if _LEGACY_LINE_RE.match(line)), None,
    )
    if legacy_idx is None:
        raise MigrationError("找不到根级 semantic_model: 行")

    # 块 = semantic_model 行之后,直到下一个根级键(缩进版的 sequence 也一样:
    # 根级键的缩进一定小于列表项,或与 indentless 列表项同列但不是 "- ")。
    block_start = legacy_idx + 1
    block_end = len(lines)
    for i in range(block_start, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _is_item(line):
            continue
        key = _root_key(line)
        if key is not None:
            block_end = i
            break

    block = lines[block_start:block_end]
    item_idx: int | None = None
    content_prefix = ""
    item_rest = ""
    item_newline = "\n"
    for i, line in enumerate(block):
        stripped = line.rstrip("\r\n")
        m = _ITEM_CONTENT_RE.match(stripped)
        if m is None:
            continue
        item_idx = i
        # 内容列 = 列表项缩进 + "-" + 其后的空白(通常是 2 列)。
        content_prefix = m.group("indent") + " " * (1 + len(m.group("gap")))
        item_rest = m.group("rest")
        item_newline = line[len(stripped):] or "\n"
        break
    if item_idx is None:
        raise MigrationError("semantic_model 下没有列表项(空包装)")

    # 去掉条目内层的 version 行(如果它写在模型条目里)。
    entry_version_prefix = content_prefix + "version:"
    dedented: list[str] = []
    for i, line in enumerate(block):
        if i == item_idx:
            # 第一项:`- name: demo` → `name: demo`。
            if item_rest:
                dedented.append(item_rest + item_newline)
            continue
        stripped = line.rstrip("\r\n")
        if (entry.get("version") is not None
                and stripped.startswith(entry_version_prefix)):
            continue
        if line.startswith(content_prefix):
            dedented.append(line[len(content_prefix):])
        else:
            dedented.append(line)

    prefix: list[str] = []
    for line in lines[:legacy_idx]:
        if _root_key(line) == "version":
            continue
        prefix.append(line)
    trailing: list[str] = []
    for line in lines[block_end:]:
        if _root_key(line) == "version":
            continue
        trailing.append(line)

    new_text = (
        _quote_version_line(target_version)
        + "".join(prefix)
        + "".join(dedented)
        + "".join(trailing)
    )

    parsed = yaml.safe_load(new_text) or {}
    if parsed != expected:
        raise MigrationError(
            "迁移后 YAML 数据与旧模型不等价(转换器拒绝写出半份模型)")
    try:
        model = parse_ossie(new_text)
    except Exception as e:  # 解析回环失败 → 转换有问题,拒绝
        raise MigrationError(f"迁移后 parse_ossie 失败: {e}") from e
    if _model_names(model) != _expected_names(entry):
        raise MigrationError(
            "迁移后解析出的模型条目与旧文档不一致(转换器拒绝写出半份模型)")
    return new_text, new_text != text


def _convert_flat(text: str, target_version: str) -> tuple[str, bool]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise MigrationError("OSSIE 文档根必须是映射（mapping）")
    if "version" not in data:
        # 新文档/存量扁平文档缺版本:在最顶补一行,数据等价由 safe_load 校验。
        new_text = _quote_version_line(target_version) + text
        expected = stamp_ossie_version(data)
        expected["version"] = target_version
        if (yaml.safe_load(new_text) or {}) != expected:
            raise MigrationError("补 version 后 YAML 数据不等价")
        return new_text, True
    try:
        # 已有版本:只接受当前解释能读的文档(旧扁平版可收养,新版本拒绝)。
        if yaml.safe_load(text) != stamp_ossie_version(data):
            return text, False  # 无需迁移(版本已是目标,形状不变)
    except OssieFormatError as e:
        raise MigrationError(str(e)) from e
    return text, False


def convert_text(text: str, target_version: str = OSSIE_VERSION) -> tuple[str, bool]:
    """旧包装/缺版本的 ``semantics.yml`` → 扁平 0.2 文本。

    Returns:
        ``(new_text, changed)``。已合规的扁平文档原样返回 ``changed=False``。

    Raises:
        MigrationError: 不能安全转换(多模型/空包装/坏 YAML/版本问题)。
    """
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise MigrationError("OSSIE 文档根必须是映射（mapping）")
    if "semantic_model" in data:
        return _convert_legacy(text, target_version)
    return _convert_flat(text, target_version)
