"""LLM 数据出境治理单测:工具观测 / few-shot 样例值的结构化脱敏。

钉死的三条边界:

1. **没声明就没行为**(§"未配置 = 逐字节不变"):无 ``mask`` 声明、无语义层、
   关闭开关 → 观测原样通过,报告为空。
2. **失败方向是扣留**,不是回落原文:结构异常 / 位置信息不可用且有声明 /
   hash salt 解析不出 → ``[ERR:EGRESS_REDACT]``(样例值则整段剥掉)。
3. **egress 与 ``masking.enabled`` 解耦**:关掉交付前脱敏不等于允许把声明
   字段发给模型 —— redactor 从不读 ``masking.enabled``。
"""

from __future__ import annotations

import hashlib

import pytest

from trove.core.config import AgentConfig, EgressConfig, MaskingConfig
from trove.services.authz.egress import (
    WITHHELD_OBSERVATION,
    DATA_TOOLS,
    EgressRedactor,
    egress_entry,
    merge_egress_reports,
)
from trove.services.semantic_layer.models import (
    MaskingPolicy,
    SemanticDataset,
    SemanticField,
    SemanticModel,
)

SALT_ENV = "TROVE_TEST_EGRESS_SALT"
SALT = "egress-salt"
SALT_REF = f"env:{SALT_ENV}"


@pytest.fixture
def salt_env(monkeypatch):
    monkeypatch.setenv(SALT_ENV, SALT)
    return SALT


def _field(name: str, mask: str = "", expression: str = "") -> SemanticField:
    return SemanticField(name=name, expression=expression or name, mask=mask)


def _model(*fields: SemanticField, name: str = "customers") -> SemanticModel:
    return SemanticModel(
        datasets=[
            SemanticDataset(name=name, source=f"public.{name}", fields=list(fields))
        ],
        masking=MaskingPolicy(),
    )


class _Provider:
    """最小语义层 provider:``model()`` 返回固定模型或抛异常。"""

    def __init__(self, model: SemanticModel | None = None, error: Exception | None = None):
        self._model = model
        self._error = error

    def model(self) -> SemanticModel:
        if self._error is not None:
            raise self._error
        return self._model  # type: ignore[return-value]


def _config(**egress: object) -> AgentConfig:
    cfg = AgentConfig()
    if egress:
        cfg.egress = EgressConfig(**egress)  # type: ignore[arg-type]
    cfg.masking = MaskingConfig(hash_salt_ref=SALT_REF)
    return cfg


def _hash(value: str, salt: str = SALT) -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()[:12]


def _probe(columns: list[str], rows: list[list[object]], sql: str) -> dict:
    return {"ok": True, "row_count": len(rows), "columns": columns, "rows": rows}


# ── 没声明 / 关闭 → 逐字节不变 ────────────────────────────────


class TestInertPaths:
    def test_no_declarations_is_byte_identical(self):
        model = _model(_field("phone"), _field("city"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = _probe(["phone"], [["13812348888"]], "SELECT phone FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"}, value,
        )
        assert out is value            # 同一个对象:没有复制、没有 note
        assert report == {}

    def test_no_semantic_provider_is_inert(self):
        redactor = EgressRedactor(None, _config())
        value = _probe(["phone"], [["13812348888"]], "SELECT phone FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"}, value,
        )
        assert out is value
        assert report == {}

    def test_egress_disabled_is_inert(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config(enabled=False))
        value = _probe(["phone"], [["13812348888"]], "SELECT phone FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"}, value,
        )
        assert out is value
        assert report == {}

    def test_redact_tool_results_off_is_inert_for_tools(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(
            _Provider(model), _config(redact_tool_results=False),
        )
        value = _probe(["phone"], [["13812348888"]], "SELECT phone FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"}, value,
        )
        assert out is value
        assert report == {}

    def test_masking_disabled_does_not_disable_egress(self):
        """关掉交付前脱敏 ≠ 允许声明字段出境:egress 不读 masking.enabled。"""
        model = _model(_field("phone", mask="partial"))
        cfg = _config()
        cfg.masking = MaskingConfig(enabled=False, hash_salt_ref=SALT_REF)
        redactor = EgressRedactor(_Provider(model), cfg)
        value = _probe(["phone"], [["13812348888"]], "SELECT phone FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"}, value,
        )
        assert out["rows"] == [["138****8888"]]
        assert report["fields"] == {"phone": "partial"}


# ── probe_query rows ─────────────────────────────────────────


class TestProbeRows:
    @pytest.mark.parametrize("mode,expected", [
        ("partial", "138****8888"),
        ("null", None),
        ("hash", _hash("13812348888")),
    ])
    def test_modes_applied(self, salt_env, mode, expected):
        model = _model(_field("phone", mask=mode))
        redactor = EgressRedactor(_Provider(model), _config())
        value = _probe(["phone", "city"], [["13812348888", "sh"]],
                       "SELECT phone, city FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query",
            {"sql": "SELECT phone, city FROM customers"},
            value,
        )
        assert out["rows"] == [[expected, "sh"]]
        assert out["row_count"] == 1
        assert out["note"] == "sensitive field values are redacted"
        assert report == {
            "channel": "tool_result",
            "tool": "probe_query",
            "fields": {"phone": mode},
            "rows": 1,
            "values": 0,
        }
        # 原对象不被改写(probe_cache 跨轮共享同一份结果)
        assert value["rows"] == [["13812348888", "sh"]]
        assert "note" not in value

    def test_alias_projection_masked_by_position(self, salt_env):
        """``SELECT email AS phone``:按投影位置命中 email,不按输出列名 phone。"""
        model = _model(_field("email", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        sql = "SELECT email AS phone FROM customers"
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": sql}, _probe(["phone"], [["alice@example.com"]], sql),
        )
        assert out["rows"] == [["ali**********.com"]]
        assert report["fields"] == {"email": "partial"}

    def test_ok_false_observation_passes_through(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = {"ok": False, "error": "[ERR:SQL_SYNTAX] boom"}
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT"}, value,
        )
        assert out is value
        assert report == {}

    def test_empty_rows_pass_through(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = _probe(["phone"], [], "SELECT phone FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"}, value,
        )
        assert out is value
        assert report == {}

    def test_unparseable_sql_with_declarations_is_withheld(self):
        """位置信息不可用 + 有声明 → 整条扣留(strict 分支)。"""
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = _probe(["phone"], [["13812348888"]], "SELECT phone FROM")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM"}, value,
        )
        assert out == WITHHELD_OBSERVATION
        assert report["withheld"] is True

    def test_hash_without_salt_is_withheld(self, monkeypatch):
        monkeypatch.delenv(SALT_ENV, raising=False)
        model = _model(_field("phone", mask="hash"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = _probe(["phone"], [["13812348888"]], "SELECT phone FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"}, value,
        )
        assert out == WITHHELD_OBSERVATION
        assert report["withheld"] is True

    def test_model_read_failure_is_withheld(self):
        redactor = EgressRedactor(_Provider(error=RuntimeError("kb gone")), _config())
        value = _probe(["phone"], [["13812348888"]], "SELECT phone FROM customers")
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"}, value,
        )
        assert out == WITHHELD_OBSERVATION
        assert report["withheld"] is True

    def test_malformed_value_is_withheld(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        out, report = redactor.redact_tool_result(
            "probe_query", {"sql": "SELECT phone FROM customers"},
            {"ok": True, "rows": "not-a-list", "columns": ["phone"]},
        )
        assert out == WITHHELD_OBSERVATION
        assert report["withheld"] is True


# ── search_values ────────────────────────────────────────────


class TestSearchValues:
    def test_single_column_values_masked(self, salt_env):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = {
            "ok": True, "table": "customers", "column": "phone",
            "values": ["13812348888", "13900139000"],
        }
        out, report = redactor.redact_tool_result("search_values", {}, value)
        assert out["values"] == ["138****8888", "139****9000"]
        assert out["note"]
        assert report["channel"] == "search_values"
        assert report["values"] == 2
        assert value["values"] == ["13812348888", "13900139000"]

    def test_hits_masked_per_column(self, salt_env):
        model = _model(_field("email", mask="hash"), _field("city"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = {
            "ok": True, "table": "customers",
            "hits": {"email": ["a@example.com"], "city": ["sh"]},
        }
        out, report = redactor.redact_tool_result("search_values", {}, value)
        assert out["hits"]["email"] == [_hash("a@example.com")]
        assert out["hits"]["city"] == ["sh"]
        assert report["fields"] == {"email": "hash"}
        assert report["values"] == 1

    def test_no_declared_hit_passes_through(self):
        model = _model(_field("city"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = {"ok": True, "table": "customers", "column": "city", "values": ["sh"]}
        out, report = redactor.redact_tool_result("search_values", {}, value)
        assert out is value
        assert report == {}

    def test_check_result_only_returns_text(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        text = "VIOLATION [shape] expected one row"
        out, report = redactor.redact_tool_result("check_result", {}, text)
        assert out == text
        assert report == {}

    def test_non_data_tool_passes_through(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        value = {"ok": True, "columns": "id, phone"}
        out, report = redactor.redact_tool_result("lookup_schema", {}, value)
        assert out is value
        assert report == {}

    def test_data_tools_closed_set(self):
        assert DATA_TOOLS == {"probe_query", "search_values", "check_result"}


# ── few-shot 样例值 ─────────────────────────────────────────


def _shots(values: list[str], column: str = "customers.phone") -> list[dict]:
    return [{
        "question": "q",
        "sql": "SELECT * FROM customers WHERE phone = '{{p}}'",
        "parameters": [{
            "name": "p", "type": "dimension", "column": column,
            "sample_values": values,
        }],
    }]


class TestRedactShots:
    def test_sample_values_masked(self, salt_env):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        shots = _shots(["13812348888"])
        out, report = redactor.redact_shots(shots)
        assert out[0]["parameters"][0]["sample_values"] == ["138****8888"]
        assert report == {
            "channel": "prompt_samples",
            "tool": "prompt",
            "fields": {"phone": "partial"},
            "rows": 0,
            "values": 1,
        }
        # 入参不被改写(gen_ctx 里的原列表还归检索侧所有)
        assert shots[0]["parameters"][0]["sample_values"] == ["13812348888"]

    def test_no_sample_values_is_noop(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        shots = [{"question": "q", "sql": "SELECT 1"}]
        out, report = redactor.redact_shots(shots)
        assert out == shots
        assert report == {}

    def test_undeclared_column_unchanged(self):
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(_Provider(model), _config())
        shots = _shots(["sh"], column="customers.city")
        out, report = redactor.redact_shots(shots)
        assert out == shots
        assert report == {}

    def test_failure_strips_sample_values(self, monkeypatch):
        """hash 无 salt → 不是「原样发出去」,而是样例值整段剥掉。"""
        monkeypatch.delenv(SALT_ENV, raising=False)
        model = _model(_field("phone", mask="hash"))
        redactor = EgressRedactor(_Provider(model), _config())
        shots = _shots(["13812348888"])
        out, report = redactor.redact_shots(shots)
        assert "sample_values" not in out[0]["parameters"][0]
        assert report["withheld"] is True
        assert shots[0]["parameters"][0]["sample_values"] == ["13812348888"]

    def test_shots_gated_by_enabled_not_redact_tool_results(self, salt_env):
        """``redact_tool_results`` 只管工具观测;样例值归 egress.enabled。"""
        model = _model(_field("phone", mask="partial"))
        redactor = EgressRedactor(
            _Provider(model), _config(redact_tool_results=False),
        )
        out, _report = redactor.redact_shots(_shots(["13812348888"]))
        assert out[0]["parameters"][0]["sample_values"] == ["138****8888"]


# ── 汇总合并 ─────────────────────────────────────────────────


class TestMergeReports:
    def test_counts_add_and_fields_union(self):
        base = {"probe_query": {"fields": {"email": "hash"}, "rows": 2, "values": 0}}
        extra = {
            "probe_query": {"fields": {"phone": "partial"}, "rows": 1, "values": 0},
            "prompt": {"fields": {"email": "hash"}, "rows": 0, "values": 3},
        }
        merged = merge_egress_reports(base, extra)
        assert merged["probe_query"] == {
            "fields": {"email": "hash", "phone": "partial"}, "rows": 3, "values": 0,
        }
        assert merged["prompt"]["values"] == 3

    def test_withheld_counts(self):
        merged = merge_egress_reports(
            {"probe_query": {"fields": {}, "rows": 0, "values": 0, "withheld": 1}},
            {"probe_query": {"fields": {}, "rows": 0, "values": 0, "withheld": 2}},
        )
        assert merged["probe_query"]["withheld"] == 3

    def test_empty_reports(self):
        assert merge_egress_reports(None, None) == {}
        assert merge_egress_reports({}, egress_entry({})) == {}
