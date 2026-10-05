"""LLM 调用点全量清单:新增一个调用点就必须回答一次"它送的文本从哪来"。

设计稿 :mod:`trove.llm.untrusted` 的立论是**两条结构性通道**能覆盖全仓——
A 模板渲染 / B 工具回喂。这个立论会随代码变化而失效的唯一方式,是**多出一个
调用点**却没人注意:它自己拼 messages,于是两条通道都够不着它。

所以这里冻结全量清单(与 ``tests/prompts/test_template_vars_snapshot.py``
同一个装置):AST 枚举 ``trove/`` 与 ``scripts/`` 里所有 ``.chat`` /
``.chat_full`` / ``.chat_stream`` / ``.embedding`` 调用,与清单比对,
**多一个少一个都失败**。
失败信息会告诉你加哪个 key;加 key 的时候必须顺手答一次分类问题。

**这个测试保证的是"清单是全的",不是"分类是对的"** —— ``_KINDS`` 只校验
标签落在闭集里,不校验某个站点真的走了那条路。分类是人工判断,写在这里是为了
让它在 diff 里可见;要推翻某一格,读代码,别信这张表。

**盲区:它枚举的是"谁在调模型",看不见"字符串怎么拼"。** ``_METHODS`` 只认
``.chat/.chat_full/.chat_stream``,所以"调用点没变、投递内容变了"它不红 ——
往 ``system_text`` 上追加一段外部正文,清单一个字节都不动。这条盲区**不由扩充
清单来补**(枚举字符串拼接没有闭合的尽头),而由**单一门**补:追加只有
``prompts.skills.append_skill_block`` 一个入口,围栏与来源标注都在那里
(2026-09-29 的 org skill 封口)。新增"往 prompt 上挂东西"的路径时,挂到那个门上去。

标签(闭集):
  - ``A``  该函数的 messages 里**就地**调了 ``render(...)``;
  - ``Av`` render 产物先存进变量,再作为 messages 内容传入;
  - ``Ab`` 内容来自同文件的 **prompt 构造器**(``build_*_prompt`` → render);
  - ``Ac`` 内容是**实参**,由调用方 render 后传入(本函数里看不见 render);
  - ``B``  agent 循环自身的那一次调用(工具回喂通道的宿主);
  - ``E``  **嵌入调用**(``.embedding``):送的是待向量化的文本(KB 文档 / 用户
           问题 / 情节),去向是**向量**而不是消息列表 —— 前四档问的那句「消息里
           的文本从哪来」对它不适用,单列一档的理由是它**同样是一次 LLM 调用**:
           新增时一样要有人看一眼,漏在扫描面外就没人看(``embedding`` 正是这样
           在清单外待了一阵 —— 名字不以 ``chat`` 开头,旧的门禁断言没认出来)。

键格式:``<相对路径>::<限定名>#<该函数内第几个调用>``。带序号是因为同一个
函数里可能有多个调用点;序号按源码顺序,插入一个新调用点会让后面的序号移位 ——
这正是"要有人看一眼"的信号。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

# ── 被扫描的根:产品包 + 脚本(scripts/distill_lessons.py 也在调 LLM)──
#: 刻意**不含** tests/ 与 eval/:那是评测/测试自己的调用,不是产品路径。
_ROOTS = ("trove", "scripts")

#: 相对仓库根解析(测试从仓库根跑;pytest rootdir 见 pyproject)。
_REPO = pathlib.Path(__file__).resolve().parents[2]

#: 网关的入口(``trove/llm/gateway.py``)。新增入口方法时这里要跟着改 ——
#: 否则新入口上的调用点对清单是隐形的。入口的口径是**形状**(公开方法且是协程),
#: 不是名字 —— 按 ``chat`` 前缀认的那些年,``embedding`` 一直在扫描面之外。
_METHODS = frozenset({"chat", "chat_full", "chat_stream", "embedding"})

_KINDS = frozenset({"A", "Av", "Ab", "Ac", "B", "E"})

#: 全量调用点(2026-09-29 设计稿 §2 盘点 32 处;计入 scripts、补上它漏计的
#: ``embedding`` 后共 34 处;B6 假设层起草 +1 → 35 处;A4 历史蒸馏 CLI +1
#: → 36 处,形状照 distill_lessons:render + build_distill_prompt)。
CALL_SITES: dict[str, str] = {
    # ── agent 会话层:提示词都来自 render ──
    "trove/agent/session.py::SessionManager.compact_session#1": "Av",
    "trove/agent/session.py::SessionManager._decompose_tasks#1": "Av",
    "trove/agent/session.py::SessionManager._interpret_followup#1": "Av",
    "trove/agent/session.py::SessionManager._synthesize_batch#1": "A",
    # ── CLI:KB 学习命令,提示词就地 render ──
    "trove/cli/commands/kb_cmds.py::register_kb_commands._cmd_learn#1": "A",
    "trove/cli/commands/kb_cmds.py::register_kb_commands._cmd_learn#2": "A",
    # ── agent 循环自身(B 通道的宿主:system/user 由调用方给,tool 回喂在
    #    _model_observation 收口)──
    "trove/llm/agent_loop.py::run_agent_loop._chat_once#1": "B",
    # ── 嵌入(E 档):唯一一条不走消息的通道 —— 送的是待向量化的文本,
    #    去向是向量。与上一条并列,因为这两个都不是「render 出来的提示词」──
    "trove/services/kb/backends/dense.py::GatewayEmbedder.embed#1": "E",
    # ── KB 构建端(离线/管理侧):schema_text/samples 走 render 参数过核 ──
    "trove/services/kb/init_pipeline.py::_draft_init_chunk#1": "Av",
    "trove/services/kb/init_pipeline.py::_draft_init_chunk#2": "A",
    "trove/services/kb/init_pipeline.py::_draft_init_chunk#3": "A",
    "trove/services/kb/semantic_draft.py::draft_semantic_annotations#1": "Av",
    "trove/services/kb/semantic_draft.py::draft_refusal_extension#1": "A",
    "trove/services/kb/synthetic.py::generate_synthetic_examples#1": "A",
    # ── 记忆:内容来自同文件的 prompt 构造器 ──
    "trove/services/memory/preferences.py::extract_and_store#1": "Ab",
    "trove/services/memory/service.py::MemoryService._capture_failure_lesson#1": "Ab",
    # ── 主动扫描的假设层(B6):LLM 只起草,render 产物存进 prompt 变量后
    #    随 messages 送出(messages 里的 system 是系统自带的固定句)──
    "trove/services/scan/hypotheses.py::propose#1": "Av",
    "trove/services/skills/service.py::SkillService.draft_with_llm#1": "Av",
    # ── 工作流节点 ──
    "trove/workflow/graphs.py::make_route_intent._classify#1": "Av",
    "trove/workflow/graphs.py::make_route_intent._rewrite_followup#1": "Av",
    "trove/workflow/nodes/analyze_error.py::make_analyze_error.analyze_error#1": "Av",
    "trove/workflow/nodes/answer.py::make_answer_metadata.answer_metadata#1": "Av",
    "trove/workflow/nodes/attribution.py::make_attribution.attribution#1": "A",
    "trove/workflow/nodes/chart.py::_llm_chart#1": "A",
    "trove/workflow/nodes/conclusion.py::make_conclusion.conclusion#1": "A",
    "trove/workflow/nodes/gen_sql.py::make_generate.generate#1": "Av",
    "trove/workflow/nodes/insights.py::make_insights.insights#1": "A",
    "trove/workflow/nodes/metadata_check.py::make_metadata_check.metadata_check#1": "Av",
    "trove/workflow/nodes/query_sketch.py::make_query_sketch.query_sketch.call_query_sketch#1": "Av",
    "trove/workflow/nodes/query_sketch.py::make_query_sketch.query_sketch.call_query_sketch#2": "Av",
    # reflect 三次:主裁决就地 render;两次裁决的 system/user 由 reflect 传进来
    "trove/workflow/nodes/reflect.py::make_reflect.reflect#1": "Av",
    "trove/workflow/nodes/reflect.py::_reask_verdict#1": "Ac",
    "trove/workflow/nodes/reflect.py::_rejudge_verdict#1": "Ac",
    "trove/workflow/nodes/semantics.py::make_semantics.semantics#1": "A",
    # ── 脚本:批量蒸馏,走 build_distill_prompt ──
    "scripts/distill_lessons.py::main#1": "Ab",
    # 历史蒸馏的教训提炼(材料来自行为记录,提示词管线与上行同源)
    "scripts/distill_history.py::main#1": "Ab",
}


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """fn 的直接后代(不穿过嵌套 def/class):保证一个调用只归属最内层函数。"""
    out: list[ast.AST] = []

    def rec(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            out.append(child)
            rec(child)

    rec(fn)
    return out


def _qualnames(tree: ast.AST) -> dict[ast.AST, str]:
    out: dict[ast.AST, str] = {}

    def rec(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                rec(child, prefix + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[child] = ".".join(prefix + [child.name])
                rec(child, prefix + [child.name])

    rec(tree, [])
    return out


def _enumerate() -> list[str]:
    """全仓 LLM 调用点 → ``<路径>::<限定名>#<序号>``(源码顺序外的稳定排序)。"""
    keys: list[str] = []
    for root in _ROOTS:
        for path in sorted((_REPO / root).rglob("*.py")):
            tree = ast.parse(path.read_text())
            rel = path.relative_to(_REPO)
            seen: set[int] = set()
            # 深的先走:一个调用归属**最内层**函数(工厂里返回的闭包不算两份)
            defs = _qualnames(tree)
            for fn, qual in sorted(defs.items(), key=lambda kv: -len(kv[1].split("."))):
                idx = 0
                for node in _own_nodes(fn):
                    if id(node) in seen:
                        continue
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in _METHODS
                    ):
                        idx += 1
                        seen.add(id(node))
                        keys.append(f"{rel}::{qual}#{idx}")
    return sorted(keys)


class TestLLMCallSiteInventory:
    def test_inventory_is_complete(self):
        """多一个调用点 → 失败(必须回答它送的文本从哪来);少一个 → 也失败。"""
        live = set(_enumerate())
        new = sorted(live - set(CALL_SITES))
        gone = sorted(set(CALL_SITES) - live)
        assert not new and not gone, (
            "LLM 调用点清单与代码不一致。\n"
            f"新增(判断它走不走 render/工具回喂,再往 CALL_SITES 加 key): {new}\n"
            f"消失(删掉对应 key): {gone}\n"
            "—— 新调用点如果自己拼 messages,它就在两条结构性通道之外。"
        )

    def test_kinds_are_closed(self):
        bad = {k: v for k, v in CALL_SITES.items() if v not in _KINDS}
        assert not bad, f"标签不在闭集里: {bad}"

    def test_gateway_entry_points_match(self):
        """网关入口与扫描面(_METHODS)必须一一对上,**两个方向都算**。

        入口的口径是**形状**(公开方法 + 异步),不是名字:写成
        ``name.startswith("chat")`` 的那些年,``embedding`` 明明是个入口却不在
        扫描面里,而它照样是一次 LLM 调用。按形状认,新入口不管叫什么名字都跑不掉。

        「异步」是**两种**:协程(``chat`` / ``chat_full`` / ``embedding``)与
        异步生成器(``chat_stream`` —— 它 ``yield`` 分片)。只认协程会把
        ``chat_stream`` 判成「扫描面多出来的」,所以两个判定要并联。
        """
        from trove.llm.gateway import LLMGateway

        live = {
            name for name, attr in vars(LLMGateway).items()
            if not name.startswith("_") and (
                inspect.iscoroutinefunction(attr)
                or inspect.isasyncgenfunction(attr)
            )
        }
        extra = sorted(live - _METHODS)
        missing = sorted(_METHODS - live)
        assert not extra and not missing, (
            f"网关与扫描面(_METHODS)不一致。网关多了 {extra} —— 往 _METHODS 加,"
            f"否则经它的调用点对清单隐形;扫描面多了 {missing} —— 网关已无此方法,"
            "连同它的调用点 key 一起删。"
        )
