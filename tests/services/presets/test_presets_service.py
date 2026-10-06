"""PresetService —— 双源遮蔽、套用只落草稿、确认门(红线回归)。

这个文件里最重要的三条是**回归门**(与 P4 的验收绑定):

1. 套用之后、确认之前 —— 技能**不在**投递面(``_match_org`` /
   ``available_skills_block`` 都看不到它),规则**不在** ``decisions.yml``
   里(那是决策执行面的唯一读源)。确认之后两样才出现。
2. 契约:未知键拒绝,并且报错里列得出合法键名。
3. 幂等:同一份 preset 套两次,第二次全部 skipped —— 待审队列里不会叠出
   第二份同名草稿。
"""

from __future__ import annotations

import pytest
import yaml

from trove.services.decision.drafts import DecisionDraftStore
from trove.services.kb.service import KbService
from trove.services.presets.models import PresetError
from trove.services.presets.service import PresetService
from trove.services.skills.service import SkillService

from tests.helpers.kb import topic_model_yaml

SKILL_TEMPLATE = {
    "name": "period-comparison",
    "description": "Period-over-period comparison discipline.",
    "tier": "available",
    "lang": "en",
    "triggers": {"node": "gen_sql"},
    "body": "## Period comparison\n1. name the base\n2. align the grain\n",
}

DECISION_TEMPLATE = {
    "id": "watch-ratio",
    "name": "Ratio rising",
    "window": "last month",
    "subject": {"metrics": ["loan_balance"]},
    "baseline": {"kind": "prev_period"},
    "conditions": ["delta_pct > 0.2"],
}

BASE = {
    "name": "starter",
    "description": "接入模板",
    "skills": [SKILL_TEMPLATE],
    "decisions": [DECISION_TEMPLATE],
    "semantics": {"notes": ["口径提示:比率要分子分母分开建模"]},
    "presentation": {"chart": "line"},
}


def _write(root, name, data):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "preset.yml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return d


@pytest.fixture
def builtin_root(tmp_path):
    return _write(tmp_path / "builtin", "starter", BASE).parent


@pytest.fixture
def kb(tmp_path):
    service = KbService(tmp_path / "proj")
    (service.kb_dir / "demo").mkdir(parents=True, exist_ok=True)
    return service


@pytest.fixture
def skills(tmp_path):
    return SkillService(tmp_path / "proj" / ".trove" / "skills")


@pytest.fixture
def svc(tmp_path, builtin_root, kb, skills):
    return PresetService(
        tmp_path / "proj" / ".trove" / "presets",
        builtin_root=builtin_root, kb=kb, skills=skills, git_enabled=False)


def _item(report, section, item):
    return next(i for i in report.items
                if i.section == section and i.item == item)


class TestDualSource:
    def test_builtin_listed(self, svc):
        rows = {r["name"]: r for r in svc.merged()}
        assert rows["starter"]["source"] == "builtin"
        assert rows["starter"]["shadowed"] is False
        assert rows["starter"]["counts"]["skills"] == 1

    def test_org_shadows_builtin(self, svc, tmp_path):
        _write(tmp_path / "proj" / ".trove" / "presets", "starter",
               {**BASE, "version": 4, "description": "组织本地版"})
        rows = svc.merged()
        assert len(rows) == 2                       # 两份都在列表里
        shadowed = {r["source"]: r for r in rows}
        assert shadowed["org"]["shadowed"] is False
        assert shadowed["builtin"]["shadowed"] is True
        # 装载取组织版(遮蔽是确定性的)
        assert svc.load("starter").description == "组织本地版"
        assert svc.load("starter").source == "org"
        assert "组织本地版" in svc.read_raw("starter")

    def test_unknown_preset_lists_known_names(self, svc):
        with pytest.raises(KeyError) as e:
            svc.load("nope")
        assert "starter" in str(e.value)

    def test_bad_file_is_skipped_by_reader(self, svc, tmp_path):
        d = tmp_path / "proj" / ".trove" / "presets" / "broken"
        d.mkdir(parents=True)
        (d / "preset.yml").write_text("name: broken\n", encoding="utf-8")
        # 缺 description:读取面跳过(validate 会点名),不炸列表
        assert "broken" not in {r["name"] for r in svc.merged()}


class TestApplySkills:
    async def test_template_lands_pending_and_is_not_delivered(self, svc):
        """回归门 ①:套用后、确认前,技能不在任何投递面。"""
        report = await svc.apply("starter", "demo")
        assert _item(report, "skills", "period-comparison").status == "drafted"

        entry = svc.skills.read_skill("period-comparison")
        assert entry is not None and entry["status"] == "pending"
        # 未确认 → 匹配/广告都看不到它
        assert svc.skills._match_org("gen_sql") == []
        assert "period-comparison" not in svc.skills.available_skills_block(
            "gen_sql", lang="en")
        assert svc.skills.load_skill_content("period-comparison", "en")

        # 确认后才进入投递面 —— 门在这一步,而不是在套用那一步
        svc.skills.confirm("period-comparison")
        assert [s["name"] for s in svc.skills._match_org("gen_sql")] == [
            "period-comparison"]
        assert "period-comparison" in svc.skills.available_skills_block(
            "gen_sql", lang="en")

    async def test_reference_to_code_skill_is_skipped(self, svc, kb, skills):
        code = skills.list_code_skills()[0]["name"]
        svc.save("refs", {"name": "refs", "description": "引用既有技能",
                          "skills": [code]})
        report = await svc.apply("refs", "demo")
        assert _item(report, "skills", code).status == "skipped"

    async def test_missing_reference_is_unresolved(self, svc):
        svc.save("refs", {"name": "refs", "description": "引用不存在的技能",
                          "skills": ["no-such-skill"]})
        report = await svc.apply("refs", "demo")
        assert _item(report, "skills", "no-such-skill").status == "unresolved"

    async def test_pending_org_skill_reference_is_unresolved(self, svc, skills):
        skills.create({"name": "half-done", "description": "还没确认",
                       "body": "b"})
        svc.save("refs", {"name": "refs", "description": "引用未确认技能",
                          "skills": ["half-done"]})
        report = await svc.apply("refs", "demo")
        item = _item(report, "skills", "half-done")
        assert item.status == "unresolved" and "确认" in item.reason

    async def test_disabled_org_skill_reference_names_the_kill_switch(
            self, svc, skills):
        """E6:停用与未确认同属「未生效」,但下一句话不同 —— 指错方向比不报更坏。"""
        skills.create({"name": "parked", "description": "被停了", "body": "b"})
        skills.confirm("parked")
        skills.disable("parked")
        svc.save("refs", {"name": "refs", "description": "引用停用技能",
                          "skills": ["parked"]})
        report = await svc.apply("refs", "demo")
        item = _item(report, "skills", "parked")
        assert item.status == "unresolved"
        assert "停用" in item.reason and "enable" in item.reason
        assert "尚未确认" not in item.reason

    async def test_template_colliding_with_code_skill_skips(self, svc, skills):
        code = skills.list_code_skills()[0]["name"]
        svc.save("shadow", {"name": "shadow", "description": "同名 code skill",
                            "skills": [{**SKILL_TEMPLATE, "name": code}]})
        report = await svc.apply("shadow", "demo")
        assert _item(report, "skills", code).status == "skipped"
        assert skills.read_skill(code) is None     # 没有落任何草稿

    async def test_invalid_template_is_unresolved(self, svc):
        svc.save("bad", {"name": "bad", "description": "坏骨架",
                         "skills": [{**SKILL_TEMPLATE, "name": "Bad Name"}]})
        report = await svc.apply("bad", "demo")
        assert _item(report, "skills", "Bad Name").status == "unresolved"


class TestApplyDecisions:
    async def test_template_lands_in_drafts_not_decisions(self, svc, kb):
        """回归门 ①:套用后、确认前,规则不在 decisions.yml(唯一执行读源)。"""
        report = await svc.apply("starter", "demo")
        item = _item(report, "decisions", "watch-ratio")
        assert item.status == "drafted"
        assert "decision_drafts.yml" in item.reason
        assert "enabled: false" in item.reason       # 模板默认停用

        assert kb.load_decisions("demo").rules == []
        assert not kb.decisions_path("demo").exists()
        drafts = DecisionDraftStore(kb)
        assert drafts.path("demo").exists()
        pending = drafts.grouped("demo")["pending"]
        assert [d["rule"]["id"] for d in pending] == ["watch-ratio"]
        assert pending[0]["source"] == "preset:starter"

        # 确认走既有写门(save_decisions:lint + git) → 这时才生效
        await drafts.confirm("demo", pending[0]["id"], actor="admin")
        rules = kb.load_decisions("demo").rules
        assert [r.id for r in rules] == ["watch-ratio"]
        assert rules[0].enabled is False            # 安全默认被保真携带
        assert drafts.grouped("demo")["applied"][0]["status"] == "applied"

    async def test_bad_template_is_unresolved_and_writes_nothing(self, svc, kb):
        bad = {**DECISION_TEMPLATE, "id": "watch-bad",
               "conditions": ["current > baseline * 1.2"]}
        svc.save("bad", {"name": "bad", "description": "条件语言里没有算术",
                         "decisions": [bad]})
        report = await svc.apply("bad", "demo")
        item = _item(report, "decisions", "watch-bad")
        assert item.status == "unresolved" and "lint" in item.reason
        # 结构不过 → 不落草稿(落下去就是一份永远确认不了的东西)
        assert not DecisionDraftStore(kb).path("demo").exists()

    async def test_reference_resolution(self, svc, kb):
        svc.save("refs", {"name": "refs", "description": "引用规则",
                          "decisions": ["watch-ratio"]})
        report = await svc.apply("refs", "demo")
        assert _item(report, "decisions", "watch-ratio").status == "unresolved"

        await svc.apply("starter", "demo")          # 落一份草稿
        drafts = DecisionDraftStore(kb)
        draft = drafts.grouped("demo")["pending"][0]
        await drafts.confirm("demo", draft["id"], actor="admin")
        report = await svc.apply("refs", "demo")   # 规则已在 decisions.yml
        assert _item(report, "decisions", "watch-ratio").status == "skipped"

    async def test_missing_metric_is_noted(self, svc, kb):
        kb.semantics_path("demo").write_text(
            topic_model_yaml(["loan"], {}), encoding="utf-8")
        report = await svc.apply("starter", "demo")
        item = _item(report, "decisions", "watch-ratio")
        assert item.status == "drafted"
        assert "loan_balance" in item.reason and "未在目标语义模型声明" in item.reason

    async def test_apply_twice_is_idempotent(self, svc, kb):
        await svc.apply("starter", "demo")
        again = await svc.apply("starter", "demo")
        assert _item(again, "skills", "period-comparison").status == "skipped"
        assert _item(again, "decisions", "watch-ratio").status == "skipped"
        drafts = DecisionDraftStore(kb)
        assert len(drafts.grouped("demo")["pending"]) == 1


class TestApplyDomains:
    async def test_empty_datasets_is_unresolved_and_writes_nothing(
            self, svc, kb):
        kb.semantics_path("demo").write_text(
            topic_model_yaml(["loan"], {}), encoding="utf-8")
        before = kb.semantics_path("demo").read_text(encoding="utf-8")
        svc.save("dom", {"name": "dom", "description": "域骨架",
                         "domains": [{"name": "credit-risk",
                                      "description": "信贷口径"}]})
        report = await svc.apply("dom", "demo")
        item = _item(report, "domains", "credit-risk")
        assert item.status == "unresolved" and "datasets" in item.reason
        assert not (kb.kb_dir / "demo" / "semantic_drafts.yml").exists()
        assert kb.semantics_path("demo").read_text(encoding="utf-8") == before

    async def test_resolvable_datasets_land_a_topic_draft(self, svc, kb):
        kb.semantics_path("demo").write_text(
            topic_model_yaml(["loan"], {}), encoding="utf-8")
        before = kb.semantics_path("demo").read_text(encoding="utf-8")
        svc.save("dom", {"name": "dom", "description": "域骨架",
                         "domains": [{"name": "credit-risk",
                                      "datasets": ["loan"],
                                      "description": "信贷口径"}]})
        report = await svc.apply("dom", "demo")
        assert _item(report, "domains", "credit-risk").status == "drafted"
        data = yaml.safe_load(
            (kb.kb_dir / "demo" / "semantic_drafts.yml").read_text(encoding="utf-8"))
        draft = data["drafts"][0]
        assert (draft["kind"], draft["name"], draft["status"]) == (
            "topic", "credit-risk", "pending")
        # 语义层本体一个字节不动(确认才写)
        assert kb.semantics_path("demo").read_text(encoding="utf-8") == before
        # 幂等:再套一次不叠第二份
        again = await svc.apply("dom", "demo")
        assert _item(again, "domains", "credit-risk").status == "skipped"

    async def test_undeclared_dataset_names_the_missing_ones(self, svc, kb):
        kb.semantics_path("demo").write_text(
            topic_model_yaml(["loan"], {}), encoding="utf-8")
        svc.save("dom", {"name": "dom", "description": "域骨架",
                         "domains": [{"name": "credit-risk",
                                      "datasets": ["repayments"]}]})
        report = await svc.apply("dom", "demo")
        item = _item(report, "domains", "credit-risk")
        assert item.status == "unresolved" and "repayments" in item.reason

    async def test_missing_topic_reference_is_unresolved(self, svc, kb):
        kb.semantics_path("demo").write_text(
            topic_model_yaml(["loan"], {}), encoding="utf-8")
        svc.save("dom", {"name": "dom", "description": "域引用",
                         "domains": ["credit-risk"]})
        report = await svc.apply("dom", "demo")
        assert _item(report, "domains", "credit-risk").status == "unresolved"


class TestApplyHints:
    async def test_hints_are_reported_but_write_nothing(self, svc, kb):
        report = await svc.apply("starter", "demo")
        sem = [i for i in report.items if i.section == "semantics"]
        pres = [i for i in report.items if i.section == "presentation"]
        assert sem and pres
        assert {i.status for i in sem} == {"skipped"}
        assert {i.status for i in pres} == {"skipped"}
        assert all("仅提示" in i.reason for i in sem + pres)
        # 提示段不产任何文件:语义层/展示配置都不在 preset 的写面上
        assert not kb.semantics_path("demo").exists()

    async def test_report_covers_every_section(self, svc):
        report = await svc.apply("starter", "demo")
        assert {i.section for i in report.items} == {
            "skills", "decisions", "semantics", "presentation"}
        assert report.to_dict()["datasource"] == "demo"


class TestSave:
    def test_save_is_contract_first(self, svc, tmp_path):
        with pytest.raises(PresetError):
            svc.save("oops", {"name": "oops", "description": "d", "skillz": []})
        assert not (tmp_path / "proj" / ".trove" / "presets" / "oops").exists()

    def test_save_bumps_version(self, svc):
        first = svc.save("mine", {"name": "mine", "description": "v1"})
        assert first["version"] == 1
        second = svc.save("mine", {"name": "mine", "description": "v2"})
        assert second["version"] == 2
        assert svc.load("mine").description == "v2"

    def test_named_must_match(self, svc):
        with pytest.raises(PresetError):
            svc.save("mine", {"name": "other", "description": "d"})

    def test_history_without_git_is_empty(self, svc):
        svc.save("mine", {"name": "mine", "description": "d"})
        assert svc.history("mine") == []

    def test_rollback_needs_an_org_copy(self, svc):
        with pytest.raises(KeyError):
            svc.rollback("starter", "deadbeef")
