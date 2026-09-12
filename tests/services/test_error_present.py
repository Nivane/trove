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

from trove.services.errors import classify_error
from trove.services.errors.present import present_error  # noqa: F401

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


class TestShape:
    def test_contract_keys(self):
        info = present_error("boom", lang="zh")
        assert set(info) >= {
            "kind", "title", "explanation", "suggestion", "retryable", "detail",
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
