"""结果集脱敏节点(设计 §5.5 G4 / I5;验收 A5-A7 · A10 · A11)。

``Masker`` 的单测证明的是「给定声明,值被改写成什么样」;本文件证明的是
**这一步真的长在链路上,且只长在链路的正确位置**:``validate`` 通过之后、
任何面向 LLM 的节点之前。两件事分开测是因为它们会各自出错 —— 引擎错了是
改写错,位置错了是**根本没改写**(原有的 P2/G3 就是后者:快径跳过 RLS)。

四条纪律在这里同时钉住:

* **A11 惰性**:没声明过 ``mask`` 的模型,升级后行为逐字节不变;
* **A7 按 scope 不按 role**:持 ``pii`` 看原文,admin 不自动 bypass(§8.4);
* **§10 不降级为明文**:salt 解析不出来 → 拒绝,并且**清空结果集**;
* **I5 模型看不到原文**:这一条由 ``test_graphs.py`` 的端到端断言接走
  (那里才拿得到 LLM 的入参快照)。
"""

from __future__ import annotations

from trove.core.config import AgentConfig, MaskingConfig
from trove.services.authz.policy import Principal, principal_to_wire
from trove.services.semantic_layer.models import (
    MaskingPolicy,
    SemanticDataset,
    SemanticField,
    SemanticModel,
)
from trove.workflow.nodes.masking import make_masking
from trove.workflow.state import WorkflowState

RAW_PHONE = "13800138888"   # 设计 §5.5 的示例形状:138****8888


def _model(
    *,
    mask: str = "partial",
    column: str = "phone",
    default_policy: str = "apply",
    bypass_scopes: tuple[str, ...] = (),
    salt_ref: str = "",
) -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[
            SemanticDataset(
                name="customers",
                source="customers",
                fields=[
                    SemanticField(name=column, expression=column, mask=mask),
                    SemanticField(name="county", expression="county"),
                ],
            )
        ],
        masking=MaskingPolicy(
            default_policy=default_policy,
            bypass_scopes=list(bypass_scopes),
            hash_salt_ref=salt_ref,
        ),
    )


class _Provider:
    """最小语义层替身:节点只调 ``model()``。"""

    def __init__(self, model: SemanticModel | None) -> None:
        self._model = model

    def model(self) -> SemanticModel | None:
        return self._model


class _BoomProvider:
    def model(self) -> SemanticModel | None:
        raise RuntimeError("provider exploded")


def _principal(*scopes: str, role: str = "user") -> dict:
    return principal_to_wire(
        Principal(
            subject="7", role=role, scopes=frozenset(scopes),
            grants=frozenset({"test_db"}),
        )
    )


def _state(**kwargs) -> WorkflowState:
    defaults = {
        "session_id": "s1",
        "question": "Phones by county",
        "sql": "SELECT phone, county FROM customers",
        "columns": ["phone", "county"],
        "rows": [[RAW_PHONE, "Alameda"]],
        "row_count": 1,
        "principal": _principal(),
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def _node(provider, config: AgentConfig | None = None):
    return make_masking(
        semantic_layer=provider, config=config or AgentConfig(target="mock/model"),
    )


async def _apply(provider, state=None, config: AgentConfig | None = None) -> dict:
    return await _node(provider, config)(state or _state())


class TestInertness:
    """A11:没声明过脱敏的部署,升级后什么都不该变。"""

    async def test_a_model_without_declarations_leaves_the_rows_alone(self):
        update = await _apply(_Provider(_model(mask="")))

        assert update["rows"] == [[RAW_PHONE, "Alameda"]]
        assert update["masking_applied"] == {"fields": {}, "bypass": False}

    async def test_none_is_the_same_as_undeclared(self):
        """``mask: none`` 与不写是同义的 —— 都是「没声明」。"""
        update = await _apply(_Provider(_model(mask="none")))

        assert update["rows"] == [[RAW_PHONE, "Alameda"]]

    async def test_no_semantic_layer_is_a_no_op(self):
        """没有语义层 = 没有声明面 = 没有可脱敏的东西(不是「没有依据」)。"""
        update = await _apply(_Provider(None))

        assert update["rows"] == [[RAW_PHONE, "Alameda"]]

    async def test_no_rows_is_a_no_op(self):
        """没执行出结果集(空表 / 未执行)→ 无事可做,且不写报告。

        ``masking_applied`` 缺席 = 这一步没产出结论,与「产出了『什么都没改』
        的结论」是两回事 —— 同 ``execution_evidence`` 的三态纪律。
        """
        update = await _apply(_Provider(_model()), _state(rows=[], row_count=0))

        assert update == {}

    async def test_the_config_switch_turns_the_node_off(self):
        """装配级关闭:节点整段不跑,连报告都不写(不是「跑了但放行」)。"""
        config = AgentConfig(target="mock/model", masking=MaskingConfig(enabled=False))

        update = await _apply(_Provider(_model()), config=config)

        assert update == {}


class TestModes:
    """A5:四种模式的输出形状;A6:``hash`` 同值同哈希。"""

    async def test_partial_keeps_the_head_and_the_tail(self):
        update = await _apply(_Provider(_model(mask="partial")))

        assert update["rows"][0][0] == "138****8888"
        assert update["rows"][0][1] == "Alameda"  # 未声明的列不动

    async def test_null_replaces_every_value(self):
        update = await _apply(_Provider(_model(mask="null")))

        assert update["rows"][0][0] is None

    async def test_hash_is_stable_for_equal_values(self, monkeypatch):
        """A6 的全部依据:同盐同值必同哈希,否则分组统计会被拆开。"""
        monkeypatch.setenv("TROVE_MASK_SALT", "s3cret")
        rows = [[RAW_PHONE, "Alameda"], [RAW_PHONE, "Alameda"], ["13900139999", "Alameda"]]
        state = _state(rows=rows, row_count=3)

        update = await _apply(
            _Provider(_model(mask="hash", salt_ref="env:TROVE_MASK_SALT")), state,
        )

        first, second, third = (row[0] for row in update["rows"])
        assert first == second, "同值必须同哈希 —— 否则 COUNT(DISTINCT) 被拆成两行"
        assert first != third
        assert RAW_PHONE not in first

    async def test_an_unresolvable_salt_refuses_instead_of_leaking(self, monkeypatch):
        """§10:``hash`` 要应用而 salt 解析不出来 → **拒绝**,不降级为明文。"""
        monkeypatch.delenv("TROVE_MASK_SALT", raising=False)

        update = await _apply(
            _Provider(_model(mask="hash", salt_ref="env:TROVE_MASK_SALT")),
        )

        assert update["error"].startswith("[ERR:MASKING]")
        assert update["rows"] == []

    async def test_a_plain_partial_deployment_needs_no_salt(self):
        """只有 ``partial`` 的部署不该因为没配 salt 而全线拒绝。"""
        update = await _apply(_Provider(_model(mask="partial", salt_ref="")))

        assert "error" not in update


class TestScopes:
    """A7:按 ``Principal.scopes`` 决定看什么,**不按 role**。"""

    async def test_without_the_scope_the_declared_column_is_masked(self):
        update = await _apply(_Provider(_model()))

        assert update["rows"][0][0] == "138****8888"
        assert update["masking_applied"]["bypass"] is False

    async def test_with_the_scope_the_raw_value_passes_through(self):
        """持 ``pii`` → 原样,且报告显式记为 bypass(否则调用方分不清
        「没有可脱敏的字段」与「整段被跳过」)。"""
        state = _state(principal=_principal("pii"))

        update = await _apply(_Provider(_model(bypass_scopes=("pii",))), state)

        assert update["rows"][0][0] == RAW_PHONE
        assert update["masking_applied"] == {"fields": {}, "bypass": True}

    async def test_an_admin_without_the_scope_is_still_masked(self):
        """§8.4:admin **不自动** bypass。

        要看得见原文就显式持 ``pii`` —— 这样「谁签发过看得见原文的凭证」在
        token 侧留有记录,而不是靠角色隐式获得。
        """
        state = _state(principal=_principal(role="admin"))

        update = await _apply(_Provider(_model(bypass_scopes=("pii",))), state)

        assert update["rows"][0][0] == "138****8888"
        assert update["masking_applied"]["bypass"] is False

    async def test_no_principal_is_masked_not_unrestricted(self):
        """没有主体 → 多脱敏,不是放行(与 I7 的「没有依据 → 拒绝」同向)。"""
        update = await _apply(_Provider(_model()), _state(principal=None))

        assert update["rows"][0][0] == "138****8888"


class TestFailClosed:
    """没有下层兜底的方向必须严(§8.1 判据)。"""

    async def test_an_unreadable_semantic_model_refuses(self):
        """读不出声明面 = 不知道有没有声明 → 不能把原文放走。

        「读不出来」与「没有声明」是两回事:后者是 A11 的惰性场景(语义层
        未接),前者是故障。把故障当成惰性,等于让一次 provider 抖动变成一次
        PII 披露。
        """
        update = await _apply(_BoomProvider())

        assert update["error"].startswith("[ERR:MASKING]")
        assert update["rows"] == []

    async def test_a_refusal_also_clears_the_rows(self):
        """拒绝时**必须清空结果集**,不能只置 error 就交给下游。

        下游的 insights / chart / conclusion 确实都看 ``state.error``,但那是
        节点之间的约定;原文一旦留在 state 里,将来任何一个新节点忘了这条约定
        就是一次泄漏。清空把「约定」变成「结构」。
        """
        update = await _apply(_Provider(_model(mask="hash", salt_ref="env:NOT_SET_ANYWHERE")))

        assert update["rows"] == []
        assert update["columns"] == []


class TestReporting:
    async def test_it_reports_the_fields_it_actually_rewrote(self):
        update = await _apply(_Provider(_model()))

        assert update["masking_applied"]["fields"] == {"phone": "partial"}

    async def test_an_undeclared_column_is_not_reported(self):
        """报告里只出现**被改写过的**字段(前端据此标「此列已脱敏」)。"""
        update = await _apply(_Provider(_model()))

        assert "county" not in update["masking_applied"]["fields"]

    async def test_the_metric_counts_what_it_masked(self):
        from trove.core.metrics import render_metrics

        await _apply(_Provider(_model()))

        text = render_metrics().decode()
        assert 'trove_masking_applied_total{' in text
        assert 'field="phone"' in text
        assert 'mode="partial"' in text
