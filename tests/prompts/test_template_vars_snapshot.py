"""模板变量全量快照:新增/改名会在 diff 里冒出来,逼一次显式分类(设计稿 §4.3)。

白名单是**人工维护**的:放行一个变量 = 声明"这个位置不会出现外部内容"。
这类断言唯一会失效的方式是"没人注意就漂了" —— 所以冻结全量快照:改模板的
diff 会同时带出本文件的 diff,分类必须当次做完(与 ``MASKING_MODES`` /
``AUTHZ_DENY_REASONS`` 的"观测契约要有意识地改一次"同一条纪律)。

**按文件冻结**(不是按模板名):同一模板的 ``.en.j2`` 与 ``.zh.j2`` 可以各自
引用不同的变量,合成一个 key 会让其中一份的变量无人复核。

快照更新方式:改了模板就照 ``_live()`` 的输出改 ``_SNAPSHOT``,然后**回答一次**
新变量属于哪一类(用户原话 / 已确认的知识 / 系统标量 / 数据)。
"""

from __future__ import annotations

import jinja2
import jinja2.meta

from trove.llm.untrusted import _TRUSTED_VARS

_SNAPSHOT: dict[str, list[str]] = {
    "analyze_error/system.en.j2": [],
    "analyze_error/system.zh.j2": [],
    "analyze_error/user.en.j2": ["error", "evidence", "question", "schema_context", "sql", "trail"],
    "answer/system.en.j2": [],
    "answer/system.zh.j2": [],
    "answer/user.en.j2": ["context", "error_feedback", "question"],
    "answer/user.zh.j2": ["context", "error_feedback", "question"],
    "attribution/ratio_user.en.j2": ["baseline", "dimension", "effects", "metric", "question", "table", "total_delta"],
    "attribution/ratio_user.zh.j2": ["baseline", "dimension", "effects", "metric", "question", "table", "total_delta"],
    "attribution/system.en.j2": [],
    "attribution/system.zh.j2": [],
    "attribution/user.en.j2": ["baseline", "dimension", "metric", "question", "table", "total_delta"],
    "attribution/user.zh.j2": ["baseline", "dimension", "metric", "question", "table", "total_delta"],
    "chart/system.en.j2": [],
    "chart/system.zh.j2": [],
    "chart/user.en.j2": ["columns", "question", "rows", "rows_note", "sql", "time_context", "total_rows"],
    "chart/user.zh.j2": ["columns", "question", "rows", "rows_note", "sql", "time_context", "total_rows"],
    "conclusion/system.en.j2": [],
    "conclusion/system.zh.j2": [],
    "conclusion/user.en.j2": ["columns", "question", "rows", "rows_note", "sql", "time_context", "total_rows"],
    "conclusion/user.zh.j2": ["columns", "question", "rows", "rows_note", "sql", "time_context", "total_rows"],
    "gen_sql/fix.en.j2": ["errors", "sql", "target"],
    "gen_sql/fix.zh.j2": ["errors", "sql", "target"],
    "gen_sql/system.en.j2": ["full_rules", "has_probe"],
    "gen_sql/system.zh.j2": ["full_rules", "has_catalog", "has_probe"],
    "gen_sql/user.en.j2": ["dialect", "entities", "episodes", "error_analysis", "error_feedback", "evidence", "few_shots", "fix_mode", "history", "lessons", "metrics", "plan", "previous_sql", "profile", "question", "reasoning_context", "reflect_reason", "rejected_hypotheses", "rules", "schema_context", "sql_versions", "term_notes", "time_context", "user_facts"],
    "insights/system.en.j2": [],
    "insights/system.zh.j2": [],
    "insights/user.en.j2": ["columns", "question", "rows", "rows_note", "sql", "time_context", "total_rows"],
    "insights/user.zh.j2": ["columns", "question", "rows", "rows_note", "sql", "time_context", "total_rows"],
    "intent/followup_rewrite.en.j2": ["history", "question"],
    "intent/followup_rewrite.zh.j2": ["history", "question"],
    "intent/system.en.j2": [],
    "intent/system.zh.j2": [],
    "kb/draft_system.en.j2": [],
    "kb/draft_user.en.j2": ["question", "sql"],
    "kb/init_repair.en.j2": ["error"],
    "kb/init_repair_missing.en.j2": ["missing"],
    "kb/init_system.en.j2": [],
    "kb/init_system.zh.j2": [],
    "kb/init_user.en.j2": ["schema_text"],
    "kb/semantic_draft_system.en.j2": [],
    "kb/semantic_draft_system.zh.j2": [],
    "kb/semantic_draft_user.en.j2": ["chunk"],
    "kb/semantic_draft_user.zh.j2": ["chunk"],
    "kb/synthetic_system.en.j2": [],
    "kb/synthetic_system.zh.j2": [],
    "kb/synthetic_user.en.j2": ["schema_text"],
    "kb/synthetic_user.zh.j2": ["schema_text"],
    "lesson_distill/system.en.j2": [],
    "lesson_distill/user.en.j2": ["error", "failure_evidence", "gold_sql", "pred_sql", "question"],
    "memory/preference_extract.en.j2": ["conversation"],
    "memory/preference_extract.zh.j2": ["conversation"],
    "metadata_check/system.en.j2": [],
    "metadata_check/system.zh.j2": [],
    "metadata_check/user.en.j2": ["answer", "question"],
    "query_sketch/attribution.en.j2": [],
    "query_sketch/attribution.zh.j2": [],
    "query_sketch/system.en.j2": [],
    "query_sketch/system.zh.j2": [],
    "query_sketch/user.en.j2": ["correction", "evidence", "history", "previous_plan", "question", "schema_context", "time_context"],
    "query_sketch/user.zh.j2": ["correction", "evidence", "history", "previous_plan", "question", "schema_context", "time_context"],
    "reflect/reask_system.en.j2": [],
    "reflect/reask_user.en.j2": ["columns", "question", "sample"],
    "reflect/system.en.j2": [],
    "reflect/system.zh.j2": [],
    "reflect/user.en.j2": ["columns", "evidence", "question", "sample", "schema_context", "sql", "time_context", "total_rows"],
    "refuse/draft.en.j2": ["plan", "question", "vocabulary"],
    "refuse/draft.zh.j2": ["plan", "question", "vocabulary"],
    "schema_alignment/system.en.j2": [],
    "schema_alignment/system.zh.j2": [],
    "schema_alignment/user.en.j2": ["alignment_context", "evidence", "question"],
    "schema_alignment/user.zh.j2": ["alignment_context", "evidence", "question"],
    "semantics/system.en.j2": [],
    "semantics/system.zh.j2": [],
    "semantics/user.en.j2": ["question", "sql", "time_context"],
    "semantics/user.zh.j2": ["question", "sql", "time_context"],
    "session/compact.en.j2": ["conversation"],
    "session/compact.zh.j2": ["conversation"],
    "skills/align_schema/system.en.j2": [],
    "skills/align_schema/system.zh.j2": [],
    "skills/diagnose_failure/system.en.j2": [],
    "skills/diagnose_failure/system.zh.j2": [],
    "skills/draft.en.j2": ["description", "node", "purpose", "skill_name"],
    "skills/draft.zh.j2": ["description", "node", "purpose", "skill_name"],
    "skills/plan_query/system.en.j2": [],
    "skills/plan_query/system.zh.j2": [],
    "tasks/decompose.en.j2": ["question"],
    "tasks/decompose.zh.j2": ["question"],
    "tasks/interpret.en.j2": ["question", "tasks"],
    "tasks/interpret.zh.j2": ["question", "tasks"],
    "tasks/synthesize.en.j2": ["tasks"],
    "tasks/synthesize.zh.j2": ["tasks"],
}

#: 白名单里唯一**不是模板变量**的名字:``render(name, lang=...)`` 用它选模板,
#: 不参与插值(所以任何模板都不会引用它)。
_TRUSTED_NOT_IN_TEMPLATES = frozenset({"lang"})


def _live() -> dict[str, list[str]]:
    """遍历全部模板文件(与 ``loader`` 同一个 loader),取每个文件用到的变量名。"""
    loader = jinja2.PackageLoader("trove", "prompts")
    env = jinja2.Environment(loader=loader)
    out: dict[str, list[str]] = {}
    for rel in sorted(loader.list_templates()):
        if not rel.endswith(".j2"):
            continue
        out[rel] = sorted(
            jinja2.meta.find_undeclared_variables(env.parse(loader.get_source(env, rel)[0]))
        )
    return out


class TestTemplateVarSnapshot:
    def test_snapshot_matches_templates(self):
        live = _live()
        changed = {k: v for k, v in live.items() if _SNAPSHOT.get(k) != v}
        removed = sorted(set(_SNAPSHOT) - set(live))
        assert not changed and not removed, (
            f"模板变量变了,先给新变量做一次分类,再更新 _SNAPSHOT。\n"
            f"变化的模板: {changed}\n已删除的模板: {removed}"
        )

    def test_no_stale_whitelist_entry(self):
        """白名单里不该留着模板上已经不存在的名字(陈旧放行 = 无人复核的信任)。"""
        used = {v for vs in _live().values() for v in vs}
        stale = set(_TRUSTED_VARS) - used - _TRUSTED_NOT_IN_TEMPLATES
        assert stale == set(), f"白名单里的陈旧条目: {sorted(stale)}"

    def test_whitelisted_vars_are_the_minority(self):
        """多数变量应当按数据扫 —— 白名单太大本身就是信号(fail-safe 方向)。"""
        used = {v for vs in _live().values() for v in vs}
        trusted = used & set(_TRUSTED_VARS)
        assert len(trusted) < len(used) / 2, f"白名单占了模板变量的一半以上: {sorted(trusted)}"
