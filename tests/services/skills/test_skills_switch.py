"""组织扩展总开关(``agent.extensions.org_extensions_enabled``)的消费面。

开关语义(逐字):**单个总开关只停「组织扩展」这一层** —— org skills 的注入
与 ``load_skill`` 广告、validator 断言、决策规则执行;code skills / KB /
few-shots 一律不动。管理面(起草/确认/列表/版本化)照常可用:停的是消费,
不是管理 —— 否则恢复扩展前连内容都修不了。

两条不变量在这里钉死:

- **每问现读**:管理端 PUT 改的是共享 ``AgentConfig``(``apply_overrides``
  就地改),同一个 ``SkillService`` 实例上翻开关即时生效,无需重建 ——
  构造期快照会让开关在重启前失效,而"改了没生效"正是这层最要防的静默结局;
- **文件即真相**:改盘上的 ``SKILL.md``,下一次 render / load 直接看见新内容,
  没有缓存层(读路径零缓存是这套治理的生效半边)。
"""

from __future__ import annotations

from pathlib import Path

from trove.core.config import AgentConfig
from trove.services.skills.service import SkillService


def _svc(tmp_path: Path, *, enabled: bool = True):
    cfg = AgentConfig(target="mock/model")
    cfg.extensions.org_extensions_enabled = enabled
    return SkillService(tmp_path / ".trove" / "skills", config=cfg), cfg


_REQUIRED = {
    "name": "org-rule", "description": "org methodology",
    "tier": "required", "triggers": {"node": "query_sketch"},
    "body": "ORG-BODY-MARKER: check totals twice.",
}
_AVAILABLE = {
    "name": "org-tip", "description": "ADV-MARKER description",
    "tier": "available", "triggers": {"node": "gen_sql"},
    "body": "TIP-BODY-MARKER",
}
_VALIDATOR = {
    "name": "org-check", "description": "result assertion",
    "tier": "validator", "severity": "advisory", "targets": ["result"],
    "checks": [{"expr": "row_count >= 0"}], "body": "human-readable note",
}


class TestInjection:
    def test_disabled_stops_org_injection_keeps_code_skills(self, tmp_path):
        svc, cfg = _svc(tmp_path, enabled=False)
        svc.create(dict(_REQUIRED))
        svc.confirm("org-rule")

        rendered = svc.render_skills("query_sketch", lang="en")
        assert "Traceability self-check" in rendered      # code skill 照常
        assert "ORG-BODY-MARKER" not in rendered
        assert "<org_skill" not in rendered

        cfg.extensions.org_extensions_enabled = True      # 现场翻回,不重建
        rendered = svc.render_skills("query_sketch", lang="en")
        assert "ORG-BODY-MARKER" in rendered
        assert '<org_skill name="org-rule"' in rendered

    def test_disabled_hides_advertisement_and_load_skill(self, tmp_path):
        svc, cfg = _svc(tmp_path)
        svc.create(dict(_AVAILABLE))
        svc.confirm("org-tip")
        assert svc.has_available_for("gen_sql") is True
        assert "org-tip" in svc.available_skills_block("gen_sql", lang="en")
        assert "TIP-BODY-MARKER" in svc.load_skill_content(
            "org-tip", "en", node="gen_sql", skill_ctx={})

        cfg.extensions.org_extensions_enabled = False     # 同一个实例,现读
        assert svc.available_descriptions("gen_sql", lang="en") == []
        assert svc.has_available_for("gen_sql") is False   # load_skill 不注册
        assert svc.available_skills_block("gen_sql", lang="en") == ""
        msg = svc.load_skill_content("org-tip", "en", node="gen_sql", skill_ctx={})
        assert "disabled by the administrator" in msg      # 按名直取也停
        assert "TIP-BODY-MARKER" not in msg

    def test_disabled_stops_validator_assertions(self, tmp_path):
        svc, cfg = _svc(tmp_path)
        svc.create(dict(_VALIDATOR))
        svc.confirm("org-check")
        assert [e["name"] for e in svc.validators_for("validate")] == ["org-check"]

        cfg.extensions.org_extensions_enabled = False
        assert svc.validators_for("validate") == []


class TestManagementStaysUsable:
    def test_draft_confirm_list_work_while_disabled(self, tmp_path):
        """停用期仍可起草/确认/列表 —— 停的是消费,不是管理。"""
        svc, _ = _svc(tmp_path, enabled=False)
        svc.create(dict(_REQUIRED))
        svc.confirm("org-rule")

        assert [e["name"] for e in svc.list_org()] == ["org-rule"]
        assert svc.read_skill("org-rule")["status"] == "confirmed"


class TestFreshRead:
    def test_skill_file_edit_is_visible_on_the_next_read(self, tmp_path):
        """改盘上的 SKILL.md → 下一次 render 直接看见 —— 无缓存层。"""
        svc, _ = _svc(tmp_path)
        svc.create(dict(_REQUIRED))
        svc.confirm("org-rule")
        assert "ORG-BODY-MARKER" in svc.render_skills("query_sketch", lang="en")

        path = svc.skill_path("org-rule")
        path.write_text(
            path.read_text(encoding="utf-8").replace("ORG-BODY-MARKER", "EDITED-MARKER"),
            encoding="utf-8",
        )
        rendered = svc.render_skills("query_sketch", lang="en")
        assert "EDITED-MARKER" in rendered
        assert "ORG-BODY-MARKER" not in rendered

    def test_switch_defaults_on_without_config(self, tmp_path):
        """不传 config(旧构造点)→ 视为全开,不是视为停用。"""
        svc = SkillService(tmp_path / ".trove" / "skills")
        svc.create(dict(_REQUIRED))
        svc.confirm("org-rule")
        assert "ORG-BODY-MARKER" in svc.render_skills("query_sketch", lang="en")
