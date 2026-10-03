"""Decision rules live in the KB tree — storage, lint gate, and the
regression that they must **not** become retrievable content.

decisions.yml sits next to the other KB YAMLs so it inherits git tracking and
the `_meta` provenance block, but it is not KB content: a rule has no lexical
relationship to a user's question, so indexing it would only add noise to
hybrid retrieval. The `_entries_of` dispatch is silent-by-default (unknown
filename → zero entries), which is exactly why that needs a test rather than
a comment.
"""

from pathlib import Path

import pytest
import yaml

from trove.services.decision import DecisionDoc, RuleError, parse_rule
from trove.services.kb.merge import FILE_SPECS
from trove.services.kb.service import KbService, _entries_of


def kb_path(name):
    return Path("/tmp/kb/demo") / name

RULE = {
    "id": "loan-drop",
    "name": "贷款余额环比下滑",
    "window": "本月",
    "subject": {"metrics": ["loan_balance"], "dimensions": ["region"]},
    "baseline": {"kind": "prev_period"},
    "scope": "per_dimension",
    "conditions": {"all": ["delta_pct < -0.10", "abs(delta) > 100000"]},
}


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


def seed(kb, ds="demo", rules=(RULE,)):
    d = kb.kb_dir / ds
    d.mkdir(parents=True, exist_ok=True)
    (d / "decisions.yml").write_text(
        yaml.safe_dump({"version": 1, "rules": list(rules)}, allow_unicode=True),
        encoding="utf-8")


class TestLoad:
    def test_missing_file_is_an_empty_doc_not_an_error(self, kb):
        """`_load_asset` raises FileNotFoundError on a missing file, so the
        exists() guard is load-bearing. An empty doc is right here: "rule not
        found" is the runner's single fail-loud point."""
        doc = kb.load_decisions("demo")
        assert doc.rules == []
        assert doc.digest == ""

    def test_round_trip(self, kb):
        seed(kb)
        doc = kb.load_decisions("demo")
        assert [r.id for r in doc.rules] == ["loan-drop"]
        assert doc.rules[0].subject.metrics == ["loan_balance"]
        assert doc.rules[0].condition_mode == "all"

    def test_digest_identifies_the_rule_version(self, kb):
        """A decision run's evidence records this, so a trigger can be traced
        back to the exact revision of the rule that judged it."""
        seed(kb)
        first = kb.load_decisions("demo").digest
        assert len(first) == 64

        seed(kb, rules=[{**RULE, "severity": "critical"}])
        assert kb.load_decisions("demo").digest != first

    def test_unparseable_file_is_loud_not_silently_empty(self, kb):
        """A corrupt file must not read as "no rules" — that would silently
        disable every job pointing at it."""
        d = kb.kb_dir / "demo"
        d.mkdir(parents=True)
        (d / "decisions.yml").write_text("rules: [unclosed", encoding="utf-8")
        with pytest.raises(RuleError):
            kb.load_decisions("demo")

    def test_structurally_bad_rule_is_loud(self, kb):
        d = kb.kb_dir / "demo"
        d.mkdir(parents=True)
        (d / "decisions.yml").write_text(
            yaml.safe_dump({"rules": [{"name": "no id"}]}), encoding="utf-8")
        with pytest.raises(RuleError):
            kb.load_decisions("demo")

    def test_get_decision_by_id(self, kb):
        seed(kb)
        assert kb.get_decision("demo", "loan-drop").id == "loan-drop"
        assert kb.get_decision("demo", "nope") is None


class TestNotRetrievable:
    def test_decisions_never_enter_the_retrieval_mirror(self):
        """The product-level property: rules are not searchable content."""
        payload = {"version": 1, "rules": [RULE]}
        entries = _entries_of(kb_path("decisions.yml"), "", payload)
        assert entries == []

    def test_other_files_still_do(self):
        """Guards the guard: `_entries_of` returning [] for everything would
        make the test above vacuous."""
        payload = {"rules": [{"rule": "never select *"}]}
        assert _entries_of(kb_path("rules.yml"), "", payload) != []

    def test_registered_as_a_human_only_file(self):
        """Explicitly None, like rules.yml — never treated as generated
        output by the three-way merge."""
        assert "decisions.yml" in FILE_SPECS
        assert FILE_SPECS["decisions.yml"] is None


class TestSave:
    async def test_save_then_load(self, kb):
        doc = DecisionDoc(rules=[parse_rule(RULE)])
        await kb.save_decisions("demo", doc)
        back = kb.load_decisions("demo")
        assert [r.id for r in back.rules] == ["loan-drop"]
        assert back.rules[0].id == "loan-drop"

    async def test_written_file_carries_provenance_meta(self, kb):
        """Written through _write_doc, so the digest/staleness machinery
        sees it — otherwise the next sync would read it as "edited by hand"."""
        from trove.services.decision.rules import SCHEMA_VERSION

        await kb.save_decisions("demo", DecisionDoc(rules=[parse_rule(RULE)]))
        data = yaml.safe_load(kb.decisions_path("demo").read_text(encoding="utf-8"))
        assert "_meta" in data
        # 写入盖的是**当前** schema 版本(不是字面量 1):版本跟着生产者的
        # 认知走,一版一改,断言跟常量才不会在下一次升级时假红。
        assert data["version"] == SCHEMA_VERSION

    async def test_bad_rule_is_refused_before_it_reaches_disk(self, kb):
        """Same posture as `_reject_bad_semantics`: a rule that would never
        fire is worth refusing, not persisting."""
        bad = parse_rule({**RULE, "scope": "aggregate", "emit": "top_k"})
        with pytest.raises(RuleError):
            await kb.save_decisions("demo", DecisionDoc(rules=[bad]))
        assert not kb.decisions_path("demo").exists()

    async def test_condition_typo_is_refused(self, kb):
        bad = parse_rule({**RULE, "conditions": ["dleta_pct < -0.1"]})
        with pytest.raises(RuleError, match="dleta_pct"):
            await kb.save_decisions("demo", DecisionDoc(rules=[bad]))

    async def test_save_does_not_touch_a_neighbouring_file(self, kb):
        """git_commit is called with files=["decisions.yml"]; with the default
        glob it would stage every *.yml and sweep an uncommitted semantics.yml
        edit into this commit."""
        d = kb.kb_dir / "demo"
        d.mkdir(parents=True)
        (d / "semantics.yml").write_text("semantic_model: []\n", encoding="utf-8")
        await kb.save_decisions("demo", DecisionDoc(rules=[parse_rule(RULE)]))
        assert (d / "semantics.yml").read_text(encoding="utf-8") == "semantic_model: []\n"


class TestLintGate:
    def test_lint_closure_flags_a_bad_staged_file(self, kb):
        path = kb.decisions_path("demo")
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump({"rules": [RULE, RULE]}), encoding="utf-8")
        issues = kb.decisions_lint("demo")([path])
        assert any("duplicate rule id" in i for i in issues)

    def test_lint_closure_passes_a_good_file(self, kb):
        seed(kb)
        assert kb.decisions_lint("demo")([kb.decisions_path("demo")]) == []

    def test_lint_closure_ignores_other_files(self, kb):
        other = kb.kb_dir / "demo" / "semantics.yml"
        other.parent.mkdir(parents=True)
        other.write_text("not: yaml: at: all\n", encoding="utf-8")
        assert kb.decisions_lint("demo")([other]) == []
