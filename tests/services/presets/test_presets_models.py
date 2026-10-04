"""preset 契约(models.py)—— 闭键集、必填、语法糖、结果形状。

契约的判据是**拒绝**:未知键报错并列出合法键名(与 skills.create 同款),
名字/目录名不一致拒绝,``description`` 缺失拒绝。这些错误全部是
``PresetError`` —— 读路径(``PresetService._read_dir``)唯一接得住的异常。
"""

from __future__ import annotations

import pytest

from trove.services.presets.models import (
    ApplyReport,
    Preset,
    PresetError,
    brief,
    parse_preset,
    preset_to_dict,
    reject_unknown,
)

MINIMAL = {"name": "starter", "description": "接入模板"}


class TestContract:
    def test_minimal_parses(self):
        p = parse_preset(dict(MINIMAL), name_hint="starter")
        assert (p.name, p.version, p.source) == ("starter", 1, "builtin")
        assert p.counts == {"skills": 0, "decisions": 0, "domains": 0,
                            "semantics": 0, "presentation": 0}

    def test_unknown_top_level_key_lists_legal_keys(self):
        with pytest.raises(PresetError) as e:
            parse_preset({**MINIMAL, "skils": []})
        msg = str(e.value)
        assert "skils" in msg and "skills" in msg and "合法键" in msg

    def test_name_must_match_directory(self):
        with pytest.raises(PresetError) as e:
            parse_preset(dict(MINIMAL), name_hint="other")
        assert "不一致" in str(e.value)

    def test_name_and_description_required(self):
        with pytest.raises(PresetError):
            parse_preset({"description": "x"})
        with pytest.raises(PresetError):
            parse_preset({"name": "starter"})

    @pytest.mark.parametrize("bad", ["1", 0, True, None])
    def test_version_must_be_positive_int(self, bad):
        with pytest.raises(PresetError):
            parse_preset({**MINIMAL, "version": bad})

    def test_section_unknown_key_rejected(self):
        with pytest.raises(PresetError) as e:
            parse_preset({**MINIMAL, "skills": [{"name": "s", "desc": "oops"}]})
        assert "desc" in str(e.value) and "description" in str(e.value)

    def test_hint_sections_are_closed_too(self):
        with pytest.raises(PresetError):
            parse_preset({**MINIMAL, "semantics": {"note": "typo"}})
        with pytest.raises(PresetError):
            parse_preset({**MINIMAL, "presentation": {"charts": "line"}})

    def test_reject_unknown_on_non_mapping(self):
        with pytest.raises(PresetError):
            reject_unknown(["not", "a", "map"], ("a",), "preset")


class TestSectionSugar:
    def test_string_and_name_only_are_references(self):
        p = parse_preset({**MINIMAL, "skills": [
            "plan_query", {"name": "other"},
        ], "decisions": ["watch-x", {"id": "watch-y"}],
            "domains": ["risk", {"name": "credit"}]})
        assert p.skills == ["plan_query", "other"]
        assert p.decisions == ["watch-x", "watch-y"]
        assert p.domains == ["risk", "credit"]

    def test_full_mapping_is_a_template(self):
        p = parse_preset({**MINIMAL, "skills": [{
            "name": "period", "description": "d", "body": "b",
            "tier": "available",
        }], "decisions": [{"id": "r1", "window": "last month"}],
            "domains": [{"name": "risk", "datasets": ["loan"]}]})
        assert p.skills[0]["name"] == "period"
        assert p.decisions[0]["id"] == "r1"
        assert p.domains[0]["datasets"] == ["loan"]

    def test_empty_reference_rejected(self):
        with pytest.raises(PresetError):
            parse_preset({**MINIMAL, "skills": ["  "]})
        with pytest.raises(PresetError):
            parse_preset({**MINIMAL, "decisions": [{"id": ""}]})

    def test_sections_must_be_lists(self):
        with pytest.raises(PresetError):
            parse_preset({**MINIMAL, "skills": {"name": "x"}})


class TestRoundTrip:
    def test_preset_to_dict_is_reloadable(self):
        data = {**MINIMAL, "author": "trove", "version": 2,
                "skills": ["plan_query", {"name": "s", "description": "d",
                                          "body": "b"}],
                "semantics": {"notes": ["n"]}}
        first = parse_preset(data, name_hint="starter")
        again = parse_preset(preset_to_dict(first), name_hint="starter")
        assert preset_to_dict(again) == preset_to_dict(first)
        assert again.version == 2 and again.author == "trove"

    def test_brief_marks_shadowing(self):
        p = Preset(name="starter", source="builtin", version=3)
        assert brief(p)["shadowed"] is False
        assert brief(p, shadowed=True)["shadowed"] is True


class TestApplyReport:
    def test_counts_and_render(self):
        r = ApplyReport(preset="starter", datasource="demo", source="builtin")
        r.add("skills", "s1", "drafted", "落了草稿")
        r.add("decisions", "r1", "skipped", "已存在")
        r.add("domains", "d1", "unresolved", "数据集解析不到")
        assert r.counts == {"drafted": 1, "skipped": 1, "unresolved": 1}
        text = r.render()
        assert "starter" in text and "demo" in text
        assert "1 条落 pending 草稿" in text and "1 条未解析" in text
        d = r.to_dict()
        assert d["counts"]["drafted"] == 1
        assert [i["section"] for i in d["items"]] == ["skills", "decisions", "domains"]

    def test_invalid_status_rejected(self):
        r = ApplyReport()
        with pytest.raises(ValueError):
            r.add("skills", "s", "pending")
