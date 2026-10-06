"""E6 颗粒停用(status: disabled)—— 状态机 + 四个消费面 + 未来面护栏。

**停用与总开关不是一回事**:``agent.extensions.org_extensions_enabled`` 停
「组织扩展」整层;``status: disabled`` 停**一条资产**。后者是治理日常
(一条方法论写坏了、口径作废了,先停它,别停整层)。

四个消费面(与总开关同一批面,但判据在资产上):

① 注入 —— ``render_skills``(required 档进系统提示)
② 广告 —— ``available_descriptions`` / ``available_skills_block`` /
   ``has_available_for``(available 档进 ``load_skill`` 广告)+ 按名直取
   (``load_skill_content``:点名不是提权,停用后同样取不到)
③ 执行 —— ``validators_for``(结果域断言)与 ``guards_for``(SQL 域断言,
   与 validator **同一条投递路**)
④ 枚举 —— 试跑语料(``validator_specs_for`` / ``load_guard_tier``)与信封
   (``collect_assets`` 照列但 state=disabled;装配清单摘除,见 test_plan.py)

全部四面都经过同一个过滤点:``list_org(confirmed_only=True)`` 里那一句
``status != "confirmed"``(N8 单点)。所以逐面测试钉的是**同一条判据在四条
路上都成立**,而不是四份判据各自成立 —— 后者会在下一次加面时静默漏掉一面。
``TestFutureFaceGuard`` 把这件事升级成机制:源码级钩住每一个 ``list_org``
调用点,新的消费面不带过滤 = 直接红。

状态机(审慎、显式、不幂等):

- ``disable``:**只** confirmed → disabled(停一条草稿没有语义:v1.0 决策);
- ``enable``:**只** disabled → confirmed(恢复的是投递,不是重新过确认门);
- 其余转移一律显式报错(``ValueError``/``KeyError``)——「已经停用了」当成
  成功会让审计史少一条本该存在的痕迹;
- ``confirm`` 不兼作恢复入口(审计史里「谁重新打开的」必须读得出来);
- 自动路径(记忆/LLM)绝不写 disabled/enable —— 本文件里没有自动路径,
  这条由 grep 语义保证:写 disabled 的只有 ``disable``/``enable`` 两个方法。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trove.services.skills.guards import GuardRunner
from trove.services.skills.service import SkillService

# ── fixtures ─────────────────────────────────────────────

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
    "checks": [{"expr": "min >= 0", "columns": ["loan_count"],
                "message": "min is negative"}],
    "body": "human-readable note",
}
_GUARD = {
    "name": "org-guard", "description": "sql assertion",
    "tier": "guard",
    "guard": {"targets": ["sql"], "checks": [{
        "name": "no-star", "severity": "advisory",
        "expr": "select_star == 0", "reason": "不允许裸查全列",
    }]},
    "body": "guard note",
}


def _svc(tmp_path: Path) -> SkillService:
    return SkillService(root=tmp_path / ".trove" / "skills")


def _active(svc: SkillService, payload: dict) -> str:
    """create → confirm:一条生效的资产(四面的阳性基线)。"""
    svc.create(dict(payload))
    svc.confirm(payload["name"])
    return payload["name"]


# ── 状态机 ───────────────────────────────────────────────


class TestStateMachine:
    def test_disable_rewrites_status_on_disk(self, tmp_path):
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        before = svc.read_skill("org-rule")["version"]

        entry = svc.disable("org-rule", actor="alice")
        assert entry["status"] == "disabled"
        assert entry["version"] == before + 1          # 治理动作 = 新的一版
        text = svc.skill_path("org-rule").read_text(encoding="utf-8")
        assert "status: disabled" in text              # 落盘,不是内存态

    def test_enable_restores_to_confirmed_not_pending(self, tmp_path):
        """恢复的是**投递**,不是重新过确认门(enable 的判据只有 disabled)。"""
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        svc.disable("org-rule")
        entry = svc.enable("org-rule", actor="alice")
        assert entry["status"] == "confirmed"
        assert "status: confirmed" in svc.skill_path("org-rule").read_text(
            encoding="utf-8")

    def test_state_survives_a_new_service_instance(self, tmp_path):
        """文件即真相:新实例(模拟重启)看见的仍是 disabled。"""
        _svc(tmp_path).create(dict(_REQUIRED))
        svc = _svc(tmp_path)
        svc.confirm("org-rule")
        svc.disable("org-rule")
        assert _svc(tmp_path).read_skill("org-rule")["status"] == "disabled"

    def test_disable_pending_is_an_error(self, tmp_path):
        """停一条还没生效的草稿没有语义(它本来就不投递)—— 显式报错。"""
        svc = _svc(tmp_path)
        svc.create(dict(_REQUIRED))
        with pytest.raises(ValueError, match="is pending, not confirmed"):
            svc.disable("org-rule")

    def test_disable_disabled_is_not_idempotent(self, tmp_path):
        """第二次停用报错而不是静默成功 —— 审计史里不该出现两条『停用』。"""
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        svc.disable("org-rule")
        with pytest.raises(ValueError, match="is disabled, not confirmed"):
            svc.disable("org-rule")

    def test_enable_confirmed_is_an_error(self, tmp_path):
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        with pytest.raises(ValueError, match="is confirmed, not disabled"):
            svc.enable("org-rule")

    def test_enable_pending_is_an_error(self, tmp_path):
        svc = _svc(tmp_path)
        svc.create(dict(_REQUIRED))
        with pytest.raises(ValueError, match="is pending, not disabled"):
            svc.enable("org-rule")

    @pytest.mark.parametrize("action", ["disable", "enable"])
    def test_missing_skill_is_keyerror(self, tmp_path, action):
        svc = _svc(tmp_path)
        with pytest.raises(KeyError, match="not found"):
            getattr(svc, action)("ghost")

    def test_confirm_refuses_a_disabled_skill(self, tmp_path):
        """恢复入口只有一个(enable)—— confirm 兼作恢复会让审计史歧义。"""
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        svc.disable("org-rule")
        with pytest.raises(ValueError, match="use enable"):
            svc.confirm("org-rule")
        assert svc.read_skill("org-rule")["status"] == "disabled"  # 没被改写

    def test_status_vocab_has_disabled(self):
        """写路径的词表与 validate 共用 —— 手写 ``status: disabled`` 不是错误。"""
        from trove.services.skills.service import _STATUSES

        assert "disabled" in _STATUSES
        assert _STATUSES == ("pending", "confirmed", "rejected", "disabled")

    def test_drafts_cannot_be_born_disabled(self, tmp_path):
        """自动/起草路径绝不写 disabled:``create`` 的 status 是服务端管理的
        (入参一律被 pending 覆盖)—— 停用只能是管理员的显式 ``disable``。"""
        svc = _svc(tmp_path)
        svc.create({**_REQUIRED, "status": "disabled"})
        assert svc.read_skill("org-rule")["status"] == "pending"


# ── 四个消费面(逐面:生效 → 停 → 复)────────────────────


class TestFace1Injection:
    def test_render_skills_stops_and_restores(self, tmp_path):
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        assert "ORG-BODY-MARKER" in svc.render_skills("query_sketch", lang="en")

        svc.disable("org-rule")
        rendered = svc.render_skills("query_sketch", lang="en")
        assert "ORG-BODY-MARKER" not in rendered
        assert "<org_skill" not in rendered
        assert "Traceability self-check" in rendered       # code skill 照常

        svc.enable("org-rule")
        assert "ORG-BODY-MARKER" in svc.render_skills("query_sketch", lang="en")


class TestFace2AdvertisementAndLoad:
    def test_advertisement_stops_and_restores(self, tmp_path):
        svc = _svc(tmp_path)
        _active(svc, _AVAILABLE)
        assert svc.has_available_for("gen_sql") is True
        assert "org-tip" in svc.available_skills_block("gen_sql", lang="en")

        svc.disable("org-tip")
        assert svc.available_descriptions("gen_sql", lang="en") == []
        assert svc.has_available_for("gen_sql") is False   # load_skill 不注册
        assert svc.available_skills_block("gen_sql", lang="en") == ""

        svc.enable("org-tip")
        assert svc.has_available_for("gen_sql") is True
        assert "org-tip" in svc.available_skills_block("gen_sql", lang="en")

    def test_load_skill_names_the_disable_and_restores(self, tmp_path):
        """点名不是提权:广告不出的名字按名直取同样取不到,且判词说对方向。"""
        svc = _svc(tmp_path)
        _active(svc, _AVAILABLE)
        assert "TIP-BODY-MARKER" in svc.load_skill_content(
            "org-tip", "en", node="gen_sql", skill_ctx={})

        svc.disable("org-tip")
        msg = svc.load_skill_content("org-tip", "en", node="gen_sql",
                                     skill_ctx={})
        assert "disabled by an administrator" in msg
        assert "not confirmed yet" not in msg              # 「未确认」是另一个下一步
        assert "TIP-BODY-MARKER" not in msg

        svc.enable("org-tip")
        assert "TIP-BODY-MARKER" in svc.load_skill_content(
            "org-tip", "en", node="gen_sql", skill_ctx={})


class TestFace3Execution:
    def test_validator_selection_and_run_stop_and_restore(self, tmp_path):
        """走 validate 节点的真实两行:validators_for → run_validators。"""
        from trove.services.skills.validators import VALIDATOR_HOST, run_validators

        svc = _svc(tmp_path)
        _active(svc, _VALIDATOR)

        def _hits():
            return run_validators(
                svc.validators_for(VALIDATOR_HOST),
                columns=["loan_count"], rows=[[-5]], row_count=1, lang="zh",
            )

        assert [h["name"] for h in _hits()] == ["org-check"]   # 违约即命中

        svc.disable("org-check")
        assert svc.validators_for(VALIDATOR_HOST) == []
        assert _hits() == []                                   # 一条都不跑

        svc.enable("org-check")
        assert [h["name"] for h in _hits()] == ["org-check"]

    def test_guard_shares_the_validator_path(self, tmp_path):
        """guard 与 validator 同一条投递路:停用一并断,不需要各自的判据。"""
        from trove.services.skills.guards import GUARD_HOST

        svc = _svc(tmp_path)
        _active(svc, _GUARD)
        runner = GuardRunner(svc)
        assert [v.name for v in runner.check("SELECT * FROM loans", lang="zh")]
        assert [e["name"] for e in svc.guards_for(GUARD_HOST)] == ["org-guard"]

        svc.disable("org-guard")
        assert svc.guards_for(GUARD_HOST) == []
        assert runner.check("SELECT * FROM loans", lang="zh") == []

        svc.enable("org-guard")
        assert [v.name for v in runner.check("SELECT * FROM loans", lang="zh")]


class TestFace4Enumeration:
    def test_dry_run_corpus_stops_and_restores(self, tmp_path):
        """试跑候选集(validator 档 + guard 档)与消费面同一份清单:
        停用的资产不进试跑 —— 「停用」与「试跑通过」必须是两句话。"""
        from trove.services.extensions.dryrun import (
            load_guard_tier,
            validator_specs_for,
        )

        svc = _svc(tmp_path)
        _active(svc, _VALIDATOR)
        _active(svc, _GUARD)

        specs, _ = validator_specs_for(svc, "demo")
        assert [s["name"] for s in specs] == ["org-check"]
        assert [s["name"] for s in load_guard_tier(svc).specs] == ["org-guard"]

        svc.disable("org-check")
        svc.disable("org-guard")
        specs, not_selected = validator_specs_for(svc, "demo")
        assert specs == [] and not_selected == []
        assert load_guard_tier(svc).specs == []

        svc.enable("org-check")
        svc.enable("org-guard")
        assert [s["name"] for s in validator_specs_for(svc, "demo")[0]] == [
            "org-check"]
        assert [s["name"] for s in load_guard_tier(svc).specs] == ["org-guard"]

    def test_envelope_lists_it_but_as_disabled(self, tmp_path):
        """信封是治理声明面:停用资产照列(state=disabled)——
        清单(运行时面)摘除它,信封(声明面)留着它,两者职责不同。"""
        from trove.services.extensions import clear_cache, collect_assets

        clear_cache()
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        svc.disable("org-rule")
        envs = [e for e in collect_assets(tmp_path) if e.source == "org"]
        assert [e.name for e in envs] == ["org-rule"]
        assert envs[0].state == "disabled"
        assert envs[0].mounts                             # 挂点照常推导出来


# ── 管理面照常(停的是消费,不是管理)──────────────────


class TestManagementStaysUsable:
    def test_admin_list_still_shows_it(self, tmp_path):
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        svc.disable("org-rule")

        listed = svc.list_org()                            # 管理端全量
        assert [e["status"] for e in listed] == ["disabled"]
        assert "org-rule" in {e["name"] for e in svc.list_all()}
        assert svc.read_skill("org-rule")["body"].startswith("ORG-BODY-MARKER")

    def test_body_update_works_while_disabled(self, tmp_path):
        """停用期仍可修正文:先修好再 enable,而不是先启用再修。"""
        svc = _svc(tmp_path)
        _active(svc, _REQUIRED)
        svc.disable("org-rule")
        svc.update_body("org-rule", "FIXED-BODY")
        assert svc.read_skill("org-rule")["status"] == "disabled"  # 停用保持
        svc.enable("org-rule")
        assert "FIXED-BODY" in svc.render_skills("query_sketch", lang="en")


# ── git 审计:两条路径各留一条痕 ─────────────────────────


class TestGitTrail:
    def test_disable_and_enable_are_committed_separately(self, tmp_path):
        import os
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        for args in (("init", "-q"), ("config", "user.name", "T"),
                     ("config", "user.email", "t@local")):
            subprocess.run(["git", "-C", str(repo), *args], check=True,
                           capture_output=True, env=env)

        svc = SkillService(root=repo / ".trove" / "skills")
        _active(svc, _REQUIRED)
        svc.disable("org-rule", actor="alice")
        svc.enable("org-rule", actor="bob")

        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%s%n%b"],
            capture_output=True, text=True, env=env).stdout
        assert "skills: disable org-rule" in log
        assert "skills: enable org-rule" in log
        assert "Approved-by: alice" in log                 # 谁停的
        assert "Approved-by: bob" in log                   # 谁开的


# ── 未来面护栏(R6 检查清单的机制化)──────────────────────


#: 允许**不带过滤**读 ``list_org`` 的模块 —— 治理/枚举面,各有其理由。
#: 消费面(注入/广告/执行/试跑/装配清单)必须 ``confirmed_only=True``。
_FULL_LIST_READERS = {
    "trove/api/routers/skills.py",
    "trove/services/validate/service.py",
    "trove/services/presets/service.py",
}


def _list_org_call_sites() -> list[tuple[str, str, ast.Call]]:
    """全仓源码里每一处 ``*.list_org(...)`` 调用(文件, 限定名, 调用节点)。"""
    import trove as _root

    found: list[tuple[str, str, ast.Call]] = []
    pkg = Path(_root.__file__).parent

    def walk(node: ast.AST, path: str, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, path, f"{qual}.{child.name}")
            elif isinstance(child, ast.ClassDef):
                walk(child, path, f"{qual}.{child.name}")
            else:
                if (isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "list_org"):
                    found.append((path, qual, child))
                walk(child, path, qual)

    for path in sorted(pkg.rglob("*.py")):
        rel = path.relative_to(pkg.parent).as_posix()
        walk(ast.parse(path.read_text(encoding="utf-8")), rel, "")
    return found


class TestFutureFaceGuard:
    """加一个新消费面时的红:源码级枚举 ``list_org`` 调用点。

    判据不是"现在的四面都对"(那是上面逐面测试的事),而是**下一个面**:
    新的调用点若不带 ``confirmed_only=True``,且模块不在治理面白名单里
    (``_FULL_LIST_READERS``,每条都有自己的理由),这里直接红 ——
    「忘了加过滤」从一次 review 习惯变成一条测试。
    """

    def test_every_consumption_call_site_filters(self):
        offenders = []
        for path, qual, call in _list_org_call_sites():
            if path in _FULL_LIST_READERS:
                continue
            kwargs = {k.arg for k in call.keywords}
            if "confirmed_only" not in kwargs:
                offenders.append(f"{path}::{qual}")
        assert offenders == [], (
            "以下 list_org 调用点未声明 confirmed_only —— 停用资产会继续投递:"
            f" {offenders}(若这是治理面,请把它加进 _FULL_LIST_READERS 并写明理由)"
        )

    def test_governance_readers_are_exactly_the_allowlist(self):
        """白名单是**闭集**:新的治理面读取者必须显式登记,不能悄悄出现。"""
        governance = {
            path for path, _qual, _call in _list_org_call_sites()
            if path in _FULL_LIST_READERS
        }
        assert governance == _FULL_LIST_READERS
