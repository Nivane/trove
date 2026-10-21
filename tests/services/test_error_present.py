"""错误呈现层测试:内部错误 → 「人话 + 机器细节」双层结构。

设计约束(见设计文档 §错误呈现):
- 面向用户的字段(title/explanation/suggestion)不得出现流水线内部词汇:
  节点名(schema_linking / gen_sql)、规则号(F1_shape)、状态机术语
  (优雅降级 / 无档可升 / 回退目标)。
- 机器可读的原始文本必须原样保留在 detail 里,供管理端/日志排查。
"""

from __future__ import annotations

import re

import pytest

from trove.core.errors import LLMError
from trove.services.errors import classify_error
from trove.services.errors.present import present_error, present_exception

# 内部词汇黑名单:用户可见字段里出现即判失败。
_INTERNAL = re.compile(
    r"schema_linking|gen_sql|query_sketch|route_intent|F\d+_\w+|"
    r"优雅降级|无档可升|回退目标|no_progress|fixer|revisor|"
    r"\[ERR:|PLAN_DRIFT|BUDGET"
)


@pytest.fixture
def degrade_error() -> str:
    return "回退目标 schema_linking 连续失败且无档可升，优雅降级"


def _user_visible(info: dict) -> str:
    return " ".join(
        str(info.get(k, "")) for k in ("title", "explanation", "suggestion")
    )


class TestDegradeFromAnalyzeError:
    def test_is_retryable_and_recommends_rephrasing(self, degrade_error):
        info = present_error(degrade_error, lang="zh")
        assert info["kind"] == "gave_up"
        assert info["retryable"] is True
        assert info["suggestion"]

    def test_user_fields_are_free_of_pipeline_vocabulary(self, degrade_error):
        for lang in ("zh", "en"):
            info = present_error(degrade_error, lang=lang)
            visible = _user_visible(info)
            assert not _INTERNAL.search(visible), visible

    def test_keeps_raw_text_in_detail(self, degrade_error):
        info = present_error(degrade_error, lang="zh")
        assert info["detail"]["raw"] == degrade_error
        # 原始错误类标识留档(供日志/管理端归因)
        assert info["detail"]["node"]
        assert info["detail"]["error_class"]

    def test_lang_switch_changes_copy_not_structure(self, degrade_error):
        zh = present_error(degrade_error, lang="zh")
        en = present_error(degrade_error, lang="en")
        assert zh["title"] != en["title"]
        assert zh["detail"]["raw"] == en["detail"]["raw"]
        assert zh["retryable"] == en["retryable"]


class TestNoProgressStop:
    def test_explains_repeated_attempts_in_business_terms(self):
        info = present_error("连续 3 轮修复无进展(invalid),停止迭代,优雅降级", lang="zh")
        assert info["kind"] == "gave_up"
        visible = _user_visible(info)
        assert not _INTERNAL.search(visible), visible
        # 告诉了用户"试了几次仍不成",但用的是人话
        assert "3" in visible or "几" in visible


class TestClassifiedFailures:
    def test_datasource_auth_points_at_credentials(self):
        # 凭据/权限类失败不该诱导用户反复点重试 —— 该去找管理员。
        info = present_error("[ERR:DS_AUTH] permission denied for user", lang="zh")
        assert info["retryable"] is False
        visible = _user_visible(info)
        assert "权限" in visible or "凭据" in visible, visible
        assert not _INTERNAL.search(visible)

    def test_budget_is_labelled_as_scale_problem(self):
        info = present_error("agent budget exhausted; degrading gracefully", lang="zh")
        assert info["kind"] in ("gave_up", "too_complex")
        assert not _INTERNAL.search(_user_visible(info))

    def test_unknown_error_still_yields_actionable_copy(self):
        info = present_error("something exploded", lang="zh")
        assert info["title"]
        assert info["suggestion"]
        assert not _INTERNAL.search(_user_visible(info))


class TestActionExits:
    """一键出口(``actions``):与拒绝侧 ``next_actions`` 同形状同纪律。

    后端给全(含提问语言的 label),前端只渲染不分类;没有真实管理端落点的
    类别给空列表 —— 编一个不存在的链接,比没有链接更坏。
    """

    def test_datasource_failure_points_at_connections(self):
        info = present_error("[ERR:DS_TRANSIENT] connection refused", lang="zh")
        assert len(info["actions"]) == 1
        action = info["actions"][0]
        assert action["id"] == action["kind"] == "datasource"
        assert action["href"] == "/admin/datasources"
        assert action["label"] == "检查数据源连接"
        assert action["admin_only"] is True

    def test_model_failure_points_at_model_config(self):
        info = present_error("[ERR:LLM_TRANSIENT] upstream timeout", lang="en")
        assert [a["href"] for a in info["actions"]] == ["/admin/model-config"]
        assert info["actions"][0]["label"] == "Check the model configuration"

    def test_categories_without_admin_landing_have_no_actions(self):
        # 权限失败的正确出口是「找管理员」(没有自助页);sql/放弃/未知类同理。
        for text in (
            "[ERR:DS_AUTH] permission denied",
            "[ERR:SQL_SYNTAX] bad sql",
            "连续 3 轮修复无进展(invalid)，停止迭代，优雅降级",
            "boom",
        ):
            assert present_error(text, lang="zh")["actions"] == [], text

    def test_action_labels_are_free_of_pipeline_vocabulary(self):
        for text in ("[ERR:DS_TRANSIENT] x", "[ERR:LLM_TRANSIENT] x"):
            for lang in ("zh", "en"):
                for action in present_error(text, lang=lang)["actions"]:
                    assert action["label"], action
                    assert not _INTERNAL.search(action["label"]), action

    def test_action_shape_matches_refusal_contract(self):
        for action in present_error("[ERR:DS_TRANSIENT] x", lang="zh")["actions"]:
            assert set(action) <= {
                "id", "kind", "label", "href", "admin_only", "payload",
            }
            assert action["id"] == action["kind"]
            assert action["href"].startswith("/admin/")


class TestExceptionNormalization:
    """未捕获异常(会话边界)走同一份呈现层,不把 str(e) 直出给用户。

    ``present_exception`` 只做一件事:按异常类型选故障面(context),再复用
    ``present_error``。分类与文案仍然只有一处。
    """

    def test_llm_auth_failure_is_a_model_problem_not_a_datasource_one(self):
        exc = LLMError(
            message="LLM call failed after 3 attempts: invalid api key",
        )
        info = present_exception(exc, lang="zh")
        assert info["kind"] == "model"
        assert info["retryable"] is False
        assert [a["href"] for a in info["actions"]] == ["/admin/model-config"]
        visible = _user_visible(info)
        assert "模型" in visible
        assert "数据源" not in visible
        # 原始 provider 文本只进 detail
        assert "invalid api key" not in visible
        assert "invalid api key" in info["detail"]["raw"]

    def test_transient_llm_failure_stays_retryable(self):
        info = present_exception(
            LLMError(message="LLM call failed: upstream timeout"), lang="en",
        )
        assert info["kind"] == "model"
        assert info["retryable"] is True

    def test_generic_exception_keeps_raw_text_out_of_user_fields(self):
        info = present_exception(RuntimeError("/secret/path"), lang="zh")
        visible = _user_visible(info)
        assert "/secret/path" not in visible
        assert info["detail"]["raw"] == "/secret/path"
        assert info["detail"]["node"] == "workflow"


class TestShape:
    def test_contract_keys(self):
        info = present_error("boom", lang="zh")
        assert set(info) >= {
            "kind", "title", "explanation", "suggestion", "retryable", "actions",
            "detail",
        }
        assert set(info["detail"]) >= {"raw", "node", "error_class"}

    def test_detail_is_json_serialisable(self, degrade_error):
        import json

        json.dumps(present_error(degrade_error, lang="zh"))

    def test_empty_text_falls_back_to_generic(self):
        info = present_error("", lang="zh")
        assert info["title"] and info["suggestion"]

    def test_node_override_names_the_failed_step(self, degrade_error):
        info = present_error(degrade_error, lang="zh", node="schema_linking")
        assert info["detail"]["node"] == "schema_linking"

    def test_classifier_is_the_single_source_of_kind(self):
        # kind 由 classify_error 的 domain 决定,不另立一套判定
        cls = classify_error("[ERR:DS_AUTH] nope")
        assert present_error("[ERR:DS_AUTH] nope", lang="zh")["detail"]["error_class"] == cls.cls.id
