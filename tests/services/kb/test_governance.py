"""治理字段加载期推断(设计稿 §6.1 / I1 / I5)与认证门的纯函数测试。

只测两个纯函数:``governance_of`` (YAML 示例 → 治理字段) 与
``certification_issues`` (SQL → 准入问题)。落盘、git trailer、拒绝后文件
不变这些**服务层**行为在 ``test_governance_certify.py``;存量兼容在
``test_governance_compat.py``。分开的理由:纯函数先钉语义,服务层再钉流程,
两边红了才好定位。
"""

import pytest

from trove.services.kb.governance import (
    CERTIFIED,
    DEPRECATED,
    DRAFT,
    SOURCE_KB_INIT,
    certification_issues,
    governance_of,
)


class TestGovernanceOf:
    """``governance_of``:一条示例 → 五个治理字段(**总是**五个,不抛异常)。"""

    def test_example_without_governance_block_is_draft_from_kb_init(self):
        """I1:存量资产整块缺失 → draft + kb_init。

        这是全套治理里唯一「缺省值即语义」的地方:没有块 = /kb init 的产物,
        不是「未知」。存量 622 条因此零迁移即可加载。
        """
        assert governance_of({"question": "q", "sql": "SELECT 1"}) == {
            "status": DRAFT,
            "owner": "",
            "approved_by": "",
            "approved_at": "",
            "source": SOURCE_KB_INIT,
        }

    @pytest.mark.parametrize("broken", ["certified", ["draft"], 3, None])
    def test_broken_governance_block_falls_back_to_draft(self, broken):
        """块在但读不成映射 → 视作 draft,资产仍可用(失败模式表:open)。

        坏元数据不该让资产不可用 —— 与「拒绝加载整个文件」相比,丢掉一个
        治理标记的代价小得多。
        """
        gov = governance_of(
            {"question": "q", "sql": "SELECT 1", "governance": broken})
        assert gov["status"] == DRAFT

    def test_broken_block_reports_unknown_source_not_kb_init(self):
        """坏块与缺块的 ``source`` 不同:缺块能推断来路,坏块不能。

        这是两个失败模式的**唯一**可观测差别。把坏块也推断成 kb_init 会把
        「这份元数据坏了」伪装成「这是 init 产物」,于是没人会去修它 —— 坏块
        需要有人看见,而不是被一个合理的默认值盖住。
        """
        assert governance_of({"governance": None})["source"] == ""

    def test_empty_governance_mapping_counts_as_unknown_not_stock(self):
        """``governance: {}`` 与「键不存在」不同 —— 键一旦出现就不再推断 kb_init。

        这条规则是为将来的写端留的:一个「总是写 governance 键」的写入器,
        在还填不出内容时会写下空映射。把它当成存量(kb_init)等于宣称"这是
        init 产物",而我们其实一无所知 —— 键**出现**本身就是一条信息。
        """
        gov = governance_of({"governance": {}})
        assert gov["status"] == DRAFT
        assert gov["source"] == ""

    def test_full_certified_block_is_read_verbatim(self):
        gov = governance_of({"governance": {
            "status": CERTIFIED, "owner": "alice",
            "approved_by": "bob", "approved_at": "2026-09-28T10:00:00Z",
            "source": "human",
        }})
        assert gov == {
            "status": CERTIFIED, "owner": "alice", "approved_by": "bob",
            "approved_at": "2026-09-28T10:00:00Z", "source": "human",
        }

    def test_certified_without_approved_by_is_downgraded(self):
        """I5:certified 必须有人 —— 缺批准人 → draft(加载期降级,不报错)。

        报错会让**整份文件**不可用(一条坏资产拖垮一个数据源),而降级只损失
        这一条的权重。方向也一致:宁可标低不可标高(I6)。
        """
        gov = governance_of({"governance": {
            "status": CERTIFIED, "approved_at": "2026-09-28T10:00:00Z"}})
        assert gov["status"] == DRAFT

    def test_certified_without_approved_at_is_downgraded(self):
        """I5 的另一半:只有批准人没有日期 → 同样不是 certified。

        日期是「什么时候验的」的唯一证据 —— 一条 2026 年 8 月验过的 SQL 到
        12 月不该和上周验的同等权重,所以没有日期的认证等于没有认证。
        """
        gov = governance_of({"governance": {
            "status": CERTIFIED, "approved_by": "bob"}})
        assert gov["status"] == DRAFT

    def test_downgraded_block_keeps_approver_as_repair_hint(self):
        """降级只改 status,不清批准人 —— 加载器不擦掉盘上的证据。

        清掉的话管理端就再也看不出「这条自称 bob 批过但没写日期」,而那正是
        修好它需要的线索。降级是**派生判断**,不是对文件的改写(I1:不写回)。
        """
        gov = governance_of({"governance": {
            "status": CERTIFIED, "approved_by": "bob"}})
        assert gov["approved_by"] == "bob"

    def test_unquoted_date_literal_does_not_downgrade_certified(self):
        """``approved_at: 2026-09-28``(不加引号)会被 YAML 还原成 date 对象。

        只认 ``str`` 的实现会把它当没写 → 一条真心认证过的资产被静默降为
        draft,而且**盘上看起来完全正常**。这类坑同族于 kb_items 落库时的
        ``default=str``(裸日期字面量),所以这里也按标量序列化而不是丢弃。
        """
        import yaml

        block = yaml.safe_load(
            "status: certified\napproved_by: bob\napproved_at: 2026-09-28\n")
        assert not isinstance(block["approved_at"], str)  # 前提:YAML 真的还原了
        gov = governance_of({"governance": block})
        assert gov["status"] == CERTIFIED
        assert gov["approved_at"].startswith("2026-09-28")

    @pytest.mark.parametrize("bad", ["certified ", "CERTIFIED", "verified", "", None])
    def test_unknown_status_is_not_promoted_to_certified(self, bad):
        """未识别的 status 一律 draft —— 认不出的值绝不能被当成已认证。

        ``"CERTIFIED"`` 单独列出:大小写不该决定一条资产的权重,但方向的
        选择不是对称的 —— 猜错成 certified 是把没人背书的东西标成有人背书。
        """
        gov = governance_of({"governance": {"status": bad, "approved_by": "b"}})
        assert gov["status"] == DRAFT

    def test_deprecated_is_preserved(self):
        """deprecated 是**人显式标的**排除项(§8.2),不能被降级逻辑吃掉。"""
        assert governance_of(
            {"governance": {"status": DEPRECATED}})["status"] == DEPRECATED

    def test_non_scalar_field_values_are_treated_as_unwritten(self):
        """列表/映射当没写:``str(["a"])`` 会得到 "['a']" 这种垃圾标签。

        治理字段是给人读的标签,不是数据 —— 一个渲染不出人话的值等于没写。
        """
        gov = governance_of({"governance": {
            "owner": ["alice", "bob"], "approved_by": {"name": "bob"}}})
        assert gov["owner"] == ""
        assert gov["approved_by"] == ""

    def test_unknown_keys_in_block_are_ignored(self):
        """块里多写的键不进投影 —— 投影的键集必须与 ExampleHit 字段一一对应。

        设计稿 §7.2 的 certify 端点要写 ``{by, note}``,将来还会有别的键;
        投影多带一个键就会让 ``ExampleHit(**payload)`` 抛 TypeError,把一条
        良性扩展变成加载失败。
        """
        gov = governance_of({"governance": {"status": DRAFT, "runs": 42}})
        assert set(gov) == {"status", "owner", "approved_by", "approved_at", "source"}


class TestCertificationIssues:
    """``certification_issues``:认证门(§7.2 / G5)。空列表 = 可以认证。

    门问的是**「这条 SQL 能不能被当作可信资产执行」**,不是「它写得好不好」。
    理由:fast_match 命中参考示例后直接把它当答案执行 —— 示例比语义模型更
    接近最终答案,所以认证门的下限必须是「执行安全」而不是「检索质量」。
    """

    def test_readonly_query_passes(self):
        assert certification_issues("SELECT COUNT(*) FROM loan") == []

    def test_backtick_mysql_query_passes(self):
        """KB 里的示例是 MySQL 方言(BIRD 目标),反引号限定列必须放行。"""
        assert certification_issues(
            "SELECT `A2`, SUM(amount) FROM loan l GROUP BY `A2`") == []

    @pytest.mark.parametrize("sql", [
        "DELETE FROM loan",
        "UPDATE loan SET amount = 0",
        "SELECT 1; DELETE FROM loan",
        "WITH x AS (DELETE FROM loan RETURNING *) SELECT * FROM x",
    ])
    def test_write_surfaces_are_refused(self, sql):
        """写面必须被拒 —— 认证后的示例会进快径被直接执行。

        含 data-modifying CTE(顶层是 SELECT、树内藏 DELETE):只查顶层语句
        类型的门会放行它,而它一样会删数据。
        """
        assert certification_issues(sql) != []

    def test_unparseable_sql_is_refused(self):
        """解析失败按错误处理(``fail_on_parse_error=True``),不是放行。

        执行路径用 fail-open 是因为那里的边界在数据库只读角色;认证是**写
        资产**的动作,解析不了的 SQL 进了资产库就会变成一条永远跑不通的
        参考,没有第二道边界兜底。
        """
        assert certification_issues("SELEC * FORM loan") != []

    def test_empty_sql_is_refused(self):
        assert certification_issues("") != []

    def test_metadata_table_is_refused(self):
        """元数据表侦察面与只读与否无关,一律拒绝。"""
        assert certification_issues("SELECT * FROM sqlite_master") != []

    def test_dangerous_function_is_refused(self):
        """SLEEP/BENCHMARK 类函数会拖垮数据源,认证时挡在库外。"""
        assert certification_issues("SELECT SLEEP(5)") != []
