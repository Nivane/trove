"""Line budgets for the orchestration modules the strangler split carved out.

God modules grow back. Every extraction under the #4 orchestration split
(``graphs`` → ``gen_chain``/``graph_services``, ``session`` → ``audit``,
``state`` → ``gen_state``) must come with a ratchet: the exact line count of
each module is frozen here, and **both** directions are red.

- Over budget: the God module is regrowing — put the code on its own change
  axis instead of appending to the orchestrator.
- Under budget: the ratchet only shrinks. Deleting/moving code is fine, but the
  budget must be lowered in the same commit, otherwise the next growth spurt
  silently fits inside the old allowance.

This mirrors the repo's ``assert_frozen`` discipline: a frozen number that no
longer matches reality is stale, and a stale ratchet is no ratchet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

MODULE_LINE_BUDGET: dict[str, int] = {
    # Orchestration modules under the #4 split. Each extraction lowers the
    # original module's budget in the same commit and freezes the new module.
    "trove/workflow/graphs.py": 1119,
    "trove/workflow/gen_chain.py": 1366,
    "trove/workflow/graph_services.py": 67,
    "trove/agent/session.py": 2960,
    "trove/agent/audit.py": 304,
    "trove/workflow/state.py": 580,
    "trove/workflow/gen_state.py": 165,
}


def _line_count(rel: str) -> int:
    return len((ROOT / rel).read_text(encoding="utf-8").splitlines())


def test_module_line_budgets_are_exact():
    actual = {rel: _line_count(rel) for rel in MODULE_LINE_BUDGET}
    over = {rel: n for rel, n in actual.items() if n > MODULE_LINE_BUDGET[rel]}
    under = {rel: n for rel, n in actual.items() if n < MODULE_LINE_BUDGET[rel]}
    assert not over, (
        "module line budget exceeded (God module regrowth — extract to the "
        f"matching change axis): {over}"
    )
    assert not under, (
        "module shrank below its frozen budget — lower the budget in this "
        f"commit (ratchet only shrinks): {under}"
    )


@pytest.mark.parametrize("rel", sorted(MODULE_LINE_BUDGET))
def test_budgeted_modules_still_exist(rel: str):
    assert (ROOT / rel).is_file(), f"budgeted module disappeared: {rel}"
