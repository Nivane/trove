"""参考示例的治理维度:加载期推断(§6.1 / I1 / I5)与认证门(§7.2 / G5)。

治理字段只在 ``examples.yml`` 里写一份(与 SQL 同生共死、随 git 一起回滚),
**读的时候推断而不是迁移存量**:存量 622 条已经有 SQL 和问题,没有任何一
条人工背书的证据 —— 迁移会一次性往盘上写 622 条 ``status: draft``,那既是
一次没有信息量的写盘,也让「这条资产被谁验过」的第一次记录变成机器批量盖
的章。推断出来的 draft 和写在盘上的 draft 对检索的含义完全相同,所以不必写。

统计维度(``runs`` / ``p50_ms``)不在这里也不在 YAML 里 —— 它是高频运行时
数据,写 YAML 会让每次查询都触发一次 git commit,把资产库的历史淹在统计
噪声里(§8.1)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from trove.services.sql.guard import check_readonly

#: 状态取值。``status`` 是唯一**有决定权**的治理字段(P3 按它过滤/加权),
#: 所以只有它按值域校验;其余四个是给人看的,原样透传。
DRAFT = "draft"
CERTIFIED = "certified"
DEPRECATED = "deprecated"
STATUSES = frozenset({DRAFT, CERTIFIED, DEPRECATED})

#: 来源取值(§6.1):human | memory | kb_init | user_feedback。
#: 只有 kb_init 在这里被用到 —— 它是**推断**出来的,不是谁写上去的。
SOURCE_KB_INIT = "kb_init"

#: 投影出的键集,必须与 ``ExampleHit`` 的治理字段一一对应:``ExampleHit(**p)``
#: 多一个未知键就是 TypeError,而那会把「一条良性扩展」变成「整个数据源加载
#: 失败」。
GOVERNANCE_FIELDS = ("status", "owner", "approved_by", "approved_at", "source")


class ExampleCertificationError(ValueError):
    """认证门拒绝(失败模式表:closed)。

    继承 ``ValueError``:与 ``append_term`` 的空 mapping 守卫同族(值不合法
    → 拒绝写入),只 catch ``ValueError`` 的调用方也能兜住。

    **刻意不做成「跳过坏的那条、确认其余的」**:部分确认会让 confirmed 这个
    状态失去含义 —— admin 点了确认,却有一半没进库,而返回的计数说不清哪些
    进了。要放行就先把那条改对。
    """


def _text(value: Any) -> str:
    """治理字段一律取文本标量;列表/映射当没写。

    ``date``/``datetime`` 必须序列化而不是丢弃:``approved_at: 2026-09-28``
    不加引号时 YAML 会把它还原成 ``date`` 对象,只认 ``str`` 的实现会把它当
    没写 —— 于是一条真心认证过的资产被静默降为 draft,而**盘上看起来完全
    正常**。同族的坑在本模块外也踩过(kb_items 落库的 ``default=str``)。
    数字同理(某天有人写 ``owner: 42``,序列化出来至少还看得出写了什么)。

    列表/映射反过来要丢弃:``str(["alice"])`` 得到的是 ``"['alice']"``,一个
    渲染不出人话的标签比空值更坏 —— 它看起来像数据。``bool`` 是 ``int`` 的
    子类,先挡掉,免得 ``owner: true`` 变成 ``"True"``。
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def governance_of(example: Mapping[str, Any]) -> dict[str, str]:
    """一条示例 → 治理字段。**总是**返回 ``GOVERNANCE_FIELDS`` 那五个键。

    三条规则,方向都是「宁可标低」:

    1. **块不存在** → ``draft`` + ``kb_init``(I1):存量资产是 ``/kb init``
       的产物,这是从历史推出来的,不是从块里读出来的。
    2. **块在但读不出**(非映射、或裸 ``governance:``) → ``draft`` + 空
       source(失败模式表:open):坏元数据不该让资产不可用,但也不该被伪装
       成 init 产物 —— 坏块需要被人看见,而一个合理的默认值恰好会盖住它。
    3. **certified 必须有人**(I5):``approved_by`` 或 ``approved_at`` 任一为空
       → 降为 ``draft``。**只降 status,不擦批准人和日期**:那是修好它唯一的
       线索,而这里做的是派生判断,不是改写文件(I1:不写回)。
    """
    block = example.get("governance")
    if not isinstance(block, Mapping):
        stock = "governance" not in example
        return {
            "status": DRAFT,
            "owner": "",
            "approved_by": "",
            "approved_at": "",
            "source": SOURCE_KB_INIT if stock else "",
        }

    # 大小写不归一(与 lint._is_many_to_many 的宽松归一相反):那个函数怕的是
    # **漏判**(漏掉一个 M:N 会到编译期才炸),这里怕的是**误判**——把
    # ``CERTIFIED`` 认成 certified,等于让一个拼写差异给资产盖上人工背书的章。
    # 两种失败的方向不同,所以从严。
    status = _text(block.get("status"))
    if status not in STATUSES:
        status = DRAFT

    approved_by = _text(block.get("approved_by"))
    approved_at = _text(block.get("approved_at"))
    if status == CERTIFIED and not (approved_by and approved_at):
        status = DRAFT

    return {
        "status": status,
        "owner": _text(block.get("owner")),
        "approved_by": approved_by,
        "approved_at": approved_at,
        "source": _text(block.get("source")),
    }


def certification_issues(sql: str, dialect: str = "") -> list[str]:
    """认证门:这条 SQL 能不能作为可信资产进库。空列表 = 放行。

    门问的是**「能不能被执行」**,不是「写得好不好」。理由:``fast_match``
    命中参考示例后直接把它当答案执行 —— 示例比语义模型更接近最终答案(§2.3),
    所以认证门的下限必须是执行安全。

    **为什么不复用 ``lint.lint_examples``**:它已经存在且规则有重叠,但两者问
    的不是同一个问题,直接拿它当门会误伤 ——

    - 它把「示例问题无英文内容」列为 issue(建议双语)。那是**建议级**规则
      (文案自己写着「建议」),拿来当门会把一批合法的中文问题挡在认证之外,
      而人工确认一条中文问题是 admin 的正当选择;
    - 它查写操作只扫 ``Insert/Update/Delete/Drop`` 四类节点,而这里要挡的是
      执行面:``check_readonly`` 另外覆盖 SET/CALL/COPY/MERGE/REPLACE INTO、
      data-modifying CTE(顶层是 SELECT、树内藏 DELETE)、``SELECT ... INTO
      OUTFILE``、危险函数、元数据表侦察面 —— 每一条都是「能跑但会出事」。

    结构与质量建议仍归 ``scripts/lint_kb.py`` 与分析期(那里可以只报不拒);
    ``fail_on_parse_error=True`` 与执行路径的 fail-open 相反:执行路径有数据库
    只读角色兜底,而写进资产库的坏 SQL 没有第二道边界 —— 它会变成一条永远
    跑不通的参考,还要靠人再发现。
    """
    ok, reasons = check_readonly(sql, dialect, fail_on_parse_error=True)
    return [] if ok else reasons
