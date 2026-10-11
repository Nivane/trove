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

``severity`` 的取值是 ``error`` / ``warning``,门禁**分级**(拍板 Q4):
结构性错位(error)一律拦下;**拍板豁免的语义缺口**(warning 且 code 在
:data:`_ADVISORY_CODES`)只提示、不拦写盘。分级只认这一个名单 —— 既有
warning、未命中规则表的 ``lint`` 兜底串一律照旧阻断(fail-closed:新 lint
消息不会因为忘了登记就悄悄放行)。:func:`blocking_issues` 是这一判据的
唯一权威,写盘前门禁(``changes._write_merge``)与 git pre-commit 门禁
(``KbService.semantics_lint``)都经过它;展示面不过滤 —— 被豁免的问题
照常出现在 ``issues`` / ``issue_items`` 里(「提示得到」不等于「看不见」)。
因此 :func:`validate_draft` 的 ``ok`` 取「能不能过门禁」语义:error、
非豁免 warning 任一在场即 False —— 让校验结果与 confirm 的实际行为一致,
是这一层存在的唯一理由。
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
    "entity_invalid": "entity 子块须为映射,键取 display_name / natural_keys / aliases;坏形状会被装载侧整体丢弃",
    "entity_natural_key": "自然键须是本数据集 fields 里已声明的字段名",
    "entity_natural_key_missing": "补 natural_keys(业务键字段,如 loan_id),或给数据集声明 primary_key 走回退",
    "entity_alias_conflict": "别名与自身/其它数据集的同义词重复:删掉重复项,或把它归给真正拥有它的对象",
    "hierarchy_invalid": "hierarchies 须为映射数组,每项含 name 与非空 levels",
    "hierarchy_dup": "同名层级只保留一条:确认哪条是权威链后改名或合并",
    "hierarchy_dataset_missing": "层级须声明归属数据集 dataset,且该数据集已在 datasets 里声明",
    "hierarchy_level_invalid": "级别字段须来自归属数据集的 fields(层级链逐跳都要解析得到)",
    "hierarchy_levels_sparse": "层级至少两级(粗 → 细);单级链条没有可钻的下一级,补一级或删除该层级",
    "hierarchy_level_dup": "同一级别只写一次(装载侧保序去重,重复多半是写错)",
    "hierarchy_coverage_gap": "该口径在层级级别字段上收敛,会静默排除层级其他分支:如非刻意,改用维度过滤,或单列独立指标并写明口径",
    "entity_misplaced": "entity 写在 semantic_model[0].datasets[].entity(挂在数据集下),顶层会被忽略",
    "hierarchies_misplaced": "hierarchies 写在 semantic_model[0] 下(与 time_spine 同层),顶层会被忽略",
    "kind_unknown": "kind 必须为 metric / field / dataset / topic / entity / relationship / hierarchy 之一",
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


def _entity_target(m: re.Match[str]) -> tuple[str, str]:
    # 实体与数据集 1:1(挂在 ``datasets[].entity`` 下),目标名 = 归属数据集名。
    return ("entity", m.group("name"))


def _hierarchy_target(m: re.Match[str]) -> tuple[str, str]:
    return ("hierarchy", m.group("name"))


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
    # 新声明的两个放错层(与 masking 同款双守卫:解析层出声、写盘层拦下)
    (re.compile(r"^文档顶层的 entity 会被忽略"), "entity_misplaced", SEVERITY_ERROR, _document_target),
    (re.compile(r"^文档顶层的 hierarchies 会被忽略"), "hierarchies_misplaced", SEVERITY_ERROR, _document_target),
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
    # ── 实体家族(kb/lint.py ``_lint_entities``;设计 §4.1)──
    (re.compile(r"^数据集「(?P<name>[^」]*)」的 entity 声明"), "entity_invalid", SEVERITY_ERROR, _entity_target),
    (re.compile(r"^数据集「(?P<name>[^」]*)」的实体 natural_keys 必须是名字数组"), "entity_invalid", SEVERITY_ERROR, _entity_target),
    (re.compile(r"^数据集「(?P<name>[^」]*)」的实体 aliases 必须是名字数组"), "entity_invalid", SEVERITY_ERROR, _entity_target),
    (re.compile(r"^数据集「(?P<name>[^」]*)」的实体自然键「"), "entity_natural_key", SEVERITY_ERROR, _entity_target),
    (re.compile(r"^数据集「(?P<name>[^」]*)」声明了实体但未声明自然键"), "entity_natural_key_missing", SEVERITY_WARNING, _entity_target),
    (re.compile(r"^数据集「(?P<name>[^」]*)」的实体别名「[^」]*」与自身 synonyms"), "entity_alias_conflict", SEVERITY_WARNING, _entity_target),
    (re.compile(r"^数据集「(?P<name>[^」]*)」的实体别名「[^」]*」与数据集「"), "entity_alias_conflict", SEVERITY_WARNING, _entity_target),
    # ── 钻取层级家族(kb/lint.py ``_lint_hierarchies``;设计 §4.3)──
    (re.compile(r"^hierarchies 必须是数组"), "hierarchy_invalid", SEVERITY_ERROR, lambda _m: ("hierarchy", "")),
    (re.compile(r"^hierarchies 条目必须是映射"), "hierarchy_invalid", SEVERITY_ERROR, lambda _m: ("hierarchy", "")),
    (re.compile(r"^钻取层级缺少 name"), "hierarchy_invalid", SEVERITY_ERROR, lambda _m: ("hierarchy", "")),
    (re.compile(r"^层级「(?P<name>[^」]+)」重复定义"), "hierarchy_dup", SEVERITY_ERROR, _hierarchy_target),
    (re.compile(r"^层级「(?P<name>[^」]+)」未声明归属数据集"), "hierarchy_dataset_missing", SEVERITY_ERROR, _hierarchy_target),
    (re.compile(r"^层级「(?P<name>[^」]+)」声明的数据集「"), "hierarchy_dataset_missing", SEVERITY_ERROR, _hierarchy_target),
    (re.compile(r"^层级「(?P<name>[^」]+)」的 levels 必须是名字数组"), "hierarchy_invalid", SEVERITY_ERROR, _hierarchy_target),
    (re.compile(r"^层级「(?P<name>[^」]+)」的级别字段「"), "hierarchy_level_invalid", SEVERITY_ERROR, _hierarchy_target),
    (re.compile(r"^层级「(?P<name>[^」]+)」的级别「"), "hierarchy_level_dup", SEVERITY_WARNING, _hierarchy_target),
    (re.compile(r"^层级「(?P<name>[^」]+)」解析后只有"), "hierarchy_levels_sparse", SEVERITY_WARNING, _hierarchy_target),
    # 口径覆盖(FR-HIER-3):消息以指标开头(可修处在 metric 的过滤/表达式),
    # 目标落 metric;warning 起步、不阻断(拍板:语义缺口提示)。
    (re.compile(r"^指标「(?P<name>[^」]+)」的过滤/派生条件对层级「"), "hierarchy_coverage_gap", SEVERITY_WARNING, _metric_target),
    # 写盘路径错误文本(manage._apply_hierarchy)—— 与 lint 措辞不同、判定同一件事
    (re.compile(r"^层级「(?P<name>[^」]+)」的 dataset 必填"), "hierarchy_dataset_missing", SEVERITY_ERROR, _hierarchy_target),
    (re.compile(r"^层级「(?P<name>[^」]+)」的 levels 必填"), "hierarchy_levels_sparse", SEVERITY_ERROR, _hierarchy_target),
    # ── 其它写盘路径错误 ──
    (re.compile(r"^未知草稿类型"), "kind_unknown", SEVERITY_ERROR, lambda _m: _TARGET_UNKNOWN),
    # create_draft / changes 的 kind 白名单消息(闭集;hint 列全部合法 kind)
    (re.compile(r"^kind 必须为"), "kind_unknown", SEVERITY_ERROR, lambda _m: _TARGET_UNKNOWN),
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

#: 拍板 Q4 豁免的「语义缺口」类 lint:warning 起步、**不阻断**写盘/提交 ——
#: 只收新声明的语义缺口提示,既有 warning 一律不豁免(兼容安全带:
#: 存量行为逐字节同前)。结构性错位、解析不到一律不在名单里。
_ADVISORY_CODES = frozenset({
    "entity_natural_key_missing",
    "entity_alias_conflict",
    "hierarchy_levels_sparse",
    "hierarchy_level_dup",
    "hierarchy_coverage_gap",
})


def _advisory(severity: str, code: str) -> bool:
    """豁免判据(唯一口径):**warning 且 code 在点名名单**。

    严格取合取:error 级的同 code 串、既有 warning、未命中规则的兜底串
    全部照旧阻断 —— 判据方向永远是 fail-closed。
    """
    return severity == SEVERITY_WARNING and code in _ADVISORY_CODES


def _classify(text: str) -> tuple[str, str, tuple[str, str]] | None:
    """过 :data:`_RULES`(顺序即优先级);未命中 → ``None``。"""
    for pattern, code, severity, target_fn in _RULES:
        m = pattern.match(text)
        if m:
            return code, severity, target_fn(m)
    return None


def blocking_issues(issues: list[str]) -> list[str]:
    """门禁分级(零 IO,唯一判据):结构性错误拦下,拍板豁免的语义缺口放行。

    消费方是两条写盘路径 —— ``changes._write_merge``(写盘前门禁)与
    ``KbService.semantics_lint``(git pre-commit 门禁)。**展示面不经过
    这里**:被豁免的问题照常出现在 ``issues`` / ``issue_items`` 里。

    未命中规则表的串原样保留(兜底为阻断)—— 判据 fail-closed,新增
    lint 消息不会因为忘了登记豁免就悄悄放行。
    """
    blocking: list[str] = []
    for raw in issues or []:
        text = _ANSI.sub("", str(raw))
        hit = _classify(text)
        if hit is not None and _advisory(hit[1], hit[0]):
            continue
        blocking.append(str(raw))
    return blocking


def structured_issues(issues: list[str]) -> list[dict[str, Any]]:
    """扁平 lint/校验串 → ``issue_items``(契约:severity/code/target/message/hint)。

    未命中任何规则的串落 ``code="lint"``(severity=warning,target unknown),
    绝不丢弃 —— 结构性映射失败不该让一条真问题消失。
    """
    out: list[dict[str, Any]] = []
    for raw in issues or []:
        text = _ANSI.sub("", str(raw))
        hit = _classify(text)
        if hit is None:
            out.append(_item("lint", SEVERITY_WARNING, _TARGET_UNKNOWN, text))
        else:
            out.append(_item(hit[0], hit[1], hit[2], text))
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
    # ok 取「过门禁」语义(与 :func:`blocking_issues` 同一判据):error 与
    # 非豁免 warning 一律拦;拍板 Q4 豁免的语义缺口(warning 且 advisory)
    # 只提示不拦 —— 校验结果与 confirm 的实际行为保持一致。
    blocking_warnings = [
        w for w in warnings if not _advisory(w["severity"], w["code"])]
    return {
        "ok": not errors and not blocking_warnings,
        "errors": errors,
        "warnings": warnings,
        "normalized": {"expression": normalized},
    }
