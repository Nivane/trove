"""不可信值隔离核 — 外部值进入模型上下文前的唯一隔离实现。

一个核,两个结构性入口(设计稿:``2026-09-29-untrusted-input-boundary-design.md``):

- **A 模板渲染**:``trove.prompts.loader.render()`` —— 插值前逐参数过核;
- **B 工具回喂**:``trove.llm.agent_loop`` —— 工具返回值注入 messages 前过核。

两条之外没有第三条路(守卫测试枚举全仓 llm 调用点)。为什么不是"唯一函数入口":
工具回喂的值不在模板渲染的取值路径上;强行合并只有坏做法(见设计稿 §3)。

信任分类三条规则,其余**默认按数据扫**(fail-safe 方向:新增参数不会因为
"忘了登记"而裸奔):

1. **用户本人的话**(``question``)—— 委托人自己的指令,隔离它等于篡改需求;
2. **经人确认后落库的结构化知识**(语义层 / KB)—— 有确认人、有版本;
3. **系统自身的标量与枚举**。

不因"是谁生成的"而可信:LLM 产出的文本(计划、反思、SQL、记忆)按数据扫 ——
模型会把外部内容**回声**到自己的输出里,不扫就留下洗白通道。
(此处推翻 ``injection.py`` 旧边界声明"LLM 自生成…不在此列",旧文保留留痕。)

**粒度规则:隔离的粒度 = 传入值的结构粒度。** 传单元格列表 → 逐格替换;
传已拼好的大文本 → 整块替换。所以调用方应尽量传结构化值(见设计稿 §5.1)。
"""

from __future__ import annotations

from typing import Any

from trove.core.logging import get_logger
from trove.core.metrics import record_prompt_isolation
from trove.llm.injection import ISOLATED_MARKER, scan_injection

logger = get_logger(__name__)

# 单值扫描上限:超过此长度无法逐字审查,按保守方向整值作废。
MAX_SCAN_CHARS = 64 * 1024
# 超长作废在观测里的模式名(与真实注入模式区分开)。
OVERSIZED = "oversized"

# 白名单:只有下面三类可信。**新增参数默认扫**,要放行必须在此显式登记。
# ``question`` 的放行前提是**用户本人的话**;LLM 派生的问题字面上走这个参数,
# 却不是用户原话 —— 它们在**诞生点**各自过一次 ``screen_derived``(目前只有
# 一处:``workflow/graphs.py`` 的追问改写),见设计稿 §4.2。
_TRUSTED_VARS: frozenset[str] = frozenset({
    # 1. 用户本人的话
    "question",
    # 2. 经人确认后落库的结构化知识(语义层 / KB)
    "schema_context", "evidence", "rules", "full_rules", "few_shots",
    "lessons", "term_notes", "vocabulary", "entities", "metrics",
    # 3. 系统自身的标量与枚举(不可能命中,登记是为了让分类表完整)
    "lang", "dialect", "fix_mode", "has_probe", "total_rows",
    "node", "purpose", "skill_name",
})


def is_trusted_var(name: str) -> bool:
    """参数名是否可信(可信 = 不做注入扫描)。未知名字一律不可信。"""
    return name in _TRUSTED_VARS


class AdminConfirmed(str):
    """第三档信任级:**人确认过的配置文本**(org skill 正文)。

    前两档是「用户原话」(`_TRUSTED_VARS` 按**参数名**放行)与「数据」(扫,
    命中整值作废)。这一档两者都不是:

    * 不是数据 —— 它由管理员确认后落库(`status == "confirmed"`),信任级等同
      system prompt。对**指令性**文本做整值作废,处置与语义相反:实测一句
      「忽略之前的指令」会让整份方法论变成 ``[data: content isolated]``。
      一句话毁掉一份方法论是误伤,不是安全。
    * 不是用户原话 —— 它不该冒充 ``question``,所以不能走 `_TRUSTED_VARS`。

    所以第三档要有自己的类型。**放行的依据是登记**(调用方显式构造这个类型),
    不是内容长相 —— 与白名单同一条纪律:要放行必须显式登记。

    安全属性来自**写入关口**(确认时筛查、给人看),不来自运行期扫指令文本。
    围栏与来源标注由提示词层负责(`prompts/skills.render_org_skill_block`),
    让模型与事后审计都能分辨「这条指令来自哪份配置」。
    """


def is_admin_confirmed(value: Any) -> bool:
    """是否为登记过的配置文本(提示词层围栏前的判断点)。"""
    return isinstance(value, AdminConfirmed)


def isolate_tree(value: Any) -> tuple[Any, list[str]]:
    """递归隔离:str 叶子命中即整值替换,容器逐叶递归,其余类型原样。

    Returns:
        (隔离后的值, 命中的模式名列表[首次出现顺序去重])。
        干净值原样返回(同一对象,不复制)—— 隔离核在最热路径上,不做无谓深拷贝。
    """
    hits: list[str] = []
    return _walk(value, hits), hits


def screen_derived(value: Any, *, site: str) -> tuple[Any, list[str]]:
    """派生值**出生点**筛查:LLM 产物即将被当成"用户原话"用之前,先查一次来源。

    与两条消费通道(A 渲染 / B 回喂)的区别:这里不是值进入提示词的位置,而是
    **一个值被赋予可信身份**的位置。``question`` 在白名单里代表"用户本人的话",
    派生问题(追问改写)字面上走这个参数却不是 —— 不查就是洗白通道:外部数据
    → 模型回声 → 下一轮被当成用户说的。

    **处置留给站点**:本函数只做"筛查 + 观测",命中后的动作按站点定 ——
    追问改写是**弃用**(回落到既有的引导话术分支);统一替换成标记会让下游拿到
    一句无意义的问题。``site`` 是站点常量,不在 ``ISOLATION_SITES`` 里则不记指标
    (与工具名同一条纪律:标签值域是闭的)。
    """
    value, hits = isolate_tree(value)
    if hits:
        logger.warning("derived value isolated: site=%s patterns=%s", site, ",".join(hits))
        for pattern in hits:
            record_prompt_isolation("derive", site, pattern)
    return value, hits


def _note(hits: list[str], name: str) -> None:
    if name not in hits:
        hits.append(name)


def _walk(value: Any, hits: list[str]) -> Any:
    # 登记过的配置文本先判:它是 str 的子类,顺序反了就会被当成数据作废。
    if isinstance(value, AdminConfirmed):
        return value
    if isinstance(value, str):
        return _isolate_str(value, hits)
    if isinstance(value, (list, tuple)):
        items = [_walk(v, hits) for v in value]
        if all(a is b for a, b in zip(items, value)):
            return value
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        changed = False
        for k, v in value.items():
            nk = _walk(k, hits) if isinstance(k, str) else k
            nv = _walk(v, hits)
            if nk is not k or nv is not v:
                changed = True
            out[nk] = nv
        return out if changed else value
    return value


def _isolate_str(s: str, hits: list[str]) -> str:
    if len(s) > MAX_SCAN_CHARS:
        _note(hits, OVERSIZED)
        return ISOLATED_MARKER
    found = scan_injection(s)
    if not found:
        return s
    for name in found:
        _note(hits, name)
    return ISOLATED_MARKER
