"""草稿校验 + lint 结构化 —— 语义工作台 P2 地基的纯函数层(零 IO、零 LLM)。

两个消费者共用这一份映射(方案「后端端点」①/②):

- ``POST /v1/admin/semantic/{ds}/validate`` —— 草稿干跑校验;
- ``GET  /v1/admin/semantic/{ds}`` detail 的 ``issues`` / ``issue_items``。

``lint_semantics_document`` 返回扁平中文串(git pre-commit 门禁要能直接打印);
管理端要的是**可分类、可定位到实体、可给修复提示**的结构 ——
:func:`structured_issues` 做这一层翻译。

**三件事各自的位置**(契约边界):

- ``code`` 是**稳定契约**:前端按 code 分流 UI 组件,不随措辞漂移;
- ``message`` 是 lint 原文的**忠实转录**(可能随 lint 措辞演进);
- ``hint`` 是一句话修复建议,不承诺与 message 逐字对应。

**不匹配绝不丢弃**:未命中规则表的串落 ``code="lint"`` 兜底。宁可少分类,
不可漏报 —— 漏报的后果是页面显示「校验通过」而 confirm 被 400 拒绝。

``severity`` 的取值是 ``error`` / ``warning``,但**今天的写盘门禁不分级**:
``manage._reject_bad_semantics`` 在 issues 非空时即拒绝写入,所以 warning
同样会拦 confirm。因此 :func:`validate_draft` 的 ``ok`` 取「能不能过门禁」
语义(errors / warnings 任一非空即 False),而不是「有没有 error」——
让校验结果与 confirm 的实际行为一致,是这一层存在的唯一理由。
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from sqlglot import ErrorLevel, exp, parse_one

from trove.services.kb.lint import lint_semantics_document

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

_TARGET_UNKNOWN: tuple[str, str] = ("unknown", "")

# ── code → 一句话修复提示 ──────────────────────────────────

_HINTS: dict[str, str] = {
    "dup_metric": "同名指标只保留一条:确认哪条是权威口径后改名或合并",
    "dup_field": "同名字段只保留一条:改名或合并",
    "expr_parse": "检查括号/函数名/方言;表达式须是单条 SQL 标量或聚合表达式",
    "expr_missing": "补上 expression(如 AVG(students.grade))",
    "dataset_undeclared": "修正数据集名,或先在 datasets 里显式声明该数据集",
    "dataset_unknown": "先声明该数据集,或修正 dataset.field 的 dataset 前缀",
    "field_target_invalid": "字段目标写成 dataset.field 形式",
    "datatype_invalid": "datatype 取提示中候选集里的类型名",
    "custom_extensions": "custom_extensions 每项须带 vendor_name",
    "filter_parse": "检查 filter 表达式语法",
    "filter_ref": "filter 只引用本数据集已声明的字段",
    "enum_baked": "枚举值过滤写进 metric 会把口径锁死:改在查询期过滤,或拆独立指标",
    "agg_time_dimension": "agg_time_dimension 指向已声明的 is_time 时间字段",
    "non_additive": "non_additive 口径不应被其它指标按加法引用",
    "bad_synonym": "同义词不能为空;与其它实体重复的同义词会让检索互相抢命中",
    "mask_invalid": "mask 取 none | partial | hash | null;裸 null 会被 YAML 解析成不脱敏",
    "mask_hash_without_salt": "hash 需要模型级 masking.hash_salt_ref(无 salt 可被彩虹表还原)",
    "masking_misplaced": "masking 写在 semantic_model[0] 下(与 time_spine 同层),顶层会被忽略",
    "row_filter": "row_filter 只能引用本表已声明的字段",
    "unique_keys": "unique_keys 须为列名数组,且列已在 fields 中声明",
    "relationship_undeclared_dataset": "关系的 from/to 必须是已声明的数据集",
    "relationship_missing_cardinality": "声明 1:N / N:1 / M:N —— fan-out 保护依赖它",
    "relationship_fan_out": "M:N 会拒绝 fan-out:改 1:N + 中间表,或 fan_out=dedup 显式豁免",
    "path_ambiguity": "两表间多条简单路径会让编译器拒绝二义:收敛建模(仅声明必要边)",
    "fk_relationship_missing": "按命名约定指向已声明数据集时,补声明对应 relationship",
    "dup_topic": "同名主题域只保留一条:确认哪条是权威范围后改名或合并",
    "topic_empty_scope": "声明它覆盖的 dataset(空边界会把域内所有问题收敛成零锚定拒绝),或删除该主题域",
    "topic_undeclared_dataset": "修正数据集名,或先在 datasets 里声明它(主题域只能收敛到已声明的数据集)",
    "topic_undeclared_metric": "先声明该指标,或从主题域里移除它",
    "topic_metric_outside": "把该指标锚定的数据集并入本主题,或从本主题移除该指标",
    "topic_invalid": "topics 须为映射数组,每项至少含 name 与非空 datasets",
    "kind_unknown": "kind 必须为 metric / field / dataset / topic 之一",
    "lint": "见原文;这条问题尚未归类,请按 lint 描述修正",
}


def _metric_target(m: re.Match[str]) -> tuple[str, str]:
    return ("metric", m.group("name"))


def _field_target(m: re.Match[str]) -> tuple[str, str]:
    # 消息里的 ``表 A.B`` 目标(数据集名/字段名不含空格)。
    return ("field", m.group("target"))


def _dataset_target(m: re.Match[str]) -> tuple[str, str]:
    return ("dataset", m.group("name"))


def _relationship_target(m: re.Match[str]) -> tuple[str, str]:
    return ("relationship", m.group("name"))


def _topic_target(m: re.Match[str]) -> tuple[str, str]:
    return ("topic", m.group("name"))


def _document_target(_m: re.Match[str]) -> tuple[str, str]:
    return ("document", "")


def _pair_target(m: re.Match[str]) -> tuple[str, str]:
    return ("relationship", f"{m.group('a')}↔{m.group('b')}")


#: (正则, code, severity, target 提取)—— **顺序即优先级**,最具体的在前。
#: 关键词取自 ``trove/services/kb/lint.py`` 的逐字消息,以及写盘路径
#: (``manage._apply_*``)抛出的 ValueError 文本 —— 两条来源共用一张表,
#: 这样「validate 干跑失败」与「detail 里的 lint 问题」永远是同一种结构。
_RULES: list[tuple[re.Pattern[str], str, str, Callable[[re.Match[str]], tuple[str, str]]]] = [
    # ── 指标家族 ──
    (re.compile(r"^指标「(?P<name>[^」]+)」重复定义"), "dup_metric", SEVERITY_ERROR, _metric_target),
    # ``\s*``:lint 里是「」紧接,而 _check_expr 的 label 会多一个空格
    (re.compile(r"^指标「(?P<name>[^」]+)」\s*表达式无法解析"), "expr_parse", SEVERITY_ERROR, _metric_target),
    (re.compile(r"^指标「(?P<name>[^」]+)」锚定的数据集未声明"), "dataset_undeclared", SEVERITY_ERROR, _metric_target),
    (re.compile(r"^指标「(?P<name>[^」]+)」datatype 非法"), "datatype_invalid", SEVERITY_WARNING, _metric_target),
    (re.compile(r"^指标「(?P<name>[^」]+)」custom_extensions 缺 vendor_name"), "custom_extensions", SEVERITY_WARNING, _metric_target),
    (re.compile(r"^指标「(?P<name>[^」]+)」filter 无法解析"), "filter_parse", SEVERITY_ERROR, _metric_target),
    (re.compile(r"^指标「(?P<name>[^」]+)」filter 引用"), "filter_ref", SEVERITY_ERROR, _metric_target),
    (re.compile(r"^指标「(?P<name>[^」]+)」把枚举值过滤写死进 metric"), "enum_baked", SEVERITY_WARNING, _metric_target),
    (re.compile(r"^指标「(?P<name>[^」]+)」agg_time_dimension"), "agg_time_dimension", SEVERITY_WARNING, _metric_target),
    (re.compile(r"^non_additive 指标「(?P<name>[^」]+)」被指标"), "non_additive", SEVERITY_WARNING, _metric_target),
    # 写盘路径错误文本(manage._metric_payload_to_ossie)
    (re.compile(r"^metric 表达式必填"), "expr_missing", SEVERITY_ERROR, lambda _m: _TARGET_UNKNOWN),
    # ── 字段/数据集家族(表 X.Y 前缀)──
    (re.compile(r"^表 (?P<target>\S+\.\S+) 含空/非法 synonym"), "bad_synonym", SEVERITY_WARNING, _field_target),
    (re.compile(r"^表 (?P<target>\S+\.\S+) 表达式无法解析"), "expr_parse", SEVERITY_ERROR, _field_target),
    # _apply_field 的 _check_expr label 走「字段「X」 表达式无法解析」(多一空格)
    (re.compile(r"^字段「(?P<target>[^」]+)」\s*表达式无法解析"), "expr_parse", SEVERITY_ERROR, _field_target),
    (re.compile(r"^表 (?P<target>\S+\.\S+) custom_extensions 缺 vendor_name"), "custom_extensions", SEVERITY_WARNING, _field_target),
    (re.compile(r"^表 (?P<target>\S+\.\S+) mask 非法"), "mask_invalid", SEVERITY_ERROR, _field_target),
    (re.compile(r"^表 (?P<target>\S+\.\S+) 按命名约定指向已声明的"), "fk_relationship_missing", SEVERITY_WARNING, _field_target),
    (re.compile(r"^表 (?P<ds>\S+) 字段「(?P<f>[^」]+)」重复定义"), "dup_field", SEVERITY_ERROR,
     lambda m: ("field", f"{m.group('ds')}.{m.group('f')}")),
    (re.compile(r"^表 (?P<name>\S+) unique_keys"), "unique_keys", SEVERITY_WARNING, _dataset_target),
    (re.compile(r"^表 (?P<name>\S+) custom_extensions 缺 vendor_name"), "custom_extensions", SEVERITY_WARNING, _dataset_target),
    (re.compile(r"^表 (?P<name>\S+) row_filter"), "row_filter", SEVERITY_ERROR, _dataset_target),
    # 写盘路径错误文本(manage._apply_field)
    (re.compile(r"^数据集不存在: (?P<name>\S+)"), "dataset_unknown", SEVERITY_ERROR, _dataset_target),
    (re.compile(r"^字段表达式必填"), "expr_missing", SEVERITY_ERROR, lambda _m: _TARGET_UNKNOWN),
    (re.compile(r"^字段目标必须是 dataset\.field 形式"), "field_target_invalid", SEVERITY_ERROR, lambda _m: _TARGET_UNKNOWN),
    # ── 文档级 ──
    (re.compile(r"^字段 .+ 声明 mask: hash"), "mask_hash_without_salt", SEVERITY_ERROR, _document_target),
    (re.compile(r"^文档顶层的 masking 会被忽略"), "masking_misplaced", SEVERITY_ERROR, _document_target),
    # ── 关系家族 ──
    (re.compile(r"^关系「(?P<name>[^」]*)」引用未声明的数据集"), "relationship_undeclared_dataset", SEVERITY_ERROR, _relationship_target),
    (re.compile(r"^关系「(?P<name>[^」]*)」未声明基数"), "relationship_missing_cardinality", SEVERITY_WARNING, _relationship_target),
    (re.compile(r"^关系「(?P<name>[^」]*)」为 M:N 且 fan_out=dedup"), "relationship_fan_out", SEVERITY_WARNING, _relationship_target),
    (re.compile(r"^关系「(?P<name>[^」]*)」声明 fan_out="), "relationship_fan_out", SEVERITY_WARNING, _relationship_target),
    (re.compile(r"^关系「(?P<name>[^」]*)」为 M:N 多对多"), "relationship_fan_out", SEVERITY_ERROR, _relationship_target),
    (re.compile(r"^关系图上 (?P<a>.+?)↔(?P<b>.+?) 存在多条简单路径"), "path_ambiguity", SEVERITY_ERROR, _pair_target),
    # ── 主题域家族 ──
    # 两个来源共用:lint(kb/lint.py ``_lint_topics``)与写盘路径
    # (manage._apply_topic 的 ValueError)—— 措辞不同、判定同一件事。
    (re.compile(r"^topics 必须是数组"), "topic_invalid", SEVERITY_ERROR, lambda _m: ("topic", "")),
    (re.compile(r"^topics 条目必须是映射"), "topic_invalid", SEVERITY_ERROR, lambda _m: ("topic", "")),
    (re.compile(r"^主题域缺少 name"), "topic_invalid", SEVERITY_ERROR, lambda _m: ("topic", "")),
    (re.compile(r"^主题域「(?P<name>[^」]+)」重复定义"), "dup_topic", SEVERITY_ERROR, _topic_target),
    (re.compile(r"^主题域「(?P<name>[^」]+)」未声明任何数据集"), "topic_empty_scope", SEVERITY_ERROR, _topic_target),
    # 写盘路径的同一判定:「datasets 必填(空作用域 = 域内什么都问不了)」
    (re.compile(r"^主题域「(?P<name>[^」]+)」的 datasets 必填"), "topic_empty_scope", SEVERITY_ERROR, _topic_target),
    (re.compile(r"^主题域「(?P<name>[^」]+)」引用未声明的数据集"), "topic_undeclared_dataset", SEVERITY_ERROR, _topic_target),
    (re.compile(r"^主题域「(?P<name>[^」]+)」声明的数据集未声明"), "topic_undeclared_dataset", SEVERITY_ERROR, _topic_target),
    (re.compile(r"^主题域「(?P<name>[^」]+)」引用未声明的指标"), "topic_undeclared_metric", SEVERITY_ERROR, _topic_target),
    (re.compile(r"^主题域「(?P<name>[^」]+)」的指标「"), "topic_metric_outside", SEVERITY_ERROR, _topic_target),
    # 与字段家族共用 code:形状问题同一类,前端提示同一句话
    (re.compile(r"^主题域「(?P<name>[^」]+)」的 (?:synonyms|examples) 含空"), "bad_synonym", SEVERITY_WARNING, _topic_target),
    (re.compile(r"^主题域「(?P<name>[^」]+)」custom_extensions 缺 vendor_name"), "custom_extensions", SEVERITY_WARNING, _topic_target),
    # ── 其它写盘路径错误 ──
    (re.compile(r"^未知草稿类型"), "kind_unknown", SEVERITY_ERROR, lambda _m: _TARGET_UNKNOWN),
]


def _item(code: str, severity: str, target: tuple[str, str],
          message: str, hint: str | None = None) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "target": {"kind": target[0], "name": target[1]},
        "message": message,
        "hint": hint if hint is not None else _HINTS.get(code, _HINTS["lint"]),
    }


#: sqlglot 的解析错误带 ANSI 下划线转义(终端渲染用),进 JSON 会变成乱码。
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def structured_issues(issues: list[str]) -> list[dict[str, Any]]:
    """扁平 lint/校验串 → ``issue_items``(契约:severity/code/target/message/hint)。

    未命中任何规则的串落 ``code="lint"``(severity=warning,target unknown),
    绝不丢弃 —— 结构性映射失败不该让一条真问题消失。
    """
    out: list[dict[str, Any]] = []
    for raw in issues or []:
        text = _ANSI.sub("", str(raw))
        for pattern, code, severity, target_fn in _RULES:
            m = pattern.match(text)
            if m:
                out.append(_item(code, severity, target_fn(m), text))
                break
        else:
            out.append(_item("lint", SEVERITY_WARNING, _TARGET_UNKNOWN, text))
    return out


def _dedup(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        key = (it["code"], it["target"]["kind"], it["target"]["name"], it["message"])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _normalized_expression(expr: str, dialect: str) -> str:
    """SQLGlot 规范化渲染(与 confirm 的严格解析同一档);解析失败 → ""。

    解析失败本身由 dry-run 的 ``_check_expr`` 报出(expr_parse),
    这里只负责给出「规范化后的表达式」供前端做 before/after 对比。
    """
    try:
        tree = parse_one(expr, read=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return ""
    if isinstance(tree, exp.Alias):
        return ""
    try:
        return tree.sql(dialect=dialect)
    except Exception:
        return ""


def validate_draft(
    data: dict[str, Any] | None,
    *,
    kind: str,
    action: str,
    name: str,
    payload: dict[str, Any] | None = None,
    dialect: str | None = None,
) -> dict[str, Any]:
    """草稿干跑校验(纯函数,零 IO):**与 confirm 走同一条应用路径**。

    步骤刻意与 ``SemanticManager.confirm_draft`` 对齐,顺序也一致:

    1. ``_apply_draft`` 应用到内存副本(语法 + 数据集锚定 + 目标存在性);
    2. ``lint_semantics_document`` 判**应用后**的文档(重复定义/关系/脱敏…)。

    因此 ``ok`` 的语义是「这份草稿现在提交会不会过门禁」,不是「看起来像不像
    对」:任何一步在 confirm 会抛的东西,这里都必须出现在 errors 里。

    返回 ``{"ok", "errors", "warnings", "normalized": {"expression"}}``。
    ``normalized.expression`` 仅供 metric/field 的 upsert;其余情况为 ""。
    干跑失败时 lint 判**原样文档**(半应用状态没有判的必要,且会误报)。
    """
    dialect = dialect or "sqlite"
    payload = payload or {}
    draft = {"kind": kind, "action": action, "name": name,
             "payload": payload or None}
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    normalized = ""
    if action == "upsert" and kind in ("metric", "field"):
        expr = str(payload.get("expression") or "").strip()
        if expr:
            normalized = _normalized_expression(expr, dialect)

    applied = copy.deepcopy(data) if data else {}
    apply_error = ""
    try:
        from trove.services.semantic_layer.manage import _apply_draft  # 懒导入避免环
        _apply_draft(applied, draft, dialect)
    except (ValueError, TypeError) as e:
        apply_error = str(e)
    if apply_error:
        for item in structured_issues([apply_error]):
            if item["target"]["kind"] == "unknown":
                item["target"] = {"kind": kind, "name": name}
            errors.append(item)
        applied = copy.deepcopy(data) if data else {}

    for item in structured_issues(lint_semantics_document(applied, dialect=dialect)):
        (errors if item["severity"] == SEVERITY_ERROR else warnings).append(item)

    errors = _dedup(errors)
    warnings = _dedup(warnings)
    # ok 取「过门禁」语义:今天的写盘门禁在 issues 非空即拦截(warning 同样拦),
    # 所以 warnings 也会让 ok=False —— 校验结果与 confirm 行为保持一致。
    return {
        "ok": not errors and not warnings,
        "errors": errors,
        "warnings": warnings,
        "normalized": {"expression": normalized},
    }
