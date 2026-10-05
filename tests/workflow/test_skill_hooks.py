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
        self.models: list[str] = []

    def system_of(self, index=-1) -> str:
        msgs = self.calls[index]
        return next(m["content"] for m in msgs if m["role"] == "system")

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        self.models.append(model)
        return self.response

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        self.models.append(model)
        return {"content": self.response, "tool_calls": []}

    async def chat_stream(self, model, messages, **kwargs):
        self.calls.append(messages)
        self.models.append(model)
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


def test_every_registry_with_skills_passes_skill_ctx():
    """带 skills= 的 build_sql_registry 装配点,也必须带 skill_ctx=。

    ``load_skill`` 的判定是**触发器**(role / lang / intent …),而判定要读的
    ctx 只能由装配处递进去。漏传的后果不是报错,是**恒不匹配**:prompt 里
    按名点了技能、模型调过去却被拒,白烧一轮 —— 与注册侧漏接线同一类半接线,
    只是这次断在判定侧。

    这里只认 ``ast.Name`` 形态的调用(见 ``FACTORIES_REQUIRING_SKILLS`` 的
    同款说明);空清单断言兜住"改名/删调用"造成的永远绿。
    """
    import ast
    import inspect

    from trove.workflow import graphs as graphs_module

    tree = ast.parse(inspect.getsource(graphs_module))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "build_sql_registry"
    ]
    assert calls, "graphs.py 里没有 build_sql_registry(...) 的调用 —— 改名了?"

    with_skills = [c for c in calls if "skills" in {k.arg for k in c.keywords}]
    assert with_skills, (
        "graphs.py 里没有带 skills= 的 build_sql_registry(...) 调用 —— "
        "org available skill 的 on-demand 通道断了,清单要跟着看"
    )
    for c in with_skills:
        kw = {k.arg for k in c.keywords}
        assert "skill_ctx" in kw, (
            f"graphs.py:{c.lineno} 的 build_sql_registry(...) 带了 skills 却没带 "
            "skill_ctx —— 触发器里的 role/lang 恒不匹配,广告点名后调过去必被拒"
        )


def test_validator_host_name_is_the_node_that_runs_validators():
    """``VALIDATOR_HOST`` 是个**声明**的名字 —— 它必须真的是跑 validator 的节点。

    两处各自重打一遍字面量正是这类漂移的入口(``align_schema`` 同款):图上
    的节点改了名而常量不动,后果是**反转** —— 手写 SKILL.md 里声明
    ``node: <真节点名>`` 的会被标成 ``host_mismatch`` 永不运行,而声明
    ``node: validate``(一个不存在的节点)的照常跑。写入校验(``create``)与
    ``validators_for`` 的标记读的都是这个常量,它必须与图对得上。

    两条断言:

    1. ``validate.py`` 里 ``validators_for(`` 只有**一处**,且第一个实参是
       ``VALIDATOR_HOST`` 这个 Name(不是重打的字符串);
    2. ``graphs.py`` 里每个装配 ``make_validate_rules`` 的 ``add_node``,节点名
       都等于 ``VALIDATOR_HOST``。

    同 ``FACTORIES_REQUIRING_SKILLS``:只认 ``ast.Name`` / ``ast.Attribute``
    形态,改名或包一层就会落到空清单 —— 两句 ``assert ... , "改名了?"`` 兜住
    那种"永远绿着骗人"。
    """
    import ast
    import inspect

    from trove.services.skills.validators import VALIDATOR_HOST
    from trove.workflow import graphs as graphs_module
    from trove.workflow.nodes import validate as validate_module

    def _calls(source: str, name: str) -> list[ast.Call]:
        out = []
        for n in ast.walk(ast.parse(source)):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if (isinstance(f, ast.Name) and f.id == name) or (
                isinstance(f, ast.Attribute) and f.attr == name
            ):
                out.append(n)
        return out

    calls = _calls(inspect.getsource(validate_module), "validators_for")
    assert len(calls) == 1, (
        "validators_for 应当只有一处调用(结果断言只在一个节点跑),现在 "
        f"{len(calls)} 处:{[c.lineno for c in calls]}"
    )
    arg = calls[0].args[0]
    assert isinstance(arg, ast.Name) and arg.id == "VALIDATOR_HOST", (
        f"validate.py:{calls[0].lineno} 把宿主名重打了一遍字面量 —— 它必须来自 "
        "VALIDATOR_HOST(写入校验与 host_mismatch 标记同用它,两处各写一份就是漂移的入口)"
    )

    hosts: list[ast.expr] = []
    for n in ast.walk(ast.parse(inspect.getsource(graphs_module))):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "add_node" and len(n.args) >= 2):
            continue
        factory = n.args[1]
        if (isinstance(factory, ast.Call) and isinstance(factory.func, ast.Name)
                and factory.func.id == "make_validate_rules"):
            hosts.append(n.args[0])

    assert hosts, (
        "graphs.py 里没有装配 make_validate_rules 的 add_node(...) —— 改名或包了"
        "一层?那样这个测试会永远绿着骗人。"
    )
    for first in hosts:
        assert isinstance(first, ast.Constant) and first.value == VALIDATOR_HOST, (
            f"graphs.py:{first.lineno} 装配 make_validate_rules 的节点名不是 "
            f"{VALIDATOR_HOST!r} —— 改图上的名字必须同步改常量,否则声明的宿主"
            "与实际运行位对不上(见本测试 docstring 的反转后果)"
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
    # code skill(sql_construction)也在同一次渲染里进 system,且与 role 无关
    # —— code skill 属 de-facto required,不经 org 触发器筛。
    assert "Answer construction" in await _system_text(["viewer"], "en")


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
    # A3 回落证据:未配 model_draft → 图表判定模型与改造前逐字节一致(target)
    assert llm.models[-1] == "m"


async def test_chart_llm_uses_draft_model_when_configured(tmp_path):
    """A3 收编点:配了 model_draft 后图表判定走起草档。"""
    from trove.core.config import AgentConfig
    from trove.workflow.nodes.chart import make_chart

    llm = RecordingLLM(response="")
    node = make_chart(
        llm=llm,
        config=AgentConfig(target="m", chart_llm=True, model_draft="draft/m"),
        semantic_layer=None, skills=_org_skill(tmp_path, "chart"),
    )
    state = WorkflowState(
        session_id="s1", question="各地区授信余额", lang="zh",
        sql="SELECT region, balance FROM credit",
        columns=["region", "balance"], rows=[["A", 5], ["B", 7]], row_count=2,
    )
    await node(state)

    assert llm.models and set(llm.models) == {"draft/m"}


#: 有节点级挂点测试的节点(**全仓范围**,不只是本文件)。新增 manifest
#: 条目时,要么同时在这里加一项并写挂点测试,要么别加 —— 元测试会拦下。
#:
#: 每一项的证据在哪,必须能指出来 —— 这张表是"声明 vs 实现"的对照物,
#: 它自己不能变成第二张空头支票(``align_schema`` 就是那么来的):
#:
#:   query_sketch  tests/workflow/test_skills.py
#:                 ::test_query_sketch_includes_confirmed_org_required_skill
#:   analyze_error tests/workflow/test_skills.py
#:                 ::test_analyze_error_includes_confirmed_org_required_skill
#:   gen_sql       本文件 ::test_gen_sql_node_forwards_skill_ctx
#:                 (org 档走外层 WorkflowState 的 ctx,只能真实装配一遍才钉得住;
#:                  code skill 的渲染在同一例里断言)
#:   insights / conclusion / chart / attribution  —— 本文件,节点级渲染断言
#:                 (attribution 那条在 tests/workflow/test_attribution.py
#:                 ::TestAttributionNode::test_attribution_hook_injects_org_skill
#:                 —— 它的四道 gate 只有那边的夹具喂得满)
HOOK_TESTED_NODES: set[str] = {
    "insights", "conclusion", "chart", "attribution",
    "query_sketch", "analyze_error", "gen_sql",
}


def test_every_manifest_node_is_hook_tested():
    """manifest 声明的节点必须有挂点测试钉住。

    ``align_schema`` 的教训:声明了 ``triggers.node: schema_linking`` 却
    没有任何注入位,而测试全绿 —— 因为 ``test_skills.py`` 钉的是 manifest
    的**内容**,不是**调用点**。契约由内而外翻转过来:先声明清单,再要求
    实现满足它。
    """
    from trove.prompts.skills import _load_manifest

    declared = {
        (s.get("triggers") or {}).get("node")
        for s in _load_manifest()
    } - {None}
    missing = declared - HOOK_TESTED_NODES
    assert not missing, (
        f"manifest 声明了挂点但没有节点级注入测试: {sorted(missing)} —— "
        "要么接线并在这里登记,要么删条目。声明了却没接线的挂点是静默失效。"
    )


def test_graph_builders_bind_the_live_skill_service(tmp_path, monkeypatch):
    """装配面传的**对象**必须是 services.skills,不只是"有个 skills= 关键字"。

    与 test_every_graph_binding_passes_skills 分工:那条是静态的、对每个
    调用点查关键字(改名/漏接都红,但 skills=None 也绿);这条让真装配跑
    一遍,在工厂外面套记录器,断言**身份**。两条都不做的话,"改错对象"
    这类错要等 eval 才发现 —— org validator 静默不跑,查询照常出结果。
    """
    from trove.core.config import AgentConfig
    from trove.services.skills.service import SkillService
    from trove.workflow import graphs as graphs_module
    from trove.workflow.graphs import GraphServices, build_graphs

    svc = SkillService(tmp_path)
    seen: list[tuple[str, object]] = []

    for name in FACTORIES_REQUIRING_SKILLS:
        real = getattr(graphs_module, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            seen.append((_name, kwargs.get("skills", "<absent>")))
            return _real(*args, **kwargs)

        monkeypatch.setattr(graphs_module, name, spy)

    services = GraphServices(
        llm=RecordingLLM(), config=AgentConfig(target="mock/model"), skills=svc)
    build_graphs(services, multi_candidate=False, query_sketch=False, agentic=False)

    assert seen, "没有任何工厂被调用 —— 装配面改名了,这张清单要跟着改"
    assert {n for n, _ in seen} == set(FACTORIES_REQUIRING_SKILLS), (
        f"清单里的工厂没有全部被调用: "
        f"{sorted(set(FACTORIES_REQUIRING_SKILLS) - {n for n, _ in seen})}"
    )
    for name, bound in seen:
        assert bound is svc, f"{name} 绑的不是同一个 SkillService: {bound!r}"
