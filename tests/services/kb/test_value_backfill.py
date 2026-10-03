"""值探测与回填(车道 B):``probe_values`` / ``_backfill_values`` / 装载 / lint。

预冻结接口 Ⅰ:字段级 ``values: ["Sokolov", …]``(≤100,全 string)——
该列**实际取值**的结构事实,只被值路由读。它不提升 ``semantic_role``、
不进提示词渲染、不参与占比构造;本文件守住的是"它只是数据"这条边界:

  - 第二档探测的上限/日期排除/超时静默(枚举档 ``PROBE_LIMIT=20`` 不动);
  - 回填只**新增**缺失键,已有 values/enum_display/value_aliases 的字段不动,
    永不改名/新建(重跑幂等);
  - 装载侧对缺省/坏形状的容忍(存量模型一字不变);
  - lint 的坏形状拦截(非 string 数组、超 100 条、时间字段)。
"""

import asyncio

import pytest

from trove.services.kb.enum_probe import (
    PROBE_LIMIT,
    VALUE_PROBE_LIMIT,
    probe_enums,
    probe_values,
)
from trove.services.kb.init_pipeline import _backfill_values
from trove.services.semantic_layer.models import MAX_FIELD_VALUES
from trove.services.semantic_layer.ossie import parse_ossie


class _SlowRegistry:
    """execute 永不按时返回的假注册表(慢列/超时场景)。

    只实现 ``probe_values`` 用到的那一个方法 —— 探测是尽力而为的旁路,
    它的依赖面越小越好断言。
    """

    def __init__(self, delay: float = 5.0):
        self.delay = delay

    async def execute(self, sql: str):  # noqa: ARG002 - 假实现只吃调用
        await asyncio.sleep(self.delay)
        raise AssertionError("probe should have timed out")


class _BrokenRegistry:
    """execute 抛异常的假注册表(列不存在/权限/连接断)。"""

    async def execute(self, sql: str):
        raise RuntimeError(f"boom: {sql}")


class TestProbeValues:
    """第二档取值探测:仅文本列、排除日期、完整才落、超时静默。"""

    async def test_covers_columns_beyond_enum_tier(self, sqlite_registry):
        """枚举档(≤20)够不到的列正是第二档的用武之地:两档同探一列,
        档位不同、产物不同(枚举档跳过 >20 的高基数列)。"""
        schema = await sqlite_registry.get_schema()
        enums = await probe_enums(sqlite_registry, schema)
        values = await probe_values(sqlite_registry, schema)
        assert "name" in enums["students"]       # 5 个 → 枚举档收录
        assert values["students"]["name"] == [
            "Alice", "Bob", "Carol", "Dave", "Eve",
        ]
        # 数值列不是值词表(两档都不探)
        assert "grade" not in values["students"]

    async def test_limit_is_the_second_tier(self, sqlite_registry):
        """同一个 5 取值列:limit=3 → 超界不落(残缺取值表比没有更坏);
        limit=5 → 完整收录。"""
        schema = await sqlite_registry.get_schema()
        over = await probe_values(sqlite_registry, schema, limit=3)
        assert "name" not in over.get("students", {})
        assert over["students"]["county"] == ["Alameda", "Los Angeles", "Orange"]
        exact = await probe_values(sqlite_registry, schema, limit=5)
        assert exact["students"]["name"] == [
            "Alice", "Bob", "Carol", "Dave", "Eve",
        ]

    async def test_default_limit_matches_field_cap(self):
        """探测上限与装载上限是同一个界(任一改了另一边必须跟着改)。"""
        assert VALUE_PROBE_LIMIT == MAX_FIELD_VALUES == 100

    async def test_skips_date_typed_and_date_shaped_text(self, sqlite_registry):
        """日期列两种形态都排除:类型带日期标记的,以及取值像日期的文本列
        (BIRD 把 YYMMDD 存成 TEXT,类型看不出,取值看得出)。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY, "
            "happened_on DATE, "
            "stamp TEXT, "
            "kind TEXT)")
        await adapter.execute(
            "INSERT INTO events (happened_on, stamp, kind) VALUES "
            "('1993-01-15', '930115', 'open'), "
            "('1994-06-01', '940601', 'closed')")
        schema = await sqlite_registry.get_schema()
        values = await probe_values(sqlite_registry, schema)
        assert "happened_on" not in values.get("events", {})
        assert "stamp" not in values.get("events", {})
        assert values["events"]["kind"] == ["closed", "open"]  # 已排序

    async def test_timeout_silently_skipped(self):
        """慢列超时静默跳过:探测是尽力而为,绝不把异常抛给 init。"""
        schema = await _students_schema()
        assert await probe_values(
            _SlowRegistry(), schema, timeout_s=0.01) == {}

    async def test_failure_silently_skipped(self):
        """坏列(执行异常)同样静默 —— 与超时同一条兜底。"""
        schema = await _students_schema()
        assert await probe_values(_BrokenRegistry(), schema) == {}

    async def test_values_deduped_and_stripped(self, sqlite_registry):
        """去空白 + 去重 + 排序:同一份数据每次探测产出同一份 YAML
        (回填幂等的前提)。"""
        adapter = await sqlite_registry.get()
        await adapter.execute(
            "CREATE TABLE tags (id INTEGER PRIMARY KEY, label TEXT)")
        await adapter.execute(
            "INSERT INTO tags (label) VALUES (' b'), ('a'), ('a '), ('b')")
        schema = await sqlite_registry.get_schema()
        values = await probe_values(sqlite_registry, schema)
        assert values["tags"]["label"] == ["a", "b"]


async def _students_schema():
    """建一个独立的 sqlite registry 只取 schema(超时/异常用例的夹具)。"""
    from trove.core.types import DatasourceConfig
    from trove.services.datasource.registry import ConnectorRegistry

    registry = ConnectorRegistry()
    await registry.register(
        DatasourceConfig(name="t", type="sqlite",
                         connection_params={"path": ":memory:"}, default=True),
        set_default=True,
    )
    adapter = await registry.get()
    await adapter.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT)")
    schema = await registry.get_schema()
    await registry.close_all()
    return schema


def _doc() -> dict:
    """一份最小语义模型文档:一个 dataset + 三个字段(含时间字段)。"""

    def fld(name, datatype="String", **extra):
        entry = {
            "name": name,
            "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": name}]},
            "datatype": datatype,
        }
        entry.update(extra)
        return entry

    return {
        "version": "0.2.0.dev0",
        "semantic_model": [{
            "name": "fin",
            "datasets": [{
                "name": "district",
                "fields": [
                    fld("district_id", "Integer", semantic_role="identifier"),
                    fld("A2"),
                    fld("A3"),
                    fld("A4"),
                    fld("updated_at", "Date", semantic_role="time"),
                ],
            }],
            "metrics": [{"name": "m", "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "COUNT(district.district_id)"}]}}],
            "relationships": [],
        }],
    }


PROBED = {
    "district": {
        "A2": ["Benesov", "Sokolov"],
        "A3": ["east Bohemia", "west Bohemia"],
        "A4": ["1000", "2000"],
        "updated_at": ["900101", "910101"],
    },
}


class TestBackfillValues:
    """回填:只新增缺失的 ``values:`` 键。"""

    def test_adds_missing_values_only(self):
        doc = _doc()
        added = _backfill_values(doc, PROBED)
        fields = {f["name"]: f for f in doc["semantic_model"][0]["datasets"][0]["fields"]}
        assert fields["A2"]["values"] == ["Benesov", "Sokolov"]
        assert fields["A3"]["values"] == ["east Bohemia", "west Bohemia"]
        assert added == {"district.A2": 2, "district.A3": 2, "district.A4": 2}

    def test_idempotent(self):
        """跑两次:第二次零改动(排序 + DISTINCT 保证取值集合稳定)。"""
        doc = _doc()
        _backfill_values(doc, PROBED)
        import copy

        snapshot = copy.deepcopy(doc)
        added = _backfill_values(doc, PROBED)
        assert added == {}
        assert doc == snapshot

    def test_does_not_touch_existing_vocabulary(self):
        """已有 values / enum_display / value_aliases 的字段一律不动。"""
        doc = _doc()
        fields = {f["name"]: f for f in doc["semantic_model"][0]["datasets"][0]["fields"]}
        fields["A2"]["values"] = ["hand-written"]
        fields["A3"]["enum_display"] = {"east Bohemia": "east"}
        fields["A4"]["ai_context"] = {"value_aliases": {"1000": ["one thousand"]}}
        added = _backfill_values(doc, PROBED)
        assert added == {}
        assert fields["A2"]["values"] == ["hand-written"]
        assert fields["A3"]["enum_display"] == {"east Bohemia": "east"}
        assert "values" not in fields["A3"]
        assert "values" not in fields["A4"]

    def test_explicit_empty_values_key_is_respected(self):
        """``values: []`` 是**声明过**的键(作者显式说过"没有"),不当缺失补。"""
        doc = _doc()
        fields = {f["name"]: f for f in doc["semantic_model"][0]["datasets"][0]["fields"]}
        fields["A2"]["values"] = []
        added = _backfill_values(doc, PROBED)
        assert "district.A2" not in added
        assert fields["A2"]["values"] == []

    def test_temporal_field_never_gets_values(self):
        """时间字段不写(日期取值是时点,不是取值词表;lint 同样会拦)。"""
        doc = _doc()
        _backfill_values(doc, PROBED)
        fields = {f["name"]: f for f in doc["semantic_model"][0]["datasets"][0]["fields"]}
        assert "values" not in fields["updated_at"]
        assert "values" not in fields["district_id"]  # 探测结果里没有的列不动

    def test_unknown_table_and_column_ignored(self):
        """对不上的表/列静默丢弃 —— 回填永不新建字段。"""
        doc = _doc()
        added = _backfill_values(doc, {"ghost": {"A2": ["x"]}, "district": {"ghost": ["y"]}})
        assert added == {}
        names = [f["name"] for f in doc["semantic_model"][0]["datasets"][0]["fields"]]
        assert names == ["district_id", "A2", "A3", "A4", "updated_at"]

    def test_never_renames_anything(self):
        """隔离线:回填只加键,不碰任何名字(与 regen --write 的改名风险隔离)。"""
        doc = _doc()
        before_names = _names(doc)
        _backfill_values(doc, PROBED)
        assert _names(doc) == before_names

    def test_empty_probe_returns_empty(self):
        doc = _doc()
        assert _backfill_values(doc, {}) == {}
        assert all(
            "values" not in f
            for f in doc["semantic_model"][0]["datasets"][0]["fields"]
        )


def _names(doc: dict) -> tuple:
    """文档里所有指标/数据集/字段名(回填前后必须逐字相同)。"""
    entry = doc["semantic_model"][0]
    return (
        tuple(m["name"] for m in entry.get("metrics", [])),
        tuple(d["name"] for d in entry.get("datasets", [])),
        tuple(f["name"] for d in entry.get("datasets", []) for f in d["fields"]),
    )


class TestValuesLoader:
    """装载侧:``values`` 是数据 —— 容忍缺省,不猜坏形状,不改角色。"""

    def test_round_trip(self):
        doc = _doc()
        _backfill_values(doc, PROBED)
        import yaml

        model = parse_ossie(yaml.safe_dump(doc), preferred_dialect="sqlite")
        district = model.datasets[0]
        a2 = next(f for f in district.fields if f.name == "A2")
        assert a2.values == ["Benesov", "Sokolov"]
        # 纯数据:角色/枚举词表一个都不动
        assert a2.semantic_role == ""
        assert a2.enum_display == {}
        assert a2.value_aliases == {}
        # 时间字段没被回填 → 装载为空列表
        assert next(f for f in district.fields if f.name == "updated_at").values == []

    def test_absent_key_defaults_empty(self):
        """存量模型(无 values 键)解析结果一字不变 —— 空列表。"""
        yaml_text = """
semantic_model:
  - name: m
    datasets:
      - name: client
        fields:
          - name: gender
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: gender
            semantic_role: enum
            enum_display: {F: female, M: male}
"""
        gender = parse_ossie(yaml_text, preferred_dialect="sqlite").datasets[0].fields[0]
        assert gender.values == []
        assert gender.semantic_role == "enum"   # 角色不被 values 影响

    def test_normalizes_and_truncates(self):
        """strip、丢空串、截断到上限;坏形状(映射/嵌套)忽略而不是猜。"""
        raw = [" '  v0  ' ", " 'v1' ", "''", "null", " 7 "]
        raw += [f" ' v{i} '" for i in range(2, MAX_FIELD_VALUES + 5)]
        raw += ["{bad: 1}", "['nested']"]
        yaml_text = (
            "semantic_model:\n"
            "  - name: m\n"
            "    datasets:\n"
            "      - name: t\n"
            "        fields:\n"
            "          - name: c\n"
            "            expression: {dialects: [{dialect: ANSI_SQL, expression: c}]}\n"
            "            values: [" + ", ".join(raw) + "]\n"
        )
        values = parse_ossie(yaml_text, preferred_dialect="sqlite").datasets[0].fields[0].values
        assert len(values) == MAX_FIELD_VALUES
        assert values[:2] == ["v0", "v1"]
        assert "7" in values          # 数字归一成字符串(值域事实)
        assert "" not in values and "None" not in values  # 空串/裸 null 被丢掉
        assert "bad" not in values and "nested" not in values  # 坏形状忽略
        assert all(isinstance(v, str) and v.strip() for v in values)
        assert f"v{MAX_FIELD_VALUES + 4}" not in values  # 超限截断

    def test_bad_shape_ignored(self):
        """``values`` 写成映射(不是数组)→ 空列表(不猜),lint 会在写盘前拦。"""
        yaml_text = """
semantic_model:
  - name: m
    datasets:
      - name: t
        fields:
          - name: c
            expression: {dialects: [{dialect: ANSI_SQL, expression: c}]}
            values: {a: b}
"""
        assert parse_ossie(yaml_text, preferred_dialect="sqlite").datasets[0].fields[0].values == []


class TestEnumTierUntouched:
    """第一档(枚举)的形状与上限不动 —— 第二档是**新增**,不是改造。"""

    def test_enum_probe_limit_unchanged(self):
        assert PROBE_LIMIT == 20

    async def test_enum_probe_output_unchanged(self, sqlite_registry):
        schema = await sqlite_registry.get_schema()
        probed = await probe_enums(sqlite_registry, schema, max_rows=100)
        assert probed["students"]["name"] == "Alice; Bob; Carol; Dave; Eve"


@pytest.mark.parametrize("limit", [1, 2])
async def test_probe_values_never_exceeds_limit(sqlite_registry, limit):
    """超界即跳过:任何探测结果都不超过 limit(残缺取值表不落库)。"""
    schema = await sqlite_registry.get_schema()
    values = await probe_values(sqlite_registry, schema, limit=limit)
    assert all(len(v) <= limit for cols in values.values() for v in cols.values())
