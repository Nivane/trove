"""挂点范式:manifest 声明的节点必须**真的有注入位**。

``align_schema`` 的教训:manifest 声明了 ``triggers.node: schema_linking``,
``schema_linking.py`` 里对 skill 的引用是 0,而测试全绿 —— 因为
``test_skills.py`` 钉的是 manifest 的内容,不是调用点的存在。

本文件的纪律:**节点级的挂点测试都要让节点真实渲染一遍**,断言正文出现在
投给模型的 system prompt 里。对着 ``append_skill_block`` 这个唯一门钉,
节点改名/接线断了就红。

**装配面那条是例外**(``test_every_graph_binding_passes_skills``):它是静态
AST 检查,只证明 ``graphs.py`` 的调用点带了 ``skills=``,不渲染任何节点 ——
传进去的**对象对不对**不在它的判据内(值绑定另由 graph 装配的 spy 测试覆盖)。
"""

from __future__ import annotations

import pytest

from trove.workflow.state import WorkflowState


@pytest.fixture(autouse=True)
def _fresh_skills_cache():
    """每例重置 manifest 缓存,与 ``tests/workflow/test_skills.py`` 同一装置。

    ``_load_manifest`` 的缓存是**进程级**的;挂点测试要让节点真实渲染一遍,
    渲染就会经 ``render_skills`` 读它(它决定哪些 code skill 命中,进而决定
    "无 org 技能时 system 逐字不变"那条断言比的是什么),中间又可能有测试
    改了 manifest 的解读(或 Task 12 删了条目),不重置就会串味。
    """
    from trove.prompts import skills

    skills._cache = None
    yield
    skills._cache = None


class RecordingLLM:
    """记录每次调用的 messages;chat 返回可脚本化的字符串。"""

    def __init__(self, response=""):
        self.response = response
        self.calls: list[list[dict]] = []

    def system_of(self, index=-1) -> str:
        msgs = self.calls[index]
        return next(m["content"] for m in msgs if m["role"] == "system")

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        return self.response

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return {"content": self.response, "tool_calls": []}

    async def chat_stream(self, model, messages, **kwargs):
        self.calls.append(messages)
        yield self.response


def _org_skill(tmp_path, node, body="ORG-METHOD-BODY", tier="required", **triggers):
    """建一个 confirmed org skill;``**triggers`` 追加 node 之外的触发维度。

    ``role`` 是这里唯一能证明 ctx 真的流到调用点的维度:``lang`` 在多数
    调用点同时是具名参数,写错了照样命中,断言不出来。
    """
    from trove.services.skills.service import SkillService

    svc = SkillService(tmp_path)
    svc.create({
        "name": "org-method", "description": "组织方法论",
        "triggers": {"node": node, **triggers}, "tier": tier, "body": body,
    })
    svc.confirm("org-method")
    return svc


#: 装配面上**必须**带 skills= 的节点工厂。
#:
#: 这是一张会长大的清单,由后续 Task 逐个追加(每个 Task 追加自己那一个,
#: 所以每个 Task 结束时测试都是绿的)。用 AST 而不是源码文本:对格式、
#: 换行、参数顺序免疫,而**每新增一个调用点都自动落进检查** —— 这正是
#: 需要人工看一眼的信号("多了一个装配点")。
#:
#: **它只认 ``ast.Name`` 形态的调用**(``make_insights(...)``)。改成属性调用
#: (``builder.make_insights(...)``)会静默逃出检查 —— 装配点确实还在,只是
#: 这里看不见。哪天真要这么写,把这个匹配扩到 ``ast.Attribute``。
FACTORIES_REQUIRING_SKILLS: list[str] = [
    "make_validate_rules",
    "make_insights",
    "make_conclusion",
    "make_chart",
    "make_attribution",
]


def test_every_graph_binding_passes_skills():
    """graphs.py 里每个该带 skills 的装配点都必须真的带。

    graphs.py 有**两个**图构建函数(reflection / fixed),同一个节点在里面
    各装配一次。只改一处 = 半接线:另一条入口上 org validator 永不运行,
    而没有任何测试会红 —— 除非把断言钉在**每一处调用**上,而不是钉在
    "有一处传了"上。
    """
    import ast
    import inspect

    from trove.workflow import graphs as graphs_module

    tree = ast.parse(inspect.getsource(graphs_module))
    found: dict[str, list[ast.Call]] = {name: [] for name in FACTORIES_REQUIRING_SKILLS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in found:
                found[node.func.id].append(node)

    for name, calls in found.items():
        assert calls, (
            f"graphs.py 里没有 {name}(...) 的调用 —— 装配点改名或删掉了? "
            "改名的话这张清单要跟着改,否则这个测试会永远绿着骗人。"
        )
        for c in calls:
            kw = {k.arg for k in c.keywords}
            assert "skills" in kw, (
                f"graphs.py:{c.lineno} 的 {name}(...) 没传 skills —— "
                "该装配路径上 org skill 永不生效"
            )


async def test_gen_sql_node_forwards_skill_ctx(tmp_path):
    """gen_sql 挂点也必须把 ctx 送进去 —— 三个挂点里最不显眼的一个。

    query_sketch / analyze_error 的 ctx 来自节点自己的 state,读一眼就知道;
    而 ``gen_generate`` 拿的是**外层** WorkflowState —— 子图状态 GenSQLState
    上没有 ``skill_ctx()``(也没有 intent / tool_roles),照抄成 ``sub_state``
    会 AttributeError。所以这条只能在真实装配路径上钉:``make_state()``
    钉不到它,``test_skills.py`` 里那两条节点级 pin 也覆盖不了它。

    用 role 而不是 lang:``lang`` 在这个调用点同时也是具名参数,漏传 ctx
    照样命中,断言不出来;role 只可能从 ``**ctx`` 进来。
    """
    from trove.core.config import AgentConfig
    from trove.workflow.graphs import GraphServices, make_gen_generate
    from trove.workflow.state import GenSQLState

    class _NullSubgraph:
        """经典兜底子图占位:agentic 正常收尾时不会被调用。"""

        async def ainvoke(self, state):
            return {"sql": "SELECT 1;", "attempts": 1, "error": None}

    svc = _org_skill(tmp_path, "gen_sql", body="GEN-SQL-ORG-BODY",
                     role=["analyst"])
    llm = RecordingLLM("```sql\nSELECT 1;\n```")
    services = GraphServices(
        llm=llm, config=AgentConfig(target="mock/model"), skills=svc)
    node = make_gen_generate(services, _NullSubgraph())

    async def _system_text(tool_roles, lang):
        llm.calls.clear()
        state = WorkflowState(
            session_id="s1", question="q", lang=lang, tool_roles=tool_roles,
            datasource="demo",
            gen_ctx={"dialect": "sqlite", "complexity": "standard",
                     "in_correction": False, "examples": []},
            gen_sub_state=GenSQLState(
                question="q", lang=lang, schema_context="").model_dump(),
        )
        await node(state)
        assert llm.calls, "agentic 分支没走到模型调用 —— 装配没进去"
        # 拼**全部**消息:开了 prompt caching 时 system 被拆成稳定前缀块 +
        # 其余部分,只看 messages[0] 会漏判(实测 False 过一次)。
        return "\n".join(str(m.get("content") or "") for m in llm.calls[0])

    assert "GEN-SQL-ORG-BODY" in await _system_text(["analyst"], "zh")
    assert "GEN-SQL-ORG-BODY" not in await _system_text(["viewer"], "zh")


async def test_insights_hook_injects_org_skill(tmp_path):
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.insights import make_insights

    llm = RecordingLLM(response="- 一条洞察")
    # insights=True 必须显式给:AgentConfig.insights 默认 False(config.py:259),
    # 关了它节点在第二个 gate 就返回 {} —— 那时 llm.calls 是空的,
    # 而失败信息会把你指向"挂点没接上",与真实原因无关。
    node = make_insights(llm, AgentConfig(target="m", insights=True),
                         skills=_org_skill(tmp_path, "insights"))

    state = WorkflowState(
        session_id="s1", question="各地区授信余额", lang="zh",
        sql="SELECT region, balance FROM credit",
        columns=["region", "balance"], rows=[["A", 5]], row_count=1,
    )
    await node(state)

    assert llm.calls, "insights 没调到 LLM —— gate 早返了,按真实条件补 state"
    system = llm.system_of()
    assert "ORG-METHOD-BODY" in system
    # 来源标注:指示性文本必须可审计到"哪份配置"。
    assert 'source="admin-confirmed"' in system


async def test_insights_hook_backward_compatible():
    """skills=None → 与今天逐字一致(不含任何围栏块)。"""
    from trove.core.config import AgentConfig
    from trove.prompts import render
    from trove.workflow.nodes.insights import make_insights

    llm = RecordingLLM(response="- 一条洞察")
    node = make_insights(llm, AgentConfig(target="m", insights=True))

    state = WorkflowState(
        session_id="s1", question="q", lang="zh", sql="SELECT 1",
        columns=["a"], rows=[[1]], row_count=1,
    )
    await node(state)
    assert llm.calls
    # 逐字相等:没有围栏块时,门 append_skill_block 必须**原样**返回入参 ——
    # 这是"既有行为逐位不变"这条全局约束在这个挂点上的可执行形式。
    assert llm.system_of() == render("insights/system", lang="zh")


async def test_conclusion_hook_injects_org_skill(tmp_path):
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.conclusion import make_conclusion

    llm = RecordingLLM(response="结论一句")
    # conclusion 默认 False(config.py:260),同 Task 8 的 insights。
    node = make_conclusion(llm, AgentConfig(target="m", conclusion=True),
                           skills=_org_skill(tmp_path, "conclusion"))

    state = WorkflowState(
        session_id="s1", question="q", lang="zh", sql="SELECT 1",
        columns=["a"], rows=[[1]], row_count=1,
    )
    await node(state)
    assert llm.calls, "conclusion 没调到 LLM —— gate 早返了,按真实条件补 state"
    assert "ORG-METHOD-BODY" in llm.system_of()


async def test_chart_hook_injects_org_skill(tmp_path):
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.chart import make_chart

    llm = RecordingLLM(response="")          # 规格解析会失败 → 回退确定性推断
    # chart_llm 默认 False(config.py:261):关了它 chart.py:171 整个 LLM 分支
    # 都不进,直接走确定性推断 —— llm.calls 空,测试却看着像挂点没接。
    node = make_chart(
        llm=llm, config=AgentConfig(target="m", chart_llm=True),
        semantic_layer=None, skills=_org_skill(tmp_path, "chart"),
    )
    state = WorkflowState(
        session_id="s1", question="各地区授信余额", lang="zh",
        sql="SELECT region, balance FROM credit",
        columns=["region", "balance"], rows=[["A", 5], ["B", 7]], row_count=2,
    )
    await node(state)

    assert llm.calls, "chart 没调到 LLM —— gate 条件不满足,补 state 字段"
    assert "ORG-METHOD-BODY" in llm.system_of()
