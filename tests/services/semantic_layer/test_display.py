"""``semantic_layer.display`` 单测(E7 §5.4(g) / FR-DEC-1):声明 → 显示名。

单一纪律:**声明了就给,没声明就是没有**——三级值回退(enum_display →
value_aliases 首别名 → 实体自然键)、大小写不敏感兜底、歧义/未声明一律
``None``(调用方缺席容忍:键不出现,绝不猜)。层级 display_name **没有
回退**(与 object_graph 同一读侧纪律,不发明)。

纯函数、零 I/O:直接建 ``SemanticModel`` 断言。
"""

from __future__ import annotations

from trove.services.semantic_layer.display import (
    field_display,
    hierarchy_at,
    next_level_ref,
    ref_field_display,
    ref_next_level,
    ref_value_display,
    resolve_ref,
    value_display,
)
from trove.services.semantic_layer.models import (
    Hierarchy,
    SemanticDataset,
    SemanticEntity,
    SemanticField,
    SemanticModel,
)


def _model() -> SemanticModel:
    return SemanticModel(
        name="m",
        datasets=[
            SemanticDataset(
                name="loan", source="loan",
                fields=[
                    SemanticField(
                        name="purpose", expression="loan.purpose",
                        semantic_role="enum",
                        enum_display={"M": "monthly", "W": "weekly"},
                    ),
                    SemanticField(
                        name="frequency", expression="loan.frequency",
                        semantic_role="enum",
                        value_aliases={"M": ["monthly", "月结"]},
                    ),
                    SemanticField(
                        name="district", expression="loan.district",
                        semantic_role="dimension",
                    ),
                    SemanticField(
                        name="loan_id", expression="loan.loan_id",
                        semantic_role="identifier",
                    ),
                ],
                entity=SemanticEntity(display_name="贷款", natural_keys=["loan_id"]),
                primary_key=["loan_id"],
            ),
            SemanticDataset(
                name="branch", source="branch",
                fields=[
                    SemanticField(name="branch_id", expression="branch.branch_id",
                                  semantic_role="identifier"),
                    SemanticField(name="province", expression="branch.province",
                                  semantic_role="dimension"),
                    SemanticField(name="city", expression="branch.city",
                                  semantic_role="dimension"),
                ],
            ),
            SemanticDataset(
                name="staff", source="staff",
                fields=[
                    SemanticField(name="staff_id", expression="staff.staff_id",
                                  semantic_role="identifier"),
                    SemanticField(name="name", expression="staff.name",
                                  semantic_role="dimension"),
                ],
                entity=SemanticEntity(display_name="员工"),
                primary_key=["staff_id"],
            ),
        ],
        hierarchies=[
            Hierarchy(name="geo", display_name="地理", dataset="branch",
                      levels=["province", "city"]),
        ],
    )


class TestResolveRef:
    def test_basic_and_unknown(self):
        m = _model()
        ds, fld = resolve_ref(m, "loan.purpose")
        assert ds.name == "loan" and fld.name == "purpose"
        assert resolve_ref(m, "loan.nope") is None
        assert resolve_ref(m, "nope.purpose") is None
        assert resolve_ref(m, "") is None
        assert resolve_ref(m, "loan") is None  # 无字段段
        assert resolve_ref(m, "loan.purpose.extra") is None  # 字段名不许带点

    def test_dataset_name_with_dot(self):
        """云仓 ``project.dataset`` 名含点 —— 从长到短的声明名前缀匹配。"""
        m = SemanticModel(name="m", datasets=[SemanticDataset(
            name="proj.ds",
            fields=[SemanticField(name="region", expression="proj.ds.region")],
        )])
        ds, fld = resolve_ref(m, "proj.ds.region")
        assert ds.name == "proj.ds" and fld.name == "region"

    def test_case_insensitive_spelling(self):
        m = _model()
        ds, fld = resolve_ref(m, "LOAN.PURPOSE")
        assert ds.name == "loan" and fld.name == "purpose"


class TestValueDisplay:
    def test_enum_display_exact(self):
        assert value_display(_model(), "loan", "purpose", "M") == "monthly"
        assert value_display(_model(), "loan", "purpose", "W") == "weekly"

    def test_enum_display_case_insensitive_fallback(self):
        """存储值被驱动归一过拼写(``m`` vs 声明的 ``M``)→ 兜底命中。"""
        assert value_display(_model(), "loan", "purpose", "m") == "monthly"

    def test_value_aliases_first_non_empty(self):
        assert value_display(_model(), "loan", "frequency", "M") == "monthly"

    def test_undeclared_value_is_none(self):
        m = _model()
        assert value_display(m, "loan", "purpose", "X") is None
        assert value_display(m, "loan", "district", "东区") is None
        assert value_display(m, "loan", "nope", "M") is None
        assert value_display(m, "nope", "purpose", "M") is None

    def test_entity_natural_key(self):
        m = _model()
        assert value_display(m, "loan", "loan_id", "L1") == "贷款 L1"

    def test_entity_primary_key_fallback_when_no_natural_keys(self):
        m = _model()
        # staff 无 natural_keys → 回退 primary_key;display_name 有 → 用之
        assert value_display(m, "staff", "staff_id", "S7") == "员工 S7"

    def test_non_key_field_no_entity_fallback(self):
        m = _model()
        # 实体存在但字段不是自然键/主键 → 不套实体显示名
        assert value_display(m, "loan", "district", "X1") is None

    def test_ref_value_display(self):
        m = _model()
        assert ref_value_display(m, "loan.purpose", "M") == "monthly"
        assert ref_value_display(m, "loan.nope", "M") is None


class TestHierarchy:
    def test_hierarchy_at_and_ambiguity(self):
        m = _model()
        hidx, lidx, h = hierarchy_at(m, "branch", "province")
        assert (hidx, lidx, h.name) == (0, 0, "geo")
        assert hierarchy_at(m, "branch", "city")[1] == 1
        assert hierarchy_at(m, "branch", "branch_id") is None
        assert hierarchy_at(m, "loan", "province") is None  # 数据集不符
        # province 同时出现在 geo 与 bare 两条层级 → 歧义,不猜
        ambiguous = SemanticModel(
            name="m", datasets=m.datasets,
            hierarchies=[
                Hierarchy(name="h1", dataset="branch", levels=["province"]),
                Hierarchy(name="h2", dataset="branch", levels=["province"]),
            ],
        )
        assert hierarchy_at(ambiguous, "branch", "province") is None

    def test_field_display_only_when_declared(self):
        m = _model()
        assert field_display(m, "branch", "province") == "地理"
        assert field_display(m, "branch", "city") == "地理"
        assert field_display(m, "branch", "branch_id") is None  # 非级别
        # 层级 display_name 无回退:未声明就是没有(不发明)
        bare = SemanticModel(
            name="m", datasets=m.datasets,
            hierarchies=[Hierarchy(name="bare", dataset="branch",
                                   levels=["province"])],
        )
        assert field_display(bare, "branch", "province") is None
        assert ref_field_display(m, "branch.city") == "地理"
        assert ref_field_display(m, "branch.nope") is None

    def test_next_level_ref(self):
        m = _model()
        assert next_level_ref(m, "branch", "province") == "branch.city"
        assert next_level_ref(m, "branch", "city") is None  # 末级
        assert next_level_ref(m, "branch", "branch_id") is None  # 非级别
        assert ref_next_level(m, "branch.province") == "branch.city"
        assert ref_next_level(m, "branch.nope") is None
