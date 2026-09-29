"""字段级脱敏 —— 结果集后置改写(设计 §5.5 / §8.3,G4 · 验收 A5/A6/A7)。

**为什么在后置**。设计 §8.3 比过三种做法,选了"结果集后置脱敏":在 SQL 里把
``SELECT id_card`` 改写成 ``SELECT NULL`` 会让"按身份证去重计数"这类**合法
聚合**失效;提示词约束模型(方案 A)则违反 I4 且完全无效。后置改写保住了两件事:
``hash`` 同值同哈希 → ``COUNT(DISTINCT …)`` 语义不变(A6),``partial`` 保留
人工核对能力。代价是**模型看到的是脱敏后的行** —— 这正是要的:``insights`` /
``conclusion`` 的输入里不会出现身份证号,模型也就无从复述。

四种模式
--------

=====  ==========================================  ==========================
``none`` / ``""``  原样                                普通字段(默认)
``partial``        保留首尾、中间填 ``*``               手机号/邮箱/卡号
``hash``           ``sha256(salt + 值)[:12]``          身份证:可分组,不可还原
``null``           恒 ``None``                          高敏列
=====  ==========================================  ==========================

``partial`` 的短值规则定死在 :func:`_partial`:极短值(≤2)整体遮蔽 —— 留首尾在
两个字符上等于不遮。``""`` 与 ``none`` 同义(存量兼容,A11):缺省不是"有效果"
的值,一个没声明过脱敏的模型不该在升级后开始改写结果。

列匹配(**本模块最容易做错的地方**)
----------------------------------

1. **声明了脱敏的字段** = ``mask`` 落在 ``partial`` / ``hash`` / ``null`` 的字段。
   名字规范化见 :func:`_norm_ident`:去空白、去引号/反引号/方括号、取 ``.``
   最后一段、小写。
2. **有投影位置信息时只用位置匹配。** 位置优先取 ``contract``(编译期抽的权威
   结构,``signature_to_wire`` 形状),没有才用 ``sql`` 经 sqlglot 解析投影表。
   位置信息在场时,**"输出列名恰好叫 phone"不构成脱敏依据** —— 否则
   ``SELECT email AS phone`` 会按 phone 的模式改写 email 的值。
3. **只有纯列引用(可带别名)的位置才可能被匹配。** 函数/聚合投影
   (``COUNT(DISTINCT phone)``、``LOWER(email)``)一律**不**匹配:掩码一个计数
   是错的(§8.3 选后置脱敏就是为了保住这类聚合),掩码一个函数结果则等于拿
   未经声明的值形状做声明之外的事。这是有意的边界,不是遗漏。
4. **没有位置信息时退化为输出列名匹配**(列名 vs 字段 ``name`` 或 ``expression``
   末段)。**位置信息是增强,不是前提** —— sqlglot 解析失败(方言不支持/语法
   残缺)时**不抛异常**,静默退化为列名匹配:解析不了 ≠ 判定不通过,这条门
   的产物是"改写过的行",漏改才是坏事,而退化路径**仍然在改**。
5. **同一位置命中多个声明且模式不同 → 取最严格**(``partial`` < ``hash`` <
   ``null``)。有表限定且能对上某个数据集时先按表收窄(别名对不上就退回按列名
   全部命中,方向仍是取严)。

值处理
------

``None`` 在所有模式下保持 ``None`` —— 它是"没有值",不是"值里的 PII";把它
变成 ``"***"`` / 哈希 / 空串会让下游读不出"这行没有值"。非字符串值先 ``str()``
再进 ``partial`` / ``hash``:**掩码列是展示值,类型变化是有意的**(手机号可能
以 int 回来,身份证可能是 str;同值必须同哈希,否则分组统计被拆开)。

失败方向(设计 §8.1 判据 / §10)
------------------------------

**``hash`` 要应用而 salt 解析不出来 → 拒绝**(:class:`MaskingError`)。salt
缺失时降级为"原样返回"就是明文泄漏(R6:salt 不参与则彩虹表可还原手机号),而
PII 披露**没有下层兜底** —— 判据与 RLS 注入失败、执行层授权门一致:没兜底的
方向必须严。没有 ``hash`` 字段要应用时不解析 salt:只有 ``partial`` 的部署
不该因为没配 salt 而全线拒绝。

scope 判定见 :func:`trove.services.authz.policy.may_bypass_masking`:持
``masking.bypass_scopes`` 里任一 scope → 原样;否则 ``default_policy ==
"bypass"`` → 原样。**admin 不自动 bypass**(§8.4),**空 scopes 不是"不限"**
(§5.6:否则每个存量 token 和每次 ``on_behalf_of`` 重放都直接看原文)。

返回形状
--------

``apply`` 返回 ``(rows, report)``,``report = {"fields": {字段名: 模式},
"bypass": bool}``。比设计 §6.2 的速记 ``{field: mode}`` 多一个 ``bypass``
位:§6.3 的审计动作里 ``masking.applied`` 与 ``masking.bypass`` 是两条**不同**
的记录,只给 ``{}`` 的话调用方分不清"没有可脱敏的字段"和"整个脱敏被跳过了"。
``fields`` 只记**实际改写过的**字段(按声明里的字段名),不记值(§6.3)。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Literal

import sqlglot
from sqlglot import exp

from trove.core.logging import get_logger
from trove.services.authz.policy import Policy, Principal
from trove.services.semantic_layer.models import SemanticModel

__all__ = ["Masker", "MaskingError", "resolve_salt_ref"]

log = get_logger(__name__)

#: salt 引用的唯一 scheme:``env:NAME`` —— 值走环境变量/secrets,不进 YAML。
ENV_SCHEME = "env:"

#: 声明了脱敏的模式(``""`` / ``none`` 不算声明)。字典序即严格度,见
#: :func:`_strictest`。
STRICTNESS = {"partial": 0, "hash": 1, "null": 2}

#: hash 模式输出的长度(设计 §5.5:``sha256(salt + value)[:12]``)。
HASH_CHARS = 12


class MaskingError(RuntimeError):
    """脱敏**要应用却拿不到依据** —— ``hash`` 模式解析不出 salt(设计 §10)。

    继承 ``RuntimeError``:这是运行期的配置/环境故障,不是调用方的参数错误。
    绝不降级为明文(§10:降级即泄漏),也绝不静默跳过 —— 往上抛,由调用方
    按失败方向处理。
    """


def resolve_salt_ref(ref: str) -> str:
    """salt 引用 → salt 明文;解析不出抛 :class:`MaskingError`。

    支持的形状只有一条:``env:NAME`` → ``os.environ[NAME]``。**不认识的形状
    一律拒绝**,不做"猜一个"的兼容 —— 一个拼错的引用静默解析成空 salt,
    等于给所有部署装同一个可被彩虹表打穿的盐(R6)。

    本函数是**唯一的解析点**:部署要改成从 KMS / vault 取盐,替换这一个函数
    即可(测试也可以 monkeypatch 它,见 ``test_masking.py``)。异常信息里只出现
    **引用**(环境变量名),不出现 salt 值。
    """
    text = str(ref or "").strip()
    if not text:
        raise MaskingError("hash 脱敏需要 salt,但没有可用的 salt 引用(模型级与部署级都为空)")
    if not text.startswith(ENV_SCHEME):
        raise MaskingError(f"不认识的 salt 引用形状: {text!r}(只支持 env:NAME)")
    name = text[len(ENV_SCHEME):].strip()
    value = os.environ.get(name, "") if name else ""
    if not value:
        raise MaskingError(f"salt 引用 {text!r} 解析不出值(环境变量未设置或为空)")
    return value


def _norm_ident(raw: Any) -> str:
    """标识符 → 匹配键:去空白、去引号/反引号/方括号、取末段、小写。

    ``"`CUSTOMERS`.`Phone`"`` / ``"[Phone]"`` / ``"public.customers.Phone"`` 都
    归一成 ``phone``。**取末段**是必要的:字段 ``expression`` 常常写成
    ``customers.phone``,而投影里的列可能是裸 ``phone``(或反过来)。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    last = text.rsplit(".", 1)[-1].strip()
    while len(last) >= 2 and last[0] in '"`[' and last[-1] in '"`]':
        last = last[1:-1].strip()
    return last.lower()


def _partial(value: str) -> str:
    """保留首尾、中间 ``*``;短值规则确定(见 :class:`Masker` docstring)。

    分档而不是按比例:``13800138000`` → ``138****8888`` 是设计 §5.5 写下的
    形状,按比例算不出来。``n >= 11`` 才用 (3, 4) 这一档 —— 8 个字符里露出去
    7 个不叫脱敏。``n <= 2`` 整体遮蔽:留首尾等于不遮。
    """
    width = len(value)
    if width == 0:
        return value
    if width <= 2:
        return "*" * width
    head, tail = (3, 4) if width >= 11 else (1, 1)
    return value[:head] + "*" * (width - head - tail) + value[-tail:]


def _hash_value(value: str, salt: str) -> str:
    """``sha256(salt + value)[:12]`` —— 公式定死(设计 §5.5),别"顺手改进"。

    没有分隔符:盐是部署级固定的,歧义只在不同盐之间才可能出现,而那种情况
    本来就不需要可比。同盐同值必同哈希是 A6(可分组统计)的全部依据。
    """
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()[:HASH_CHARS]


@dataclass(frozen=True)
class _Declared:
    """一个声明了脱敏的字段(匹配用的规范化键 + 审计用的原始名字)。"""

    name: str
    mode: str
    #: 列名匹配键:字段 ``name`` 与 ``expression`` 末段
    keys: frozenset[str]
    #: 表匹配键:所属数据集的 ``name`` 与 ``source`` 末段
    tables: frozenset[str]


def _declared_fields(model: SemanticModel) -> list[_Declared]:
    """模型里所有声明了脱敏的字段。

    非法 ``mask`` 值在这里**拒绝**(§10 的方向:lint 是写前门禁,运行时是兜底;
    把 ``partital`` 当成"没声明"放行,等于"以为脱敏了其实没有")。
    """
    out: list[_Declared] = []
    for dataset in model.datasets or ():
        tables = frozenset(
            key for key in (_norm_ident(dataset.name), _norm_ident(dataset.source)) if key
        )
        for field in dataset.fields or ():
            mode = str(getattr(field, "mask", "") or "").strip().lower()
            if not mode or mode == "none":
                continue
            if mode not in STRICTNESS:
                raise MaskingError(
                    f"字段 {dataset.name}.{field.name} 的 mask 声明非法: {field.mask!r}"
                    f"(合法值: none | partial | hash | null)"
                )
            keys = frozenset(
                key for key in (_norm_ident(field.name), _norm_ident(field.expression)) if key
            )
            out.append(_Declared(name=field.name, mode=mode, keys=keys, tables=tables))
    return out


def _contract_projections(contract: Any) -> list | None:
    """契约 wire → 投影列表(``[fn, [[表, 列], ...]]``)。

    同时接受整份契约 wire(``{"signature": {"projections": ...}}``)与直接给出的
    签名 wire(``{"projections": ...}``) —— 调用方手里是哪一份由链路决定,
    两种形状都是本仓库的既有 wire(``signature_to_wire`` / ``contract_to_wire``),
    多认一种不放松任何判定。形状坏了一律 ``None``(= 这份位置信息不可用)。
    """
    if not isinstance(contract, dict):
        return None
    signature = contract.get("signature")
    if isinstance(signature, dict):
        raw = signature.get("projections")
    elif "projections" in contract:
        raw = contract.get("projections")
    else:
        return None
    return raw if isinstance(raw, list) else None


def _contract_slot(item: Any) -> tuple[str, str] | None | Literal["invalid"]:
    """一个契约投影 → 位置槽。

    三态(与 ``contract.py`` 的 ``_decode_signature`` 同款):
    ``(表, 列)`` = 纯列引用,可匹配;``None`` = **位置有效但不可匹配**
    (聚合/函数/多列表达式 —— 有意的边界);``"invalid"`` = 形状坏了,整份位置
    信息作废(部分相信一份对不上号的位置表,比不用它更危险)。
    """
    if not (isinstance(item, list) and len(item) == 2):
        return "invalid"
    fn, cols = item
    if fn is not None:
        return None
    if not isinstance(cols, list):
        return "invalid"
    if len(cols) != 1:
        # 空列集(COUNT(*))与多列表达式(a || b)都不是一个列身份
        return None
    col = cols[0]
    if not (isinstance(col, list) and len(col) == 2):
        return "invalid"
    return (str(col[0] or ""), str(col[1] or ""))


def _positions_from_contract(contract: Any, width: int) -> list[tuple[str, str] | None] | None:
    raw = _contract_projections(contract)
    if raw is None or len(raw) != width:
        return None
    slots: list[tuple[str, str] | None] = []
    for item in raw:
        slot = _contract_slot(item)
        if slot == "invalid":
            return None
        slots.append(slot)
    return slots


def _is_star(node: Any) -> bool:
    """``*`` / ``t.*`` —— 投影表不再描述输出列(见 :func:`_positions_from_sql`)。"""
    return isinstance(node, exp.Star) or (
        isinstance(node, exp.Column) and isinstance(node.this, exp.Star)
    )


def _positions_from_sql(sql: str, width: int) -> list[tuple[str, str] | None] | None:
    """SQL 的投影表 → 位置槽;解析不了/对不上号一律 ``None``。

    **不把解析异常抛出去**:位置信息是增强不是前提(sqlglot 对方言/残缺语法的
    容忍度有限,而"解析不了"不等于"判定不通过" —— 这条门的产物是被改写过的
    行,退化路径仍然在改)。解析不出就退化为按列名匹配。

    这里比契约那条路**更严**:标量函数(``LOWER(email)``)在契约里只表现为
    "无聚合"(签名看不见函数),而 sqlglot 看得见,于是不匹配。

    ``SELECT *`` 一律判位置不可用:通配投影下的"表达式个数"与"输出列数"只是
    **偶然**相等(单列表恰好 1 对 1),把它当位置表会让退化路径失效 —— 于是
    ``SELECT *`` 的结果集里那个叫 phone 的列反而不会被脱敏。宁可退回按列名匹配。
    """
    if not sql or not isinstance(sql, str):
        return None
    try:
        tree = sqlglot.parse_one(sql)
    except Exception as exc:  # noqa: BLE001 —— 解析器的异常类型不承诺稳定
        # 只记异常**类型**,不记消息:解析器的报错会引述出错的 SQL 片段,而
        # SQL 里的字面量可能就是 PII(``WHERE phone = '138…'``)。日志会被
        # 采集到日志平台,这个模块不该成为新的泄漏面。
        log.debug("masking: SQL 解析失败(%s),退化为列名匹配", type(exc).__name__)
        return None
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        return None
    expressions = select.expressions or []
    if len(expressions) != width:
        # 投影数与结果列数不一致(上游裁过列/契约来自另一条 SQL)→ 位置对不上号
        return None
    slots: list[tuple[str, str] | None] = []
    for expr in expressions:
        node = expr.this if isinstance(expr, exp.Alias) else expr
        if _is_star(node):
            return None
        if isinstance(node, exp.Column):
            slots.append((str(node.table or ""), str(node.name or "")))
        else:
            slots.append(None)   # 函数/聚合/字面量 → 位置有效但不可匹配
    return slots


class Masker:
    """按字段声明改写结果集(四种模式 + scope 判定 + salt 解析)。

    ``default_salt_ref`` 是**部署级兜底**引用(来自配置,如
    ``env:TROVE_MASK_SALT``);模型级 ``masking.hash_salt_ref`` 优先。两处都
    解析不出来而确有 ``hash`` 字段要应用 → ``MaskingError``(§10)。
    """

    def __init__(self, *, default_salt_ref: str = "") -> None:
        self._default_salt_ref = default_salt_ref

    # ── 主入口 ─────────────────────────────────────────────────

    def apply(
        self,
        rows: list[list[Any]],
        columns: list[str],
        *,
        model: SemanticModel | None,
        principal: Principal | None,
        sql: str | None = None,
        contract: dict[str, Any] | None = None,
    ) -> tuple[list[list[Any]], dict[str, Any]]:
        """结果集 → 脱敏后的**新**结果集 + 审计报告。

        返回的 ``rows`` 永远是新的外层列表与新的行列表:调用方(以及它上面
        持有同一份结果的节点)的数据不被改写。代价是每行一次浅拷贝 ——
        §8.3 选后置脱敏时就接受了这笔开销。

        ``columns`` 是结果集的列名(与 ``rows`` 对齐);``sql`` / ``contract``
        用来取**投影位置**(见模块 docstring 的列匹配五条)。

        ``principal`` 为 ``None`` 时**照常脱敏**:没有主体就没有可依据的 pii
        授权,安全方向是多脱敏而不是放行。``model`` 为 ``None`` = 没有声明面
        (没有字段可脱敏),不是"没有依据所以放行"。
        """
        rows_in = [list(row) for row in (rows or [])]

        if model is None:
            return rows_in, {"fields": {}, "bypass": False}

        # §5.5 / §8.4:bypass 只看 scope,不看 role(admin 不自动 bypass)。
        # 判定先于声明扫描:bypass 什么都不改写,也就没有"非法声明被静默放行"
        # 的问题 —— 那笔账在 lint(写前门禁)那边结。
        if principal is not None and Policy.may_bypass_masking(principal, model.masking):
            return rows_in, {"fields": {}, "bypass": True}

        declared = _declared_fields(model)
        if not declared:
            return rows_in, {"fields": {}, "bypass": False}

        names = [str(name) for name in (columns or ())]
        targets = self._targets(declared, names, sql=sql, contract=contract)

        salt = ""
        if any(mode == "hash" for mode, _ in targets.values()):
            salt = self._salt(model)

        out_rows: list[list[Any]] = []
        for row in rows_in:
            for index, (mode, _) in targets.items():
                if index < len(row):
                    row[index] = _mask_value(mode, row[index], salt)
            out_rows.append(row)

        fields: dict[str, str] = {}
        for mode, hit_names in targets.values():
            for name in hit_names:
                previous = fields.get(name)
                if previous is None or STRICTNESS[mode] > STRICTNESS[previous]:
                    fields[name] = mode
        return out_rows, {"fields": fields, "bypass": False}

    # ── 内部 ───────────────────────────────────────────────────

    def _targets(
        self,
        declared: list[_Declared],
        columns: list[str],
        *,
        sql: str | None,
        contract: dict[str, Any] | None,
    ) -> dict[int, tuple[str, list[str]]]:
        """输出位置 → (模式, 命中的声明字段名)。

        位置信息可用时**只用位置匹配**(不回退列名):``SELECT email AS phone``
        的名字退化会拿 phone 的模式去改写 email。
        """
        targets: dict[int, tuple[str, list[str]]] = {}
        slots = _positions_from_contract(contract, len(columns))
        if slots is None:
            slots = _positions_from_sql(sql, len(columns))
        if slots is not None:
            for index, slot in enumerate(slots):
                if slot is None:
                    continue          # 函数/聚合投影:有意的边界
                table, column = slot
                hit = _match(declared, column, table)
                if hit is not None:
                    targets[index] = hit
            return targets
        for index, name in enumerate(columns):
            hit = _match(declared, name, "")
            if hit is not None:
                targets[index] = hit
        return targets

    def _salt(self, model: SemanticModel) -> str:
        """模型级 ref 优先,回落到部署级 ref;解析不出抛 ``MaskingError``。"""
        ref = str(getattr(model.masking, "hash_salt_ref", "") or "").strip() or str(
            self._default_salt_ref or ""
        ).strip()
        return resolve_salt_ref(ref)


def _match(
    declared: list[_Declared], column: Any, table: Any,
) -> tuple[str, list[str]] | None:
    """一个输出位置/列名 → (最严模式, 命中的字段名列表);无命中返回 ``None``。

    表限定只在**能对上某个数据集**时用来收窄:投影里的表名可能是别名
    (``SELECT c.phone FROM customers c`` 里的 ``c``),对不上就退回按列名全部
    命中 —— 那一步的方向仍是取严,不会漏脱敏。
    """
    key = _norm_ident(column)
    if not key:
        return None
    hits = [item for item in declared if key in item.keys]
    if not hits:
        return None
    table_key = _norm_ident(table)
    if table_key:
        narrowed = [item for item in hits if table_key in item.tables]
        if narrowed:
            hits = narrowed
    strictest = max(hits, key=lambda item: STRICTNESS[item.mode]).mode
    return strictest, [item.name for item in hits]


def _mask_value(mode: str, value: Any, salt: str) -> Any:
    """单值改写。``None`` 在所有模式下不变(它是"没有值",不是 PII)。"""
    if mode == "null" or value is None:
        return None
    if mode == "partial":
        return _partial(str(value))
    if mode == "hash":
        return _hash_value(str(value), salt)
    return value
