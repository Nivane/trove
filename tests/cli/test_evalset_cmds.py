"""CLI:子命令解析/退出码/成本护栏(零 LLM)。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trove.cli.commands.evalset_cmds import main_evalset


def test_no_args_prints_usage_and_exits_2(capsys):
    assert main_evalset([]) == 2
    assert "evalset" in capsys.readouterr().out


def test_unknown_subcommand_exits_2():
    assert main_evalset(["nope"]) == 2


def test_run_without_yes_is_dry_run(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from trove.cli.commands import evalset_cmds as c

    async def fake_build(root, ds):
        return SimpleNamespace(config=object(), kb=object(), registry=object(),
                               adapter=object(), semantic_layer=None,
                               auth_store=None)

    monkeypatch.setattr(c, "build_runtime", fake_build)
    seen = {}
    async def fake_run(options):
        seen["yes"] = options.yes
        return SimpleNamespace(exit_code=0, message="dry-run")

    monkeypatch.setattr(c, "run_evalset", fake_run)
    assert main_evalset([
        "run", "--datasource", "demo", "--project-root", str(tmp_path),
    ]) == 0
    assert seen["yes"] is False


def test_run_yes_requires_explicit_flag(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from trove.cli.commands import evalset_cmds as c
    async def fake_build(root, ds):
        return SimpleNamespace(config=object(), kb=object(), registry=object(),
                               adapter=object(), semantic_layer=None,
                               auth_store=None)
    monkeypatch.setattr(c, "build_runtime", fake_build)
    async def fake_run(options):
        return SimpleNamespace(exit_code=0, message="ran", summary={}, entries=[])
    monkeypatch.setattr(c, "run_evalset", fake_run)
    assert main_evalset([
        "run", "--datasource", "demo", "--yes",
        "--project-root", str(tmp_path),
    ]) == 0


# ── 本任务的补充用例(控制器裁定 R25/R26 与成本护栏)─────────────


def _runtime(**overrides):
    """最小运行时替身:config 带起草档选模与 providers。"""
    base = dict(
        config=SimpleNamespace(
            model_for_draft=lambda node, complexity: "draft-model",
            providers=[],
        ),
        kb=object(), registry=object(), adapter=object(),
        semantic_layer=None, auth_store=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_build(monkeypatch, runtime=None):
    from trove.cli.commands import evalset_cmds as c

    runtime = runtime if runtime is not None else _runtime()

    async def fake_build(root, ds):
        return runtime

    monkeypatch.setattr(c, "build_runtime", fake_build)
    return c


class _NoLLM:
    """构造即失败:成本干跑不允许碰 LLMGateway。"""

    def __init__(self, *args, **kwargs):
        raise AssertionError("LLMGateway must not be constructed without --yes")


class _RecordingRegistry:
    def __init__(self):
        self.closed = False
        self.calls = 0

    async def close_all(self):
        self.calls += 1
        self.closed = True


@pytest.mark.parametrize("extra", [
    ["--sources", "semantic,synthetic"],
    ["--rewrite"],
])
def test_generate_llm_paths_require_yes(tmp_path, monkeypatch, capsys, extra):
    """含 synthetic 或 --rewrite 且缺 --yes → 只报模型与题数,零 LLM。"""
    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "LLMGateway", _NoLLM)

    async def unexpected(*args, **kwargs):
        raise AssertionError("generation must not run in a cost dry-run")

    monkeypatch.setattr(c, "generate_evalset", unexpected)

    code = main_evalset([
        "generate", "--datasource", "demo", "--limit", "7",
        "--project-root", str(tmp_path), *extra,
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "draft-model" in out
    assert "7" in out


def test_generate_semantic_only_needs_no_llm(tmp_path, monkeypatch):
    """语义生成是确定性的:不构造 LLMGateway,llm=None。"""
    seen = {}

    async def fake_generate(root, request, **kw):
        from trove.services.evalset.generate import GenerateReport

        seen["request"] = request
        seen.update(kw)
        return GenerateReport(datasource=request.datasource, status="ok", added=1)

    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "generate_evalset", fake_generate)
    monkeypatch.setattr(c, "LLMGateway", _NoLLM)

    assert main_evalset([
        "generate", "--datasource", "demo", "--project-root", str(tmp_path),
    ]) == 0
    assert seen["llm"] is None
    assert seen["draft_model"] == "draft-model"
    assert seen["request"].sources == ["semantic"]
    assert seen["request"].limit == 30


@pytest.mark.parametrize("status,expected", [
    ("ok", 0),
    ("empty", 0),
    ("invalid_asset", 1),
    ("no_semantic_model", 1),
    ("no_llm", 1),
])
def test_generate_exit_code_mapping(
    tmp_path, monkeypatch, capsys, status, expected,
):
    """R25:ok/empty → 0;其余失败状态 → 1。"""
    async def fake_generate(root, request, **kw):
        from trove.services.evalset.generate import GenerateReport

        return GenerateReport(datasource=request.datasource, status=status)

    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "generate_evalset", fake_generate)

    assert main_evalset([
        "generate", "--datasource", "demo", "--project-root", str(tmp_path),
    ]) == expected
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status


def test_run_tier_all_returns_max_exit_code_in_tier_order(
    tmp_path, monkeypatch, capsys,
):
    """R25:--tier all 依次 verified/semantic/synthetic,取最大退出码。"""
    registry = _RecordingRegistry()
    c = _patch_build(monkeypatch, _runtime(registry=registry))
    tiers: list[str] = []
    codes = {"verified": 0, "semantic": 2, "synthetic": 1}

    async def fake_run(options):
        tiers.append(options.tier)
        return SimpleNamespace(
            exit_code=codes[options.tier], message=f"{options.tier} done")

    monkeypatch.setattr(c, "run_evalset", fake_run)

    assert main_evalset([
        "run", "--datasource", "demo", "--tier", "all",
        "--project-root", str(tmp_path),
    ]) == 2
    assert tiers == ["verified", "semantic", "synthetic"]
    assert registry.closed
    out = capsys.readouterr().out
    assert "verified done" in out and "semantic done" in out


def test_run_maps_flags_into_run_options(tmp_path, monkeypatch):
    captured = {}

    async def fake_run(options):
        captured.update(vars(options))
        return SimpleNamespace(exit_code=0, message="")

    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "run_evalset", fake_run)

    assert main_evalset([
        "run", "--datasource", "demo", "--tier", "semantic",
        "--limit", "3", "--sample", "2", "--compile-only", "--strict",
        "--lang", "en", "--project-root", str(tmp_path),
    ]) == 0
    assert captured["tier"] == "semantic"
    assert captured["limit"] == 3
    assert captured["sample"] == 2
    assert captured["compile_only"] is True
    assert captured["strict"] is True
    assert captured["lang"] == "en"
    assert captured["yes"] is False


def test_write_actor_defaults_to_env_then_cli(tmp_path, monkeypatch, capsys):
    """R25:--actor 缺省 = TROVE_ACTOR,再缺省 "cli"。"""
    actors: list[str] = []

    class FakeService:
        def __init__(self, root, **kw):
            pass

        async def create(self, ds, **kw):
            actors.append(kw["actor"])
            return {"qid": "e-1", "status": "pending", "tier": "verified"}

    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "EvalsetService", FakeService)

    monkeypatch.setenv("TROVE_ACTOR", "alice")
    assert main_evalset([
        "create", "--datasource", "demo", "--question", "q?",
        "--sql", "SELECT 1", "--project-root", str(tmp_path),
    ]) == 0

    monkeypatch.delenv("TROVE_ACTOR", raising=False)
    assert main_evalset([
        "create", "--datasource", "demo", "--question", "q?",
        "--sql", "SELECT 1", "--project-root", str(tmp_path),
    ]) == 0
    assert actors == ["alice", "cli"]


def test_create_parses_tags_and_forwards_force(tmp_path, monkeypatch):
    seen = {}

    class FakeService:
        def __init__(self, root, **kw):
            pass

        async def create(self, ds, **kw):
            seen.update(kw)
            return {"qid": "e-1", "status": "pending", "tier": "verified"}

    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "EvalsetService", FakeService)

    assert main_evalset([
        "create", "--datasource", "demo", "--question", "q?",
        "--sql", "SELECT 1", "--tags", "metric=loan,pattern=top_n",
        "--note", "n", "--force", "--actor", "bob",
        "--project-root", str(tmp_path),
    ]) == 0
    assert seen["tags"] == {"metric": "loan", "pattern": "top_n"}
    assert seen["force"] is True
    assert seen["note"] == "n"
    assert seen["actor"] == "bob"


def test_create_malformed_tags_is_usage_error(tmp_path, monkeypatch, capsys):
    built = {"called": False}

    async def fake_build(root, ds):
        built["called"] = True
        return _runtime()

    from trove.cli.commands import evalset_cmds as c

    monkeypatch.setattr(c, "build_runtime", fake_build)

    assert main_evalset([
        "create", "--datasource", "demo", "--question", "q?",
        "--sql", "SELECT 1", "--tags", "oops",
        "--project-root", str(tmp_path),
    ]) == 2
    assert built["called"] is False
    assert "error:" in capsys.readouterr().err


def test_write_error_prints_code_and_issues_to_stderr(
    tmp_path, monkeypatch, capsys,
):
    """R25:EvalsetError → stderr `error: <code>: <message>` + issues,退出 1。"""
    from trove.services.evalset.models import EvalsetError

    class FakeService:
        def __init__(self, root, **kw):
            pass

        async def confirm(self, ds, qid, **kw):
            raise EvalsetError(
                "evalset.not_found", f"no eval item {qid!r}",
                issues=[{"qid": qid}],
            )

    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "EvalsetService", FakeService)

    assert main_evalset([
        "confirm", "e-404", "--datasource", "demo",
        "--project-root", str(tmp_path),
    ]) == 1
    err = capsys.readouterr().err
    assert "error: evalset.not_found:" in err
    assert "e-404" in err


def test_list_human_lines_and_json(tmp_path, monkeypatch, capsys):
    payload = {
        "datasource": "demo",
        "items": [{
            "qid": "e-1", "status": "pending", "tier": "verified",
            "source": "semantic", "question": "哪个地区贷款最多?",
        }],
        "counts": {"pending": 1, "total": 1},
        "by_tier": {},
        "issues": [],
        "degraded": [],
    }

    class FakeService:
        def __init__(self, root, **kw):
            pass

        async def list_items(self, ds, **kw):
            return payload

    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "EvalsetService", FakeService)

    assert main_evalset([
        "list", "--datasource", "demo", "--project-root", str(tmp_path),
    ]) == 0
    line = capsys.readouterr().out.strip()
    assert line == "e-1 pending verified semantic 哪个地区贷款最多?"

    assert main_evalset([
        "list", "--datasource", "demo", "--json",
        "--project-root", str(tmp_path),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["qid"] == "e-1"


def test_readiness_none_values_render_unknown_not_zero(
    tmp_path, monkeypatch, capsys,
):
    """R24:available=false 时 None 一律打印 unknown,绝不洗成 0。"""

    class FakeService:
        def __init__(self, root, **kw):
            pass

        async def readiness(self, ds):
            return {
                "datasource": ds,
                "available": False,
                "counts": None,
                "by_tier": None,
                "coverage": None,
                "latest_runs": {
                    "verified": None, "semantic": None, "synthetic": None,
                },
                "degraded": [{
                    "code": "asset_unreadable", "message": "bad evalset.yml",
                }],
                "evalset_rev": None,
            }

    c = _patch_build(monkeypatch)
    monkeypatch.setattr(c, "EvalsetService", FakeService)

    assert main_evalset([
        "readiness", "--datasource", "demo", "--project-root", str(tmp_path),
    ]) == 0
    out = capsys.readouterr().out
    assert "available: False" in out
    assert "counts: unknown" in out
    assert "evalset_rev: unknown" in out
    assert "verified: unknown" in out
    assert "asset_unreadable" in out


# ── build_runtime 解析(R26)────────────────────────────────────


async def test_build_runtime_registers_scheme_url(tmp_path, monkeypatch):
    from trove.cli.commands import evalset_cmds as c
    from trove.core.config import AgentConfig

    monkeypatch.setattr(
        c, "ConfigLoader",
        SimpleNamespace(load_agent_config=lambda path=None: AgentConfig()),
    )
    runtime = await c.build_runtime(tmp_path, "sqlite://:memory:")
    try:
        assert runtime.adapter.dialect() == "sqlite"
        assert runtime.kb.kb_dir == tmp_path / ".trove" / "kb"
        assert runtime.semantic_layer is None
    finally:
        await c._close_runtime(runtime)


async def test_build_runtime_registers_named_datasource(tmp_path, monkeypatch):
    import yaml

    from trove.cli.commands import evalset_cmds as c
    from trove.core.config import AgentConfig

    db_path = tmp_path / "mini.db"
    db_path.touch()
    trove_dir = tmp_path / ".trove"
    trove_dir.mkdir()
    (trove_dir / "datasources.yml").write_text(
        yaml.safe_dump({"datasources": [{
            "name": "mini", "type": "sqlite",
            "connection": {"path": str(db_path)},
        }]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        c, "ConfigLoader",
        SimpleNamespace(load_agent_config=lambda path=None: AgentConfig()),
    )
    runtime = await c.build_runtime(tmp_path, "mini")
    try:
        assert runtime.adapter is not None
        assert runtime.kb.kb_dir == tmp_path / ".trove" / "kb"
    finally:
        await c._close_runtime(runtime)


async def test_build_runtime_unknown_name_raises_datasource_error(
    tmp_path, monkeypatch,
):
    from trove.cli.commands import evalset_cmds as c
    from trove.core.config import AgentConfig
    from trove.core.errors import DatasourceError

    monkeypatch.setattr(
        c, "ConfigLoader",
        SimpleNamespace(load_agent_config=lambda path=None: AgentConfig()),
    )
    with pytest.raises(DatasourceError):
        await c.build_runtime(tmp_path, "no-such-datasource")
