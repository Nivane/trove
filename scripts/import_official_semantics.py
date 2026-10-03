#!/usr/bin/env python3
"""官方数据库文档 → semantics.yml 确定性导入(零 LLM)。

现有通道只把官方 database_description 送进 `schema_notes.yml`
(`scripts/import_bird_descriptions.py`);而编译器/规划器实际读的是
`semantics.yml` 的语义模型 —— **官方文档到语义模型没有通道**,字段描述
全由 LLM 起草,实测 financial 系统性错位:

  - `district.A11`:"standard deviation of salaries in the district"
    vs 官方 "average salary";
  - `district.district_id`:"unique identifier for a district"
    vs 官方 "location of branch";
  - `loan.status`:enum_display 是 LLM 编的 {C: in collection, D:
    defaulted} vs 官方 C = running contract, OK so far / D = running
    contract, client in debt。

本脚本把官方文档**确定性**地灌进语义模型的**两个键**(白名单,落盘前
有结构 diff 断言,越界即退出码 1):

  1. ``datasets[].fields[].description`` ← 官方 column_description(csv
     里非空才覆盖;空串保留现有描述);
  2. ``datasets[].fields[].enum_display`` ← 官方 value_description 的值对
     (合并语义:现有 code **保序保留**、官方覆盖其 label、官方新增 code
     追加)。官方只列值不给释义的行(解析出的 label 回退成 code 本身)
     不覆盖 —— 那会把 "owner" 之类写回成 "OWNER",是净损失。

**明确不写 `schema_notes.yml`**:那份文件的列描述是占比度量命名
(cdesc 插进 metric 名)与 retrieval 别名的锚,动了会重命名一批生成器
度量、churn 检索别名;schema_notes 的官方通道是
`scripts/import_bird_descriptions.py`,两段各管各的。

防呆(沿用 regen_kb_generated 的约定):
- 重写前先验 YAML 往返 byte-identical,不保真则拒绝落盘;
- 默认 dry-run 打印 per-field diff;`--check` 有漂移退出码 1;幂等 ——
  第二次运行零 diff;
- CSV stem 对不上任何 dataset、或 docs 目录空 → 退出码 2(「查不成的绿
  是假绿」)。

用法:
    uv run python scripts/import_official_semantics.py \
        --kb-dir .trove/kb/financial \
        --docs ~/hub/trove-design/fin_database_description     # dry-run
    ... --write    # 落盘(打印变更摘要)
    ... --check    # 有漂移退出码 1(不落盘)
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from trove.services.kb.deterministic_gen import _enum_values
from trove.services.kb.lint import parse_enum_values

_DUMP_KWARGS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)

#: 官方 CSV 的列名(实测表头)。account.csv 表头多一个尾逗号(第 6 列无名),
#: 且 frequency 行的值语义就落在那一列 —— 见 _row_value_text。
_COL_NAME = "original_column_name"
_COL_DESC = "column_description"
_COL_FORMAT = "data_format"
_COL_VALUES = "value_description"
_DECLARED_COLUMNS = frozenset((_COL_NAME, "column_name", _COL_DESC,
                               _COL_FORMAT, _COL_VALUES))

#: 值语义只认分类列:官方 data_format == text。数值/日期列的
#: value_description 是单位或格式注记("unit：US dollar"、"in the form
#: YYMMDD"、"not useful"),不是取值词表 —— 全角冒号会把它解析成
#: ('unit', 'US dollar') 这类垃圾对。
_TEXT_FORMATS = frozenset(("text",))

#: 官方单列最多 7 个值(trans.k_symbol);生成器默认 limit 是占比面的 4,
#: 这里要给足,否则官方值表被腰斩。
_ENUM_LIMIT = 50

#: 结构 diff 白名单:只允许这两条路径变化(enum_display 记到叶子键)。
_ALLOWED_CHANGE_RE = re.compile(
    r"semantic_model\[\d+\]\.datasets\[\d+\]\.fields\[\d+\]"
    r"\.(?:description|enum_display(?:\..+)?)")
#: changed_paths 给新增/删除/长度变化附加的后缀(判白名单前剥掉)。
_CHANGE_KIND_RE = re.compile(r" \((?:新增|删除|长度 [^)]*)\)$")


def _allowed_change(path: str) -> bool:
    return bool(_ALLOWED_CHANGE_RE.fullmatch(_CHANGE_KIND_RE.sub("", path)))


# ── 官方文档读取 ──────────────────────────────────────────


def _row_value_text(row: dict[str, Any]) -> str:
    """行的值语义文本:value_description ∪ 未声明列里的非空内容。

    实测坑:account.csv 的 frequency 行比表头多一个逗号,值语义
    (``"POPLATEK MESICNE" stands for monthly issuance`` …)落在未命名的
    第 6 列而 ``value_description`` 是空串 —— 只读列名会把该行的值词表
    整条丢掉。未声明列(``''`` / DictReader 的 ``None``)里的非空内容按行
    拼进结果。
    """
    parts: list[str] = []
    head = str(row.get(_COL_VALUES) or "").strip()
    if head:
        parts.append(head)
    for key, val in row.items():
        if key is None:  # 比表头更宽的行:多余单元挂 None
            parts.extend(str(v).strip() for v in (val or []) if str(v).strip())
        elif key not in _DECLARED_COLUMNS and str(val or "").strip():
            parts.append(str(val).strip())
    return "\n".join(parts)


def read_official_rows(csv_path: Path) -> dict[str, dict[str, str]]:
    """单个官方 CSV → {original_column_name: {description, format, values}}。"""
    out: dict[str, dict[str, str]] = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = str(row.get(_COL_NAME) or "").strip()
            if not name:
                continue
            out[name] = {
                "description": str(row.get(_COL_DESC) or "").strip(),
                "format": str(row.get(_COL_FORMAT) or "").strip().lower(),
                "values": _row_value_text(row),
            }
    return out


def read_official_docs(docs_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    """官方文档目录 → {dataset(CSV stem): {字段: 行}}。"""
    docs: dict[str, dict[str, dict[str, str]]] = {}
    for csv_path in sorted(docs_dir.glob("*.csv")):
        docs[csv_path.stem] = read_official_rows(csv_path)
    return docs


# ── 值语义解析(复用既有解析器,不新写第三份)─────────────


def _plausible_bare_value(value: str) -> bool:
    """裸值行的兜底守卫:只认短词/码 —— lint 的解析器没有叙事过滤,
    整句("each bank has unique two-letter code")与叙事尾巴
    ("commonsense evidence:")都会被它当成一个值。"""
    v = str(value or "").strip()
    return (
        bool(v)
        and ":" not in v
        and len(v) <= 24
        and len(v.split()) <= 3
    )


#: label 里至少要有一个字母/数字/汉字才算「有释义」。
_LABEL_HAS_CONTENT_RE = re.compile(r"[0-9A-Za-z一-鿿]")


def _with_content_labels(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """丢掉标点残渣对。

    ``_enum_values`` 的引号分支剥掉引号段后取剩余文本当 label:disp.type 的
    ``"OWNER" : "USER" : "DISPONENT"``(官方只列值、不给释义)经剥离后
    label 只剩 ``':'`` —— 写进 enum_display 就是 garbage。label 无任何
    字母/数字/汉字者的整对丢弃(该列官方没给释义,不应覆盖现有 label)。
    """
    return [
        (code, label) for code, label in pairs
        if _LABEL_HAS_CONTENT_RE.search(str(label or ""))
    ]


def _normalize_entries(text: str) -> str:
    """条目切分归一:``;`` 与换行都当条目边界,统一成逐行。

    官方文本以换行分隔条目,但 ``'A' stands for X; 'B' stands for Y`` 同处
    一行的形态也真实存在 —— 不拆的话 ``_enum_values`` 的引号分支会把第二条
    整段吞进第一条的 label(静默 garbage,连报错都没有)。按 ``;`` 切也正好
    与 :func:`lint.parse_enum_values` 的 ``[;\\n]`` 口径一致:同一份官方
    文档,取值域检查与实际导入切出同一套 code。
    """
    return "\n".join(
        part
        for line in str(text or "").splitlines()
        for part in line.split(";")
    )


def official_enum_pairs(text: str) -> list[tuple[str, str]]:
    """官方 value_description → [(code, label)](确定性,零 LLM)。

    主解析器 = 生成器的 :func:`deterministic_gen._enum_values`(覆盖实测
    官方四形态:``'A' stands for ...`` / ``"junior": ...`` /
    ``F：female``(全角冒号,与 ``=`` 同义) / 裸值行),label 归一复用其
    ``_enum_label``;它空手时退回 :func:`lint.parse_enum_values`(纯值行
    列表按 ``;``/换行切,label 回退 code)—— 同一份官方文档,lint 的取值域
    检查与实际导入必须切出同一套 code。叙述行(含冒号短语/多词句)一律
    跳过,不产 garbage。
    """
    text = str(text or "")
    pairs = _with_content_labels(
        _enum_values([_normalize_entries(text)], limit=_ENUM_LIMIT))
    if pairs:
        return pairs
    return [
        (v, v) for v in sorted(parse_enum_values(text))
        if _plausible_bare_value(v)
    ]


def _label_is_informative(code: str, label: str) -> bool:
    """官方是否真给了释义(而不是只列了一个值)。

    ``_enum_values`` 解析不出 label 时回退 code(disp.type 的
    ``"OWNER" : "USER" : "DISPONENT"`` 只列值不给义)。用 code 覆盖现有
    label(``owner`` → ``OWNER``)是净损失,跳过。
    """
    lab = str(label or "").strip()
    return bool(lab) and lab.lower() != str(code or "").strip().lower()


def merge_enum_display(
    existing: dict[str, str] | None, pairs: list[tuple[str, str]],
) -> tuple[dict[str, str], list[str], list[str]]:
    """合并官方值对 → (合并后的 enum_display, 变更说明, 跳过说明)。

    现有 code 保序保留(dict 插入序):官方覆盖其 label、官方新增 code
    追加。分歧只在「覆盖」一侧:官方对**已有** code 没给释义(label 回退
    成 code)时跳过 —— 用 ``OWNER`` 覆盖 ``owner`` 是净损失;新增 code 无
    释义照样追加(值本身是真的,label 列回退成 code)。
    """
    merged: dict[str, str] = {str(k): str(v) for k, v in (existing or {}).items()}
    changes: list[str] = []
    skips: list[str] = []
    for code, label in pairs:
        code = str(code or "").strip()
        if not code:
            continue
        label = str(label or "").strip() or code
        if code in merged:
            if not _label_is_informative(code, label):
                skips.append(f"{code}: 官方仅列值,保留原 label {merged[code]!r}")
                continue
            if merged[code] != label:
                changes.append(f"{code}: {merged[code]!r} → {label!r}")
            merged[code] = label
        else:
            merged[code] = label
            changes.append(f"+ {code}: {label!r}")
    return merged, changes, skips


#: enum_display 新键的插入位置:落在这些键之前(与盘上既有字段的键序一致)。
_FIELD_TAIL_KEYS = ("ai_context", "description", "values", "label", "examples",
                    "custom_extensions", "dimension")


def _set_enum_display(field: dict[str, Any], merged: dict[str, str]) -> None:
    """就地写 enum_display;键不存在时插在字段约定位置(不是追加到末尾)。"""
    if not merged:
        return
    if "enum_display" in field:
        field["enum_display"] = merged
        return
    rebuilt: dict[str, Any] = {}
    placed = False
    for key, val in field.items():
        if not placed and key in _FIELD_TAIL_KEYS:
            rebuilt["enum_display"] = merged
            placed = True
        rebuilt[key] = val
    if not placed:
        rebuilt["enum_display"] = merged
    field.clear()
    field.update(rebuilt)


# ── 套用 + 结构 diff ─────────────────────────────────────


def apply_official(
    doc: dict[str, Any], official: dict[str, dict[str, dict[str, str]]],
) -> dict[str, Any]:
    """就地套用官方文档 → 变更记录(供 CLI 渲染与测试断言)。

    只碰 ``datasets[].fields[]`` 的 ``description`` / ``enum_display``;
    dataset 名 = CSV stem。CSV 里对不上的字段、非 text 列的值语义、
    「只列值不给义」的官方对都记进 notes(不静默丢)。
    """
    lines: list[str] = []
    notes: list[str] = []
    changed_fields = 0
    matched_fields = 0
    matched_datasets: list[str] = []

    model = (doc.get("semantic_model") or [{}])[0]
    for ds in model.get("datasets") or []:
        ds_name = str(ds.get("name") or "")
        rows = official.get(ds_name)
        if not rows:
            continue
        matched_datasets.append(ds_name)
        seen: set[str] = set()
        for field in ds.get("fields") or []:
            fname = str(field.get("name") or "")
            row = rows.get(fname)
            if row is None:
                continue
            seen.add(fname)
            matched_fields += 1
            where = f"{ds_name}.{fname}"
            touched = False

            desc = str(row.get("description") or "").strip()
            if desc and str(field.get("description") or "") != desc:
                lines.append(
                    f"  ~ {where}  description: {field.get('description')!r} → {desc!r}")
                field["description"] = desc
                touched = True

            if str(row.get("format") or "") in _TEXT_FORMATS:
                pairs = official_enum_pairs(str(row.get("values") or ""))
                if pairs:
                    merged, enum_changes, enum_skips = merge_enum_display(
                        field.get("enum_display"), pairs)
                    if enum_changes:
                        lines.append(f"  ~ {where}  enum_display:")
                        lines.extend(f"      {n}" for n in enum_changes)
                        _set_enum_display(field, merged)
                        touched = True
                    for skip in enum_skips:
                        notes.append(f"{where}: {skip}")
            elif str(row.get("values") or "").strip():
                notes.append(f"{where}: 非 text 列(data_format="
                             f"{row.get('format')!r}),值文本按注记跳过")
            if touched:
                changed_fields += 1
        unmatched = sorted(set(rows) - seen)
        if unmatched:
            notes.append(f"{ds_name}: 官方文档有而 KB 无的字段 {unmatched}")

    missing_ds = sorted(set(official) - set(matched_datasets))
    if missing_ds:
        notes.append(f"官方文档有而 KB 无的 dataset {missing_ds}")
    return {
        "lines": lines, "notes": notes, "changed_fields": changed_fields,
        "matched_fields": matched_fields, "matched_datasets": matched_datasets,
    }


def changed_paths(before: Any, after: Any, path: str = "") -> list[str]:
    """YAML 文档结构 diff → 变更叶子路径列表(递归比对,纯函数)。"""
    out: list[str] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in list(before) + [k for k in after if k not in before]:
            child = f"{path}.{key}" if path else str(key)
            if key not in before:
                out.append(f"{child} (新增)")
            elif key not in after:
                out.append(f"{child} (删除)")
            else:
                out += changed_paths(before[key], after[key], child)
    elif isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            out.append(f"{path} (长度 {len(before)}→{len(after)})")
        for i, (x, y) in enumerate(zip(before, after)):
            out += changed_paths(x, y, f"{path}[{i}]")
    elif before != after:
        out.append(path)
    return out


# ── CLI ──────────────────────────────────────────────────


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kb-dir", default=".trove/kb/financial",
                    help="KB 目录(默认 financial;不要对 mysql_fin 跑 --write)")
    ap.add_argument("--docs", required=True,
                    help="官方 database_description 目录(每表一个 CSV,stem=dataset 名)")
    ap.add_argument("--write", action="store_true", help="落盘(默认只 dry-run)")
    ap.add_argument("--check", action="store_true", help="有漂移时退出码 1(不落盘)")
    args = ap.parse_args()

    kb = Path(args.kb_dir)
    sem_path = kb / "semantics.yml"
    if not sem_path.exists():
        print(f"error: 缺 {sem_path}", file=sys.stderr)
        return 2
    docs_dir = Path(args.docs).expanduser()
    if not docs_dir.is_dir():
        print(f"error: 官方文档目录不存在: {docs_dir}", file=sys.stderr)
        return 2
    official = read_official_docs(docs_dir)
    if not official:
        print(f"error: {docs_dir} 下没有 CSV", file=sys.stderr)
        return 2

    raw = sem_path.read_text(encoding="utf-8")
    before = _load_yaml(sem_path)
    if yaml.safe_dump(before, **_DUMP_KWARGS) != raw:
        print(f"error: {sem_path} YAML 往返非 byte-identical,拒绝重写", file=sys.stderr)
        return 2

    doc = yaml.safe_load(raw) or {}
    result = apply_official(doc, official)
    if not result["matched_datasets"]:
        print(f"error: 官方文档的 dataset(stem {sorted(official)})在该 KB 里"
              f"一个都对不上 —— 拒绝在无匹配的 KB 上落盘", file=sys.stderr)
        return 2

    print(f"[official] dataset 命中 {result['matched_datasets']} / "
          f"字段命中 {result['matched_fields']} / 变更字段 {result['changed_fields']}")
    for line in result["lines"]:
        print(line)
    for note in result["notes"]:
        print(f"  (skip) {note}")

    paths = changed_paths(before, doc)
    bad = [p for p in paths if not _allowed_change(p)]
    if bad:
        print("error: 变更越出白名单(只允许 datasets[].fields[].description / "
              f"enum_display):{bad}", file=sys.stderr)
        return 1
    if not paths:
        print("已在同步状态,无改动")
        return 0
    if not args.write:
        print(f"(dry-run:{len(paths)} 处变更;加 --write 落盘)")
        return 1 if args.check else 0
    sem_path.write_text(yaml.safe_dump(doc, **_DUMP_KWARGS), encoding="utf-8")
    print(f"已写入 {sem_path}({len(paths)} 处变更)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
