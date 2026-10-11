"""Static checks over parsed KB entries — catch silent KB regressions.

Covers the failure modes observed when the deterministic kb init replaced
a hand-crafted KB:
  - term mappings referencing unknown columns
  - SUM/AVG over ID/account-number columns (e.g. SUM(account_to))
  - example SQL that doesn't parse, references unknown tables, or writes
  - empty column descriptions (e.g. district A-columns left blank)
  - lessons with unusable patterns/notes
  - Chinese-only example questions (unreachable by English retrieval)

All functions are pure: parsed entries in, issue message list out.
"""

from __future__ import annotations

import re
from typing import Any

from sqlglot import ErrorLevel, exp, parse_one

from trove.services.kb.lesson_distill import MAX_PATTERN_LEN
# 字段级 values 的条数上限与装载侧同一个常量:lint 放行的上界必须就是
# 装载侧截断的那条线,否则「装进来的一定合法」不成立。
from trove.services.semantic_layer.models import MAX_FIELD_VALUES
# metric 锚定数据集只有一种判定方式(与装载侧共用,避免"lint 说锚在 A、
# 运行时锚在 B"这类两处不一致)。
from trove.services.semantic_layer.ossie import _dataset_refs

_ID_LIKE_SUFFIXES = ("_id", "_to", "_from", "_code", "id")

#: 字段级脱敏值域(设计 §5.5)。空串 = 未声明;存量模型没有 ``mask`` 键(A11)。
_VALID_MASKS = {"", "none", "partial", "hash", "null"}

#: 时态 datatype(OSSIE DataType 口径)。``is_temporal_field`` 与
#: ``lint_semantics`` 的时间字段判定共用同一份 —— 两处判定必须一致,
#: 否则回填写的键会被 lint 判成违规(自己人打自己人)。
_TEMPORAL_DATATYPES = frozenset(
    {"date", "time", "datetime", "datetimetz", "timestamp"}
)

def is_temporal_field(field: dict[str, Any]) -> bool:
    """字段是否时间维度:``semantic_role: time`` 或时态 datatype。

    回填脚本（``init_pipeline._backfill_values``）与 lint 共用这一个判定:
    日期列的取值域不是"值词表"（写进去会被 ``_lint_values`` 拦），
    而在两处各判一次迟早会判出两种结果。
    """
    if str(field.get("semantic_role") or "").strip().lower() == "time":
        return True
    dt = str(field.get("datatype") or "").strip().lower()
    return dt in _TEMPORAL_DATATYPES


def _lint_values(issues: list[str], ds_name: str, field: dict[str, Any]) -> None:
    """字段级 ``values``(列的实际取值)的形状门。三条:

    1. **必须是非空 string 数组** —— 映射/嵌套/空串是坏形状(空串在装载
       侧会被丢掉,但盘上留着它意味着写它的人以为那是取值);
    2. **≤ MAX_FIELD_VALUES** —— 超限说明探测不该落这一列(取值域不完整
       的"事实"比没有更坏:值路由会把它当完整词表用);
    3. **时间字段不得声明** —— 日期列的取值是时点,不是可路由的取值词表。

    省略该键 = 未探测(存量模型走这条,不得误报)。它**只是数据**:lint
    不检查它与 semantic_role / enum_display 的"一致性"—— 那两样不是它的
    归属,是消费侧的防御读取。
    """
    if "values" not in field:
        return
    where = f"表 {ds_name}.{field.get('name', '')}"
    raw = field.get("values")
    if not isinstance(raw, list):
        issues.append(f"{where} values 必须是字符串数组: {raw!r}")
        return
    bad = [
        v for v in raw
        if not isinstance(v, str) or not v.strip()
    ]
    if bad:
        issues.append(f"{where} values 含非字符串/空条目: {bad[:3]!r}")
    if len(raw) > MAX_FIELD_VALUES:
        issues.append(
            f"{where} values 超过 {MAX_FIELD_VALUES} 条({len(raw)})——"
            "取值域不完整的事实不如不写,请只落完整探测结果")
    if is_temporal_field(field):
        issues.append(
            f"{where} 是时间字段,不得声明 values(取值是时点,不是取值词表)")


def parse_enum_values(enum_text: str) -> set[str]:
    """枚举文本 → 取值集合(兼容三种来源格式):

    - "A=含义" 分号列表(LLM 草稿/人工;全角冒号 "F：female" 同义)
    - "'A' stands for ..." BIRD value_description 原文(多行)
    - "VALUE1; VALUE2" 探测原始值(无 "=" 无引号 → 整段为值)
    """
    values: set[str] = set()
    for part in re.split(r"[;\n]", enum_text):
        entry = part.strip()
        if not entry:
            continue
        if "=" in entry or "：" in entry:
            key = entry.split("=" if "=" in entry else "：", 1)[0].strip()
        else:
            m = re.match(r"""['"]([^'"]+)['"]""", entry)
            key = m.group(1).strip() if m else entry
        if key:
            values.add(key)
    return values


def _parse(sql: str, dialect: str = "mysql") -> exp.Expression | None:
    """严格解析:语法错误返回 None(宽松模式下 "SELEC broken" 会被
    解析成 Alias 而非报错);调用方按需再判是否为查询语句。

    默认按 MySQL 解析(BIRD 评估目标);BIRD gold 的 DATE_FORMAT、
    CAST(x AS DOUBLE)、反引号限定列等写法通用方言会误报。
    """
    try:
        return parse_one(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
    except Exception:
        return None


def lint_terms(
    terms: list[dict[str, Any]],
    schema: dict[str, set[str]],
    dialect: str = "mysql",
) -> list[str]:
    """Term mappings must reference existing columns; no SUM/AVG on ID-like columns.

    schema 的列名与 mapping 按小写比较(MySQL 列名大小写不敏感,
    schema_notes 里 A5 与 mapping 里 a5 是同一列)。
    """
    schema = {t: {c.lower() for c in cols} for t, cols in schema.items()}
    issues: list[str] = []
    for term in terms:
        name = str(term.get("term", ""))
        mapping = str(term.get("mapping", ""))
        tree = _parse(mapping, dialect)
        if tree is None:
            issues.append(f"术语「{name}」mapping 无法解析: {mapping[:60]}")
            continue
        agg = next(tree.find_all(exp.Sum, exp.Avg), None)
        bound = [t for t in term.get("tables", []) if t]
        for col in tree.find_all(exp.Column):
            col_name = (col.name or "").lower()
            col_table = (col.table or "").lower()
            if col_table:
                if col_table not in schema or col_name not in schema[col_table]:
                    issues.append(
                        f"术语「{name}」mapping 引用不存在的列 {col_table}.{col_name}")
            elif bound:
                if not any(col_name in schema.get(t, set()) for t in bound):
                    issues.append(f"术语「{name}」mapping 引用绑定表中不存在的列 {col_name}")
            elif not any(col_name in cols for cols in schema.values()):
                issues.append(f"术语「{name}」mapping 引用不存在的列 {col_name}")
            if agg is not None and col_name.endswith(_ID_LIKE_SUFFIXES):
                issues.append(f"术语「{name}」对 ID/账号类列 {col_name} 做聚合,无业务含义")
    return issues


def lint_examples(
    examples: list[dict[str, Any]],
    tables: set[str],
    dialect: str = "mysql",
) -> list[str]:
    """Example SQL must parse, read only known tables; questions need English reachability."""
    issues: list[str] = []
    for example in examples:
        question = str(example.get("question", ""))
        sql = str(example.get("sql", ""))
        tree = _parse(sql, dialect)
        if tree is None:
            issues.append(f"示例「{question[:30]}」SQL 无法解析: {sql[:60]}")
            continue
        if any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Drop))
               for n in tree.walk()):
            issues.append(f"示例「{question[:30]}」包含写操作,不应作为参考示例")
            continue
        if not isinstance(tree, exp.Query):
            issues.append(f"示例「{question[:30]}」SQL 无法解析(或不是查询语句): {sql[:60]}")
            continue
        for table in tree.find_all(exp.Table):
            if table.name and table.name not in tables:
                issues.append(f"示例「{question[:30]}」引用了不存在的表 {table.name}")
        if question and not re.search(r"[A-Za-z]", question):
            issues.append(
                f"示例问题「{question[:30]}」无英文内容,英文问题检索命中率低(建议双语)")
    return issues


def lint_semantics(model: dict[str, Any], dialect: str = "mysql") -> list[str]:
    """语义层模型(OSSIE):同名字段/指标、坏表达式、非法 alias、越界关系、
    metric 内建 filter / agg_time_dimension 声明一致性、M:N 与路径二义提前暴露。"""
    issues: list[str] = []
    _VALID_OSSIE_DATATYPES = {
        "string", "integer", "decimal", "float", "boolean",
        "date", "time", "datetime", "datetimetz", "opaque",
    }
    datasets = list(model.get("datasets", []) or [])
    ds_fields: dict[str, set[str]] = {
        str(d.get("name", "")): {
            str(f.get("name", "")) for f in (d.get("fields", []) or [])
        }
        for d in datasets
    }
    temporal_fields: dict[str, set[str]] = {}
    enum_fields: dict[str, set[str]] = {}
    for d in datasets:
        ds_name = str(d.get("name", ""))
        tmp = set()
        enums = set()
        for f in d.get("fields", []) or []:
            if isinstance(f, dict) and is_temporal_field(f):
                tmp.add(str(f.get("name", "")))
            if (
                str(f.get("semantic_role", "") or "").lower() == "enum"
                or (f.get("enum_display") or {})
            ):
                enums.add(str(f.get("name", "")))
        temporal_fields[ds_name] = tmp
        enum_fields[ds_name] = enums

    seen_metrics: set[str] = set()
    # 层级级别字段索引(FR-HIER-3):dataset → [(层级名, [级别字段])],按声明序。
    # 只收解析得动的级别(悬空 dataset/字段已由 _lint_hierarchies 报错);
    # 未声明层级 → 空索引,覆盖检查整段零动作(存量模型输出不变)。
    levels_by_ds = _hierarchy_levels_by_dataset(model, ds_fields)
    for m in model.get("metrics", []) or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name", ""))
        if name and name in seen_metrics:
            issues.append(f"指标「{name}」重复定义")
        seen_metrics.add(name)
        for dia in (m.get("expression") or {}).get("dialects", []) or []:
            expr = str(dia.get("expression", ""))
            if expr and _parse(expr, dialect) is None:
                issues.append(f"指标「{name}」表达式无法解析: {expr[:60]}")
        _lint_metric_filter(issues, m, ds_fields, dialect)
        _lint_metric_filter_enum(issues, m, ds_fields, enum_fields, dialect)
        _lint_metric_agg_time(issues, m, ds_fields, temporal_fields)
        _lint_metric_non_additive(issues, m, model)
        _lint_metric_hierarchy_coverage(
            issues, m, levels_by_ds, temporal_fields, dialect)
        if m.get("datatype") and str(m.get("datatype")).lower() not in _VALID_OSSIE_DATATYPES:
            issues.append(
                f"指标「{name}」datatype 非法: {m.get('datatype')} "
                f"(应为 {sorted(_VALID_OSSIE_DATATYPES)})")
        for ext in m.get("custom_extensions") or []:
            if not (isinstance(ext, dict) and str(ext.get("vendor_name") or "").strip()):
                issues.append(f"指标「{name}」custom_extensions 缺 vendor_name")

    ds_names = set(ds_fields)
    for d in datasets:
        if not isinstance(d, dict):
            continue
        ds_name = str(d.get("name", ""))
        declared = ds_fields.get(ds_name, set())
        # OSSIE v0.2.0.dev0:unique_keys 的每个键必须落在已声明字段内
        for keys in d.get("unique_keys") or []:
            if not isinstance(keys, list):
                issues.append(f"表 {ds_name} unique_keys 必须为列名数组")
                continue
            for k in keys:
                if str(k) not in declared:
                    issues.append(
                        f"表 {ds_name} unique_keys 引用未声明的列 {k}")
        _lint_row_filter(
            issues, ds_name, str(d.get("row_filter") or "").strip(),
            declared, dialect,
        )
        for ext in d.get("custom_extensions") or []:
            if not (isinstance(ext, dict) and str(ext.get("vendor_name") or "").strip()):
                issues.append(f"表 {ds_name} custom_extensions 缺 vendor_name")
        seen_fields: set[str] = set()
        for f in d.get("fields", []) or []:
            if not isinstance(f, dict):
                continue
            fname = str(f.get("name", ""))
            if fname and fname in seen_fields:
                issues.append(f"表 {ds_name} 字段「{fname}」重复定义")
            seen_fields.add(fname)
            for syn in (f.get("ai_context") or {}).get("synonyms") or []:
                if not isinstance(syn, str) or not syn.strip():
                    issues.append(f"表 {ds_name}.{fname} 含空/非法 synonym")
            for dia in (f.get("expression") or {}).get("dialects", []) or []:
                expr = str(dia.get("expression", ""))
                if expr and _parse(expr, dialect) is None:
                    issues.append(f"表 {ds_name}.{fname} 表达式无法解析: {expr[:60]}")
            for ext in f.get("custom_extensions") or []:
                if not (isinstance(ext, dict) and str(ext.get("vendor_name") or "").strip()):
                    issues.append(f"表 {ds_name}.{fname} custom_extensions 缺 vendor_name")
            _lint_values(issues, ds_name, f)

    rel_pairs: set[frozenset[str]] = set()
    for r in model.get("relationships", []) or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("from", "")) not in ds_names or str(r.get("to", "")) not in ds_names:
            issues.append(f"关系「{r.get('name', '')}」引用未声明的数据集")
        if not str(r.get("cardinality") or "").strip():
            issues.append(f"关系「{r.get('name', '')}」未声明基数(cardinality),编译器将保守 MISS")
        if str(r.get("cardinality") or "").strip().upper() == "M:N":
            fan = str(r.get("fan_out") or "").strip().lower()
            if fan == "dedup":
                issues.append(
                    f"关系「{r.get('name', '')}」为 M:N 且 fan_out=dedup:编译期以 "
                    "SELECT DISTINCT 子查询消除整行重复(仅适用纯关联/桥接表的"
                    "精确重复行;同键多行请建模为 1:N + 中间表)")
            elif fan.startswith("bridge:"):
                issues.append(
                    f"关系「{r.get('name', '')}」声明 fan_out={fan},但 bridge "
                    "豁免未实现,编译器仍会拒绝 fan-out")
            else:
                issues.append(
                    f"关系「{r.get('name', '')}」为 M:N 多对多,编译期将拒绝 fan-out"
                    "(如确属多对多:建议改为 1:N + 中间表显式建模,"
                    "或声明 fan_out=dedup 显式豁免)")
        rel_pairs.add(frozenset((str(r.get("from", "")), str(r.get("to", "")))))

    _lint_path_ambiguity(issues, model, ds_names)
    _lint_mask(issues, model)
    _lint_topics(issues, model, ds_fields, ds_names)
    _lint_entities(issues, model, ds_fields, ds_names)
    _lint_hierarchies(issues, model, ds_fields, ds_names)

    # 命名约定 FK → 已声明数据集 但未声明关系:编译器「matched 但连不上」
    # 会产出 FROM anchor 无 JOIN 的非法 SQL(被投影表守卫拦成 MISS)。
    # 在建模期暴露,别等查询期。
    for d in datasets:
        if not isinstance(d, dict):
            continue
        ds_name = str(d.get("name", ""))
        for f in d.get("fields", []) or []:
            if not isinstance(f, dict):
                continue
            fname = str(f.get("name", ""))
            if not fname.endswith("_id") or len(fname) <= 3:
                continue
            target = fname[:-3]
            if target == ds_name or target not in ds_names:
                continue
            if frozenset((ds_name, target)) not in rel_pairs:
                issues.append(
                    f"表 {ds_name}.{fname} 按命名约定指向已声明的 {target},"
                    "但 relationships 未声明这对关系(联表将 MISS)")
    return issues


def lint_semantics_document(data: dict[str, Any], dialect: str = "mysql") -> list[str]:
    """Lint a full ``semantics.yml`` document (every ``semantic_model`` entry).

    ``lint_semantics`` takes a single OSSIE model mapping; this is the
    document-level wrapper shared by the admin issues view, the git
    pre-commit gate and the pre-write gate — so all three judge exactly
    the same bytes.
    """
    issues: list[str] = []
    if "masking" in (data or {}):
        # masking 挂在模型入口下(与 time_spine 同层);设计稿 §5.5 注释的
        # 「semantics.yml 顶层」是单模型文件的松散说法。写错层会被解析器
        # 静默忽略 = 静默不脱敏,所以在写盘前拦下而不是留给运行时。
        issues.append(
            "文档顶层的 masking 会被忽略 —— 请写在 semantic_model[0] 下"
            "(与 time_spine 同层)")
    if "topics" in (data or {}):
        # 同 masking:写错层会被解析器忽略。主题域是**收敛边界**,静默忽略
        # 意味着「看起来按主题收敛了、其实没收」,比不脱敏更难发现。
        issues.append(
            "文档顶层的 topics 会被忽略 —— 请写在 semantic_model[0] 下"
            "(与 time_spine 同层)")
    if "entity" in (data or {}):
        # 同 masking/topics:entity 只有**一个**声明位置(数据集子块
        # ``datasets[].entity``);放错层被静默忽略 = 「对象看起来声明了、
        # 投影里却没有」,一次无痕的失败。与 ossie 侧的 logger.warning
        # 成对(解析层出声、写盘层拦下 —— N3 的双守卫先例)。
        issues.append(
            "文档顶层的 entity 会被忽略 —— 请写在 semantic_model[0] 下的"
            "数据集上(semantic_model[0].datasets[].entity)")
    if "hierarchies" in (data or {}):
        # 同 topics:层级写在模型入口下(与 time_spine 同层);放错层会被
        # 解析器忽略,钻取链看起来生效、其实不存在。
        issues.append(
            "文档顶层的 hierarchies 会被忽略 —— 请写在 semantic_model[0] 下"
            "(与 time_spine 同层)")
    for entry in (data or {}).get("semantic_model", []) or []:
        if isinstance(entry, dict):
            issues += lint_semantics(entry, dialect=dialect)
    return issues


def _lint_mask(issues: list[str], model: dict[str, Any]) -> None:
    """字段级脱敏声明(设计 §5.5 / §10 / §11 R6)。两条规则:

    1. **值域**:``mask`` 只能取 ``none | partial | hash | null``;**省略该键**
       = 不脱敏(存量模型走这条,不得误报)。裸 ``mask: null`` 会被 YAML 解析
       成 None,而作者本意多半是 ``null`` 模式 —— 解析器不猜(猜错方向就是
       静默不脱敏),这里拦下并提示加引号。
    2. **hash 必须有 salt**:无 salt 的 hash 可被彩虹表还原(R6),不得降级为
       明文。只对 ``hash`` 要 —— ``partial`` 是可人工核对的掩码,没有这个问题。

    salt 引用只认**模型级** ``masking.hash_salt_ref``:lint 是纯函数,三个调用
    点(管理端 issues / git pre-commit 门禁 / 写盘前门禁)判**同一份字节**。
    部署配置的兜底是**运行时**的事,引进来会让同一份文档在不同机器上结论
    不同 —— 而 pre-commit 门禁恰恰要在没有密钥的 CI 上跑。引用不是密文,
    写进 YAML 不泄漏(salt 的**值**才走 secrets)。
    """
    hash_fields: list[str] = []
    for d in model.get("datasets", []) or []:
        if not isinstance(d, dict):
            continue
        ds_name = str(d.get("name", ""))
        for f in d.get("fields", []) or []:
            if not isinstance(f, dict) or "mask" not in f:
                continue  # 未声明 → 惰性(A11)
            raw = f.get("mask")
            if raw is None:
                # 键在、值是 YAML 的 null:作者本意多半是 ``null`` 模式,而
                # 解析器给的是空串(不脱敏)—— 最严的声明变成什么都不做,
                # 必须在这里拦下,而不是等运行期。
                issues.append(
                    f"表 {ds_name}.{f.get('name', '')} mask 非法: None"
                    "(裸 null 会被 YAML 解析成空值 = 不脱敏,"
                    "高敏列请写 \"null\";如本意是不脱敏请省略该键)")
                continue
            value = str(raw).strip().lower()
            if value not in _VALID_MASKS:
                issues.append(
                    f"表 {ds_name}.{f.get('name', '')} mask 非法: {raw!r}"
                    "(应为 none | partial | hash | null,省略该键 = 不脱敏)")
                continue
            if value == "hash":
                hash_fields.append(f"{ds_name}.{f.get('name', '')}")
    if hash_fields and not _hash_salt_ref(model):
        issues.append(
            "字段 " + "、".join(hash_fields) + " 声明 mask: hash,"
            "但模型未声明可解析的 salt 引用(masking.hash_salt_ref 为空)——"
            "无 salt 的 hash 可被彩虹表还原,请在模型级声明引用"
            "(如 env:TROVE_MASK_SALT)")


def _hash_salt_ref(model: dict[str, Any]) -> str:
    """模型级 salt 引用(空 = 未声明)。"""
    masking = model.get("masking")
    if not isinstance(masking, dict):
        return ""
    return str(masking.get("hash_salt_ref") or "").strip()


def _lint_row_filter(
    issues: list[str], ds_name: str, row_filter: str,
    declared: set[str], dialect: str,
) -> None:
    """数据集 row_filter(RLS)必须可解析,且只引用本数据集的已声明字段。

    编译期会把该谓词注入引用此数据集的每个 FROM/JOIN 顶层 WHERE;引用
    其他表/未声明列会让任一问题都编译失败(或更糟,静默少一个安全条件)。
    在声明层拦下,别等查询期。
    """
    if not row_filter:
        return
    tree = _parse(row_filter, dialect)
    if tree is None:
        issues.append(f"表 {ds_name} row_filter 无法解析: {row_filter[:60]}")
        return
    declared_lower = {str(f).lower() for f in declared}
    for col in tree.find_all(exp.Column):
        col_name = (col.name or "").lower()
        col_table = (col.table or "").lower()
        if col_table and col_table != ds_name.lower():
            issues.append(
                f"表 {ds_name} row_filter 引用了其他表 {col.table}.{col.name}"
                "(行级过滤只能限定本数据集)")
        elif col_name not in declared_lower:
            issues.append(
                f"表 {ds_name} row_filter 引用未声明的列 {col.name}")


def _lint_metric_filter(
    issues: list[str], m: dict[str, Any], ds_fields: dict[str, set[str]],
    dialect: str,
) -> None:
    """metric 内建 filter 必须可解析,且引用的列须落在 metric 数据集的声明字段内。"""
    ftext = str(m.get("filter") or "").strip()
    if not ftext:
        return
    name = str(m.get("name", ""))
    tree = _parse(ftext, dialect)
    if tree is None:
        issues.append(f"指标「{name}」filter 无法解析: {ftext[:60]}")
        return
    bound = [t for t in (m.get("datasets") or []) if t]
    for col in tree.find_all(exp.Column):
        col_name = (col.name or "").lower()
        col_table = (col.table or "").lower()
        if col_table:
            if col_table not in ds_fields or col_name not in ds_fields[col_table]:
                issues.append(
                    f"指标「{name}」filter 引用不存在的列 {col_table}.{col_name}")
        elif bound:
            if not any(col_name in ds_fields.get(t, set()) for t in bound):
                issues.append(
                    f"指标「{name}」filter 引用不在其数据集中的列 {col_name}")
        elif not any(col_name in cols for cols in ds_fields.values()):
            issues.append(f"指标「{name}」filter 引用不存在的列 {col_name}")


def _lint_metric_filter_enum(
    issues: list[str], m: dict[str, Any], ds_fields: dict[str, set[str]],
    enum_fields: dict[str, set[str]], dialect: str,
) -> None:
    """值不得写死在 metric 里:对已声明 enum 字段做等值过滤 → 建模异味。

    根治「number_of_female_clients = COUNT(...) FILTER (WHERE gender='F')」
    这类把维度值拆成 N 条 metric 的录入:枚举值应留在维度字段
    (semantic_role=enum + enum_display),查询时走 conditions 维度过滤,
    而不是 metric 内建 filter。命中 → issue(建模期拦截,不改 SQL)。
    """
    ftext = str(m.get("filter") or "").strip()
    if not ftext:
        return
    name = str(m.get("name", ""))
    tree = _parse(ftext, dialect)
    if tree is None:
        return
    bound = [t for t in (m.get("datasets") or []) if t]
    for eq in tree.find_all(exp.EQ):
        col = eq.left if isinstance(eq.left, exp.Column) else None
        if col is None:
            continue
        col_name = (col.name or "").strip().lower()
        col_table = (col.table or "").strip().lower()
        if col_table:
            if col_table in enum_fields and col_name in enum_fields[col_table]:
                _enum_baked_issue(issues, name, f"{col_table}.{col_name}")
        elif bound:
            for t in bound:
                if col_name in enum_fields.get(t, set()):
                    _enum_baked_issue(issues, name, f"{t}.{col_name}")
                    break
        else:
            for t, enums in enum_fields.items():
                if col_name in enums:
                    _enum_baked_issue(issues, name, f"{t}.{col_name}")
                    break


def _enum_baked_issue(issues: list[str], metric_name: str, field_ref: str) -> None:
    issues.append(
        f"指标「{metric_name}」把枚举值过滤写死进 metric(filter 对 {field_ref} "
        "做等值过滤)——值应留在维度字段(enum_display),查询走维度过滤;"
        "请改为值无关 metric + conditions,避免每个枚举值各建一条 metric"
    )


def _lint_metric_agg_time(
    issues: list[str], m: dict[str, Any], ds_fields: dict[str, set[str]],
    temporal_fields: dict[str, set[str]],
) -> None:
    """agg_time_dimension 必须是 metric 数据集内已声明的时间字段。"""
    ref = str(m.get("agg_time_dimension") or "").strip()
    if not ref:
        return
    name = str(m.get("name", ""))
    tbl = ref.split(".", 1)[0] if "." in ref else ""
    col = ref.split(".", 1)[1] if "." in ref else ref
    bound = [t for t in (m.get("datasets") or []) if t]
    if tbl:
        if tbl not in ds_fields or col not in ds_fields[tbl]:
            issues.append(f"指标「{name}」agg_time_dimension 引用不存在的列 {tbl}.{col}")
        elif col not in temporal_fields.get(tbl, set()):
            issues.append(f"指标「{name}」agg_time_dimension {tbl}.{col} 不是时间字段")
    else:
        cands = [t for t in (bound or list(ds_fields)) if col in ds_fields.get(t, set())]
        if len(cands) != 1:
            issues.append(
                f"指标「{name}」agg_time_dimension {col} 未唯一解析到声明字段")
        elif col not in temporal_fields.get(cands[0], set()):
            issues.append(
                f"指标「{name}」agg_time_dimension {cands[0]}.{col} 不是时间字段")


def _lint_metric_non_additive(
    issues: list[str], m: dict[str, Any], model: dict[str, Any],
) -> None:
    """non_additive 度量被其他度量表达式按名引用(再聚合)→ 警告。"""
    if not m.get("non_additive"):
        return
    name = str(m.get("name", ""))
    for other in model.get("metrics", []) or []:
        if not isinstance(other, dict):
            continue
        oname = str(other.get("name", ""))
        if oname == name:
            continue
        for dia in (other.get("expression") or {}).get("dialects", []) or []:
            if name.lower() in str(dia.get("expression", "")).lower():
                issues.append(
                    f"non_additive 指标「{name}」被指标「{oname}」的表达式引用,"
                    "可能造成重复计算/再聚合,请复核")
                return


def _lint_topics(
    issues: list[str], model: dict[str, Any],
    ds_fields: dict[str, set[str]], ds_names: set[str],
) -> None:
    """业务主题域的结构门(模型内 ``topics`` 段)。

    主题域是**收敛边界**:命中后 schema linking 的数据集锚定被限制在
    ``datasets`` 内。所以这里拦的每一条都是"边界本身不成立"的形态:

    1. 名字为空/重复 —— 主题域靠名字选择,重名让选择有歧义;
    2. ``datasets`` 缺失或空 —— 空边界会把该主题下的**所有**问题收敛成
       零锚定拒绝(不是"不限制"),几乎必然是建模事故而非本意;
    3. 引用未声明的 dataset —— 悬空引用会让边界比作者以为的窄,且窄得
       无从解释(锚定永远命中不了它);
    4. ``metrics`` 引用未声明的 metric —— 同上;
    5. metric 锚定到主题外的 dataset —— 该 metric 在主题内必然 MISS
       (锚定越界),属于自相矛盾的边界;
    6. synonyms/examples 的形状与字段同款(空串/非字符串是坏形状)。

    ``topics`` 缺省(存量模型)不报 —— 不启用主题域是合法状态。
    """
    raw_topics = model.get("topics")
    if raw_topics is None:
        return
    if not isinstance(raw_topics, list):
        issues.append(f"topics 必须是数组: {raw_topics!r}")
        return
    # metric 的锚定数据集**从表达式推**(与装载侧 parse_ossie._dataset_refs 同
    # 一个函数、同一份 declared 集合)—— 原始 YAML 里 metric 并不落 datasets
    # 键,读显式键会把每条 metric 都判成"无锚定",于是"主题外锚定"这条门
    # 静默失效。
    metric_datasets: dict[str, list[str]] = {}
    for m in model.get("metrics", []) or []:
        if not (isinstance(m, dict) and m.get("name")):
            continue
        anchor: list[str] = []
        for dia in (m.get("expression") or {}).get("dialects", []) or []:
            expr = str(dia.get("expression") or "")
            if expr:
                for d in _dataset_refs(expr, ds_names):
                    if d not in anchor:
                        anchor.append(d)
        metric_datasets[str(m["name"])] = anchor
    seen: set[str] = set()
    for t in raw_topics:
        if not isinstance(t, dict):
            issues.append(f"topics 条目必须是映射: {t!r}")
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            issues.append("主题域缺少 name")
            continue
        if name in seen:
            issues.append(f"主题域「{name}」重复定义")
        seen.add(name)
        for key in ("synonyms", "examples"):
            for v in t.get(key) or []:
                if not isinstance(v, str) or not v.strip():
                    issues.append(f"主题域「{name}」的 {key} 含空/非法条目: {v!r}")
        datasets = t.get("datasets")
        if not isinstance(datasets, list) or not datasets:
            issues.append(
                f"主题域「{name}」未声明任何数据集 —— 主题域是收敛边界,"
                "空边界会把该主题下的全部问题收敛成零锚定拒绝;"
                "请声明它覆盖的 dataset(或删除该主题域)")
            continue
        for d in datasets:
            if str(d) not in ds_names:
                issues.append(f"主题域「{name}」引用未声明的数据集 {d}")
        declared = {str(d) for d in datasets}
        for metric in t.get("metrics") or []:
            mname = str(metric)
            if mname not in metric_datasets:
                issues.append(f"主题域「{name}」引用未声明的指标 {metric}")
                continue
            outside = [d for d in metric_datasets[mname] if d not in declared]
            if outside:
                issues.append(
                    f"主题域「{name}」的指标「{mname}」锚定到主题外的数据集 "
                    f"{', '.join(sorted(outside))} —— 该指标在主题内必然 MISS"
                    "(请把它锚定的数据集并入本主题,或从本主题移除该指标)")
        for ext in t.get("custom_extensions") or []:
            if not (isinstance(ext, dict) and str(ext.get("vendor_name") or "").strip()):
                issues.append(f"主题域「{name}」custom_extensions 缺 vendor_name")


def _raw_name_list(raw: Any) -> list[str]:
    """名字列表的字形净化:保序**不去重**(``_clean_names`` 减掉去重)。

    重复是字形事实 —— 装载侧 ``_clean_names`` 保序去重会让「级别重复」
    这条规则没得判(解析产物里只剩一份),所以 lint 看原始序列。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for v in raw:
        if v is None or isinstance(v, (dict, list, tuple, set)):
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out


def _dataset_synonyms(d: dict[str, Any]) -> Any:
    """数据集 ``ai_context.synonyms`` 的原始字形(与装载侧 ``_ai_context``
    同一个来源 —— 词法锚定的既有词汇)。"""
    ai = d.get("ai_context")
    if not isinstance(ai, dict):
        return None
    return ai.get("synonyms")


def _lint_entities(
    issues: list[str], model: dict[str, Any],
    ds_fields: dict[str, set[str]], ds_names: set[str],
) -> None:
    """业务实体的结构门 + 语义缺口提示(设计 §4.1,拍板 Q4)。

    「对象 = 已声明实体的数据集」,实体挂在数据集 ``entity`` 子块下。这里
    拦的每一条都是"声明本身不成立"的形态;拍板 Q4 把它们分两级:

    结构性 error(**拦写盘** —— 与「坏派生定义才是硬 MISS」同一条边界):
    1. ``entity`` 坏形状 —— 装载侧整体丢弃(不猜),声明了却不在模型里;
    2. ``natural_keys`` 引用本数据集未声明的字段 —— 照收,但自然键消解
       (等值)拿它去比对永远命中不了;
    3. ``aliases`` 坏形状 —— 只留声明得清楚的,坏形状是写错而不是缺省;
    4. 归属数据集没有 name(防御性,正常不可达)—— 装载侧会连数据集带
       实体整条丢弃。

    语义缺口 warning(**提示但不阻断**):
    5. 未声明 natural_keys 且数据集无 ``primary_key`` 可回退 —— 回退链
       两头都空,对象消解无从进行;
    6. aliases 与自身 synonyms 重复、或与其它数据集的 synonyms/别名重复
       —— 词法锚定互相抢命中(E4 把 aliases 与 synonyms 并入同一通道)。

    ``entity`` 缺席(None)不报 —— 不启用对象视图是合法状态,存量模型
    逐字节同前。
    """
    datasets = [d for d in (model.get("datasets") or []) if isinstance(d, dict)]
    # 词法锚定面的**已有**词汇:各数据集 synonyms + 各实体 aliases(大小写
    # 不敏感比较,消息里保留原始拼写)。重复判定必须确定:按数据集声明序
    # setdefault,命中后只报"后来者"(同一对只报一次)。
    vocab: dict[str, str] = {}
    for d in datasets:
        owner = str(d.get("name") or "")
        for term in _raw_name_list(_dataset_synonyms(d)):
            vocab.setdefault(term.casefold(), owner)
    for d in datasets:
        raw_entity = d.get("entity")
        if not isinstance(raw_entity, dict):
            continue
        for term in _raw_name_list(raw_entity.get("aliases")):
            vocab.setdefault(term.casefold(), str(d.get("name") or ""))
    for d in datasets:
        ds_name = str(d.get("name") or "")
        raw_entity = d.get("entity")
        if raw_entity is None:
            continue
        if not ds_name or ds_name not in ds_names:
            # 防御性:正常不可达(遍历的就是声明过的数据集)。触发的唯一
            # 现实形态是数据集没写 name —— 装载侧会整条丢弃(含实体),
            # 声明了却不在模型里,静默失败,所以拦下。
            issues.append(
                f"数据集「{ds_name}」的 entity 声明无处安放:数据集缺少 name,"
                "装载侧会把整条数据集(含实体声明)丢弃")
            continue
        if not isinstance(raw_entity, dict):
            issues.append(
                f"数据集「{ds_name}」的 entity 声明必须是映射: {raw_entity!r}"
                "(坏形状会被装载侧整体丢弃 —— 声明了却不在模型里)")
            continue
        declared = ds_fields.get(ds_name, set())
        raw_keys = raw_entity.get("natural_keys")
        if raw_keys is not None and not isinstance(raw_keys, (str, list, tuple)):
            issues.append(
                f"数据集「{ds_name}」的实体 natural_keys 必须是名字数组: "
                f"{raw_keys!r}")
            keys: list[str] = []
        else:
            keys = _raw_name_list(raw_keys)
        for key in keys:
            if key not in declared:
                issues.append(
                    f"数据集「{ds_name}」的实体自然键「{key}」不是本数据集"
                    "已声明字段(自然键等值消解拿它去比对永远命中不了)")
        if not keys and not d.get("primary_key"):
            issues.append(
                f"数据集「{ds_name}」声明了实体但未声明自然键,且数据集无 "
                "primary_key 可回退 —— 对象消解(自然键等值)无从进行,"
                "请补 natural_keys 或 primary_key")
        raw_aliases = raw_entity.get("aliases")
        if raw_aliases is not None and not isinstance(
            raw_aliases, (str, list, tuple)
        ):
            issues.append(
                f"数据集「{ds_name}」的实体 aliases 必须是名字数组: "
                f"{raw_aliases!r}")
            continue
        own_synonyms = {
            t.casefold() for t in _raw_name_list(_dataset_synonyms(d))}
        for alias in _raw_name_list(raw_aliases):
            key = alias.casefold()
            if key in own_synonyms:
                issues.append(
                    f"数据集「{ds_name}」的实体别名「{alias}」与自身 synonyms "
                    "重复(声明两次不会让词法锚定更准)")
                continue
            other = vocab.get(key, "")
            if other and other != ds_name:
                issues.append(
                    f"数据集「{ds_name}」的实体别名「{alias}」与数据集"
                    f"「{other}」的同义词/别名重复 —— 词法锚定会互相抢命中,"
                    "请删掉重复项或把它归给真正拥有它的对象")


def _lint_hierarchies(
    issues: list[str], model: dict[str, Any],
    ds_fields: dict[str, set[str]], ds_names: set[str],
) -> None:
    """钻取层级的结构门 + 语义缺口提示(设计 §4.3,拍板 Q4)。

    层级是**级别字段的有序链**(粗 → 细),模型级 ``hierarchies`` 段。
    形态模板同 ``_lint_topics``:缺席即合法、坏形状不猜、逐条报。

    结构性 error(**拦写盘**):
    1. ``hierarchies`` 不是数组 / 条目不是映射 —— 装载侧丢弃;
    2. 缺少 name —— 装载侧按空名丢弃整条;
    3. name 重复 —— 层级靠名字被引用,重名让引用有歧义;
    4. 未声明归属 dataset / dataset 不存在 —— 级别字段无处解析;
    5. 级别字段不在归属数据集的 fields 里 —— 层级链在任何一跳都会断
       (拍板 Q4 的「解析不到」一侧)。

    语义缺口 warning(**提示但不阻断**):
    6. 解析后级别少于 2 个 —— 单级链条没有可钻的下一级;
    7. 级别重复 —— 装载侧保序去重,重复多半是写错。

    ``hierarchies`` 缺席(存量模型)不报 —— 不启用层级是合法状态。
    """
    raw_hierarchies = model.get("hierarchies")
    if raw_hierarchies is None:
        return
    if not isinstance(raw_hierarchies, list):
        issues.append(f"hierarchies 必须是数组: {raw_hierarchies!r}")
        return
    seen: set[str] = set()
    for h in raw_hierarchies:
        if not isinstance(h, dict):
            issues.append(f"hierarchies 条目必须是映射: {h!r}")
            continue
        name = str(h.get("name") or "").strip()
        if not name:
            issues.append("钻取层级缺少 name")
            continue
        if name in seen:
            issues.append(f"层级「{name}」重复定义")
        seen.add(name)
        raw_levels = h.get("levels")
        if raw_levels is not None and not isinstance(
            raw_levels, (str, list, tuple)
        ):
            issues.append(
                f"层级「{name}」的 levels 必须是名字数组: {raw_levels!r}")
            continue
        levels = _raw_name_list(raw_levels)
        for i, level in enumerate(levels):
            if level in levels[:i]:
                issues.append(
                    f"层级「{name}」的级别「{level}」重复(同一级别只写一次)")
        # 长度判**解析后**的级别数(装载侧保序去重);[a, a] 的重复已被上面
        # 的级别重复警告点名,不再对同一个笔误叠报一次"单级链条"(修掉重复
        # 它就是一条两级链,提示指向同一个修法)。
        parsed_levels = list(dict.fromkeys(levels))
        if len(parsed_levels) < 2 and len(levels) == len(parsed_levels):
            issues.append(
                f"层级「{name}」解析后只有 {len(parsed_levels)} 个级别 —— "
                "单级链条没有可钻的下一级(层级至少两级,粗 → 细)")
        ds = str(h.get("dataset") or "").strip()
        if not ds:
            issues.append(
                f"层级「{name}」未声明归属数据集 dataset —— 级别字段无处解析")
        elif ds not in ds_names:
            issues.append(
                f"层级「{name}」声明的数据集「{ds}」不存在"
                "(级别字段无处解析,请先声明该数据集或更正 dataset)")
        else:
            declared = ds_fields.get(ds, set())
            for level in parsed_levels:
                if level not in declared:
                    issues.append(
                        f"层级「{name}」的级别字段「{level}」不在数据集"
                        f"「{ds}」的 fields 中(层级链在这一跳解析不到)")


def _hierarchy_levels_by_dataset(
    model: dict[str, Any], ds_fields: dict[str, set[str]],
) -> dict[str, list[tuple[str, list[str]]]]:
    """层级级别字段索引(FR-HIER-3 的输入侧):dataset → [(层级名, [级别])]。

    只收**解析得动**的层级:条目须是映射、name/dataset 非空、dataset 已声明、
    级别字段在 dataset 的 fields 里(悬空引用已由 ``_lint_hierarchies`` 报错,
    这里不重复报,也不拿未声明字段去比对口径)。按声明序遍历、保序去重级别
    (与装载侧 ``_clean_names`` 同一净化),输出与 PYTHONHASHSEED 无关。
    """
    out: dict[str, list[tuple[str, list[str]]]] = {}
    for h in model.get("hierarchies") or []:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip()
        ds = str(h.get("dataset") or "").strip()
        if not name or ds not in ds_fields:
            continue
        declared = ds_fields.get(ds, set())
        levels = [lv for lv in dict.fromkeys(_raw_name_list(h.get("levels")))
                  if lv in declared]
        if levels:
            out.setdefault(ds, []).append((name, levels))
    return out


def _level_col_name(col: exp.Expression) -> str:
    """谓词左端的列名(非 Column 结构 → 空串,调用方跳过)。"""
    if isinstance(col, exp.Column):
        return (col.name or "").strip()
    return ""


def _lint_metric_hierarchy_coverage(
    issues: list[str], m: dict[str, Any],
    levels_by_ds: dict[str, list[tuple[str, list[str]]]],
    temporal_fields: dict[str, set[str]], dialect: str,
) -> None:
    """口径覆盖检查(FR-HIER-3):metric 的过滤/派生条件把层级收窄到了某一支。

    判据(确定性、零 LLM):metric 的**内建 filter** 或**表达式**里,对某条
    已声明层级的级别字段做等值(``=``)/不等(``!=``)/``IN``(含 ``NOT IN``)
    —— 谓词把口径钉在一个/一组级别值上,层级其余分支被**静默排除**
    (需求原文的样例:贷款总额排除个人贷款)。命中按 (层级, 级别) 逐条报,
    消息带层级名与级别名。

    刻意排除的两侧:

    * **顺序比较不算**(``>=``/``<`` 等):它不"收敛"到具名支,且时间窗口
      类的惯用写法正落在这一侧;
    * **级别字段是时态字段不报**(N35 边界):时间等值是「时间锚」不是
      「层级分支」,报出来是误报 —— 层级级别本就以维度/枚举字段为主。

    级别列解析规则与 ``_lint_metric_filter_enum`` 同款:带表限定必须落在该
    表上;裸列落 metric 的绑定数据集内;无绑定则按声明序在全部数据集里取
    首个命中(确定性)。**warning 起步、不阻断**(拍板:语义缺口提示),豁免
    登记在 ``issues._ADVISORY_CODES``。
    """
    if not levels_by_ds:
        return
    texts: list[str] = []
    ftext = str(m.get("filter") or "").strip()
    if ftext:
        texts.append(ftext)
    for dia in (m.get("expression") or {}).get("dialects", []) or []:
        if isinstance(dia, dict):
            expr = str(dia.get("expression") or "").strip()
            if expr:
                texts.append(expr)
    if not texts:
        return
    name = str(m.get("name", ""))
    bound = [t for t in (m.get("datasets") or []) if t]
    seen: set[tuple[str, str]] = set()

    def _hits(ds: str, col_name: str) -> list[tuple[str, str]]:
        """该列在 dataset 上命中的 (层级名, 级别字段) 列表(声明序)。

        时态级别字段直接零命中 —— 时间等值是「时间锚」不是「层级分支」
        (N35 边界);大小写不敏感(与装载侧/编译器的列名口径同款)。
        """
        lower = col_name.lower()
        if lower in {str(t).lower() for t in temporal_fields.get(ds, set())}:
            return []
        return [
            (hname, lv)
            for hname, levels in levels_by_ds.get(ds, [])
            for lv in levels
            if lv.lower() == lower
        ]

    for text in texts:
        tree = _parse(text, dialect)
        if tree is None:
            continue  # 解析失败已由 filter_parse / expr_parse 报出
        predicates: list[exp.Expression] = []
        for eq in tree.find_all(exp.EQ, exp.NEQ):
            predicates.append(eq.left)
        for inn in tree.find_all(exp.In):
            predicates.append(inn.this)
        for node in predicates:
            col_name = _level_col_name(node)
            if not col_name:
                continue
            col_table = (node.table or "").strip()
            if col_table:
                candidates = [t for t in levels_by_ds if t.lower() == col_table.lower()]
            elif bound:
                candidates = [t for t in bound if t in levels_by_ds]
            else:
                candidates = list(levels_by_ds)  # 声明序
            for ds in candidates:
                hits = _hits(ds, col_name)
                if not hits:
                    continue
                for hname, lv in hits:
                    key = (hname, lv)
                    if key in seen:
                        continue
                    seen.add(key)
                    issues.append(
                        f"指标「{name}」的过滤/派生条件对层级「{hname}」的级别字段"
                        f"「{lv}」做等值/IN 收敛 —— 该口径静默排除层级其他分支"
                        "(下钻/对比只覆盖被钉住的那一支);如非刻意,请改用维度"
                        "过滤,或单列独立指标并写明口径")
                # 首个命中的数据集(与 _lint_metric_filter_enum 同款判定);
                # 裸列不跨表连报 —— 一条谓词只对一处口径说话。
                break


def _is_many_to_many(cardinality: Any) -> bool:
    """基数归一化判定(与 semantic_layer.compiler 同构):任意 M:N 拼写
    (``M:N``/``many-to-many``/``MANY-TO-MANY``/``M2M``)一律视为多对多。"""
    c = str(cardinality or "").strip().upper().replace("_", " ").replace("-", " ")
    c = "".join(c.split())  # 去全部空白("M:N" 保留冒号)
    return c in {"MN", "M:N", "N:M", "M2M", "MANYTOMANY", "MANY:MANY"}


def _lint_path_ambiguity(
    issues: list[str], model: dict[str, Any], ds_names: set[str],
) -> None:
    """声明关系图上两「事实/枢纽」数据集之间多条简单路径 → 编译期可能
    ambiguous MISS 预警。

    与编译器 JoinResolver 的 ``route_capable`` 语义对齐(compiler.py):
    纯 1:N 维度叶(只作 to 端、自身无 FK,如 BIRD 的 district)是星型
    共享维度——account/client 各自 FK 它,经它绕行 = 共享维度二次进入,
    编译器按「查询所需表(needed)」作用域视为**虚假路由**不计入二义。
    因此这里:
      - 端点只取 route_capable 表(作过 many→one from 端、或 M:N 的 to
        端)——纯维度叶(如 district)不参与计数,star 共享维度不误报;
      - 路径中间节点也必须 route_capable 才计数——绕经纯维度叶的第二
        路由不算,正确 BFS 树不会被误判。
    只有「两个事实/枢纽表之间存在 ≥2 条仅经事实/枢纽表中转的简单路径」
    (真菱形,如 loan—account—client 且 account/client 各自拥有 FK)才预警。
    图小,直接 BFS 计数简单路径(深度上限防爆);无查询上下文,这正是
    运行时按 needed 作用域可放行的最贴近近似。
    """
    route_capable: set[str] = set()
    adj: dict[str, list[str]] = {}
    for r in model.get("relationships", []) or []:
        if not isinstance(r, dict):
            continue
        a, b = str(r.get("from", "")), str(r.get("to", ""))
        if a not in ds_names or b not in ds_names:
            continue
        route_capable.add(a)  # from 端 = 自身拥有 FK → 事实/枢纽表
        if _is_many_to_many(r.get("cardinality")):
            route_capable.add(b)
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    if not adj:
        return

    def paths(u: str, v: str, seen: frozenset[str], depth: int) -> int:
        if depth > 12:
            return 0
        if u == v:
            return 1
        n = 0
        for nb in adj.get(u, []):
            if nb in seen:
                continue
            if nb != v and nb not in route_capable:
                continue  # 纯维度叶不能作中间路由(共享维度二次进入)
            n += paths(nb, v, seen | {u}, depth + 1)
            if n > 1:
                return n  # 只需知道 >1
        return n

    reported: set[frozenset[str]] = set()
    nodes = sorted(route_capable)  # 端点限事实/枢纽表,纯维度叶对不检查
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if frozenset((a, b)) in reported:
                continue
            if paths(a, b, frozenset(), 0) > 1:
                reported.add(frozenset((a, b)))
                issues.append(
                    f"关系图上 {a}↔{b} 存在多条简单路径,编译器将拒绝二义"
                    "(ambigous_join_path)——请收敛建模(仅声明必要边)")


def lint_tables(tables: list[dict[str, Any]]) -> list[str]:
    """Columns with empty descriptions are knowledge blind spots.

    入参与 _parse_file 的 schema_notes payload 同形:columns 为
    {列名: 描述} 字典,表名在 "name" 字段。
    """
    issues: list[str] = []
    for table in tables:
        table_name = str(table.get("name", ""))
        for col_name, desc in (table.get("columns") or {}).items():
            if not str(desc or "").strip():
                issues.append(
                    f"表 {table_name} 的列 {col_name} 描述为空"
                    "(盲区,建议从官方文档导入或标注含义未知)")
    return issues


_STATS_KEYS = {
    "null_ratio", "distinct", "min", "max", "min_len", "max_len", "shape",
    "top_values", "sample",
}
_SHAPES = {"numeric", "json", "composite", "all_caps", "capital", "text"}


def lint_stats(tables: list[dict[str, Any]]) -> list[str]:
    """Profiling stats 格式校验:未知键、取值域、形状枚举。

    tables 形如 _parse_file 的 table payload:{"name", "stats": {列名: dict}}。
    统计是 LLM 描述的证据源,格式错了会静默污染 schema_context。
    """
    issues: list[str] = []
    for table in tables:
        table_name = str(table.get("name", ""))
        for col_name, st in (table.get("stats") or {}).items():
            if not isinstance(st, dict):
                issues.append(f"表 {table_name} 列 {col_name} stats 不是对象")
                continue
            unknown = set(st) - _STATS_KEYS
            if unknown:
                issues.append(
                    f"表 {table_name} 列 {col_name} stats 含未知键: {sorted(unknown)}")
            nr = st.get("null_ratio")
            if nr is not None and not (
                isinstance(nr, (int, float)) and 0 <= nr <= 1
            ):
                issues.append(f"表 {table_name} 列 {col_name} null_ratio 超出 [0,1]: {nr}")
            d = st.get("distinct")
            if d is not None and not (isinstance(d, int) and d >= 0):
                issues.append(f"表 {table_name} 列 {col_name} distinct 非法: {d}")
            mn, mx = st.get("min_len"), st.get("max_len")
            if mn is not None and mx is not None and mn > mx:
                issues.append(f"表 {table_name} 列 {col_name} min_len > max_len")
            shape = st.get("shape")
            if shape is not None and shape not in _SHAPES:
                issues.append(f"表 {table_name} 列 {col_name} shape 未知: {shape}")
            tv = st.get("top_values")
            if tv is not None:
                if (
                    not isinstance(tv, list)
                    or not tv
                    or any(
                        not isinstance(p, list) or len(p) != 2 or not isinstance(p[1], int)
                        for p in tv
                    )
                ):
                    issues.append(
                        f"表 {table_name} 列 {col_name} top_values 非法"
                        "(应为 [[值, 频次], ...])")
            sample = st.get("sample")
            if sample is not None and (
                not isinstance(sample, list) or not sample
                or any(v is None for v in sample)
            ):
                issues.append(
                    f"表 {table_name} 列 {col_name} sample 非法"
                    "(应为非空取值列表)")
    return issues


def lint_lessons(lessons: list[dict[str, Any]]) -> list[str]:
    """Lessons must carry a short, retrievable pattern and a non-empty note."""
    issues: list[str] = []
    for lesson in lessons:
        pattern = str(lesson.get("pattern", "") or "").strip()
        note = str(lesson.get("note", "") or "").strip()
        if len(pattern) > MAX_PATTERN_LEN:
            issues.append(
                f"lesson pattern 过长({len(pattern)}>{MAX_PATTERN_LEN}): {pattern[:40]}…")
        if not note:
            issues.append(f"lesson「{pattern[:30]}」note 为空")
    return issues
