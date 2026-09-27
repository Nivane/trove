"""Decision layer — semantic-model-driven deterministic rules.

Trove's chain runs 问数 → 分析 → 决策 → 行动. Query and analysis are thick;
decision used to be ``jobs/alerts.py`` alone: one expression, six comparators,
a right-hand side that could only be a literal. Meanwhile the attribution
node already computed 环比/同比 baselines and contribution breakdowns — the
alert layer just had no way to reach them.

This package closes that gap without adding an LLM to the loop: a rule names
its subject in the semantic model's own vocabulary, the engine resolves the
window and baseline deterministically, and the condition language decides
whether the resulting numbers are a problem. Every trigger carries the SQL
and the rows it was judged on, because for a scheduled run the stored
evidence *is* the audit trail.

Nothing here writes to a business datasource — rules are read-only queries
plus a judgement.
"""

from trove.services.decision.expr import (
    UNKNOWN,
    DecisionExprError,
    as_number,
    condition_variables,
    evaluate_condition,
    parse_condition,
)
from trove.services.decision.rules import (
    BASELINE_KINDS,
    EMITS,
    SCOPES,
    SEVERITIES,
    Baseline,
    DecisionDoc,
    DecisionRule,
    RuleError,
    Subject,
    compile_condition,
    lint_document,
    lint_rule,
    parse_document,
    parse_rule,
)

__all__ = [
    "BASELINE_KINDS",
    "Baseline",
    "DecisionDoc",
    "DecisionExprError",
    "DecisionRule",
    "EMITS",
    "RuleError",
    "SCOPES",
    "SEVERITIES",
    "Subject",
    "UNKNOWN",
    "as_number",
    "compile_condition",
    "condition_variables",
    "evaluate_condition",
    "lint_document",
    "lint_rule",
    "parse_condition",
    "parse_document",
    "parse_rule",
]
