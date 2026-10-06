"""装配 dump 测试 —— 清单形状 / 落盘语义 / 与 .log 成对的保留策略。

assembly.json 是「这一问用了什么」的 run 级结构化清单,与 .log 同款语义:
同一 run_id 重跑覆盖、按 MAX_RUN_LOGS 成对裁剪。零 LLM / 零网络。
"""

import json
from pathlib import Path
from types import SimpleNamespace

from trove.tracing.assembly import (
    BLOCK_NAMES,
    build_report,
    tools_from_registry,
    write_report,
)
from trove.tracing.local import configure_trace_store
from trove.tracing.runlog import MAX_RUN_LOGS, create_tracer


def _spec(name, level="core", roles=None):
    return SimpleNamespace(name=name, level=level, roles=roles)


class TestBuildReport:
    """state → 清单:schema 三类节 + 字段名一次定死。"""

    def _state(self, **over):
        base = dict(
            run_id="r1", datasource="demo", complexity="standard",
            assembly_blocks=[
                {"name": "few_shots", "tokens": 120, "truncated": True,
                 "items": [
                     {"ref": "How many loans?", "tokens": 70, "truncated": False},
                     {"ref": "Loans by district", "tokens": 50, "truncated": True},
                 ]},
                {"name": "plan", "tokens": 30, "truncated": False,
                 "items": [{"ref": "plan", "tokens": 30, "truncated": False}]},
            ],
            assembly_tools=[
                {"name": "check_result", "level": "core", "roles": None,
                 "lazy": False, "activated": True, "calls": 2},
            ],
            validator_hits=[{"name": "v1", "verdict": False}],
            validation_hits=[{"name": "F1_shape", "reason": "one row expected"}],
            fast_path=False,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_three_sections_and_exact_block_shape(self):
        report = build_report(self._state())
        assert set(report) == {
            "run_id", "datasource", "complexity", "blocks", "tools", "verdicts",
        }
        assert report["run_id"] == "r1"
        assert report["datasource"] == "demo"
        assert report["complexity"] == "standard"

        shots = report["blocks"][0]
        assert set(shots) == {"name", "tokens", "truncated", "items"}
        assert [it["ref"] for it in shots["items"]] == [
            "How many loans?", "Loans by district",
        ]
        assert set(shots["items"][0]) == {"ref", "tokens", "truncated"}

    def test_block_names_within_declared_closure(self):
        report = build_report(self._state())
        assert {b["name"] for b in report["blocks"]} <= set(BLOCK_NAMES)

    def test_tools_section_shape(self):
        report = build_report(self._state())
        tool = report["tools"][0]
        assert set(tool) == {
            "name", "level", "roles", "lazy", "activated", "calls",
        }
        assert tool["name"] == "check_result"
        assert tool["calls"] == 2

    def test_verdicts_section(self):
        v = build_report(self._state()).get("verdicts")
        assert v is not None
        assert set(v) == {
            "validator_hits", "guard_hits", "rule_hits", "fast_path_hit",
        }
        assert v["validator_hits"][0]["name"] == "v1"
        assert v["guard_hits"] == []  # 本 state 未跑过 guard(缺失降级为空)
        assert v["rule_hits"][0]["name"] == "F1_shape"
        assert v["fast_path_hit"] is False

    def test_missing_state_fields_degrade_to_empty(self):
        """dump 是观测物:state 缺字段/为空一律降级,绝不抛出。"""
        report = build_report(SimpleNamespace())
        assert report["run_id"] == ""
        assert report["blocks"] == []
        assert report["tools"] == []
        assert report["verdicts"] == {
            "validator_hits": [], "guard_hits": [],
            "rule_hits": [], "fast_path_hit": False,
        }

    def test_junk_entries_normalized_not_leaked(self):
        report = build_report(self._state(
            assembly_blocks=[{"name": "rules", "tokens": "12",
                              "items": [{"ref": "r1", "tokens": None},
                                        {"ref": "r2", "tokens": "abc"},
                                        "not-a-dict"]},
                             "not-a-dict"],
            assembly_tools=[{"name": "t", "roles": "analyst", "calls": "x"}],
        ))
        blk = report["blocks"][0]
        assert blk["tokens"] == 12 and blk["truncated"] is False
        assert blk["items"] == [
            {"ref": "r1", "tokens": 0, "truncated": False},
            {"ref": "r2", "tokens": 0, "truncated": False},  # 不可解析 → 0,不抛出
        ]
        tool = report["tools"][0]
        assert tool["roles"] is None and tool["calls"] == 0
        assert tool["lazy"] is False and tool["activated"] is False


class TestToolsFromRegistry:
    """ToolRegistry → tools 节:level/roles 直读,lazy 快照判定激活,calls 计数。"""

    def _registry(self):
        return SimpleNamespace(
            _specs={
                "validate_sql": _spec("validate_sql"),
                "search_values": _spec(
                    "search_values", level="catalog", roles=["analyst"]),
            },
            _lazy_specs={
                "lookup_schema": _spec(
                    "lookup_schema", level="catalog", roles=["analyst"]),
            },
        )

    def test_lazy_pending_and_roles_passthrough(self):
        tools = tools_from_registry(
            self._registry(), lazy_names={"lookup_schema"},
            tool_history=[{"name": "validate_sql"}, {"name": "validate_sql"}],
        )
        by_name = {t["name"]: t for t in tools}
        assert by_name["validate_sql"]["calls"] == 2
        assert by_name["validate_sql"]["lazy"] is False
        assert by_name["validate_sql"]["activated"] is True
        assert by_name["validate_sql"]["roles"] is None  # 不限角色原样透出
        assert by_name["search_values"]["level"] == "catalog"
        assert by_name["search_values"]["roles"] == ["analyst"]
        # 懒注册且从未激活:模型从头到尾没在 defs 里见过它
        assert by_name["lookup_schema"]["lazy"] is True
        assert by_name["lookup_schema"]["activated"] is False
        assert by_name["lookup_schema"]["calls"] == 0

    def test_activated_lazy_tool_moved_out_of_pending(self):
        registry = self._registry()
        spec = registry._lazy_specs.pop("lookup_schema")
        registry._specs["lookup_schema"] = spec  # activate_lazy 的效果
        tools = tools_from_registry(registry, lazy_names={"lookup_schema"})
        tool = next(t for t in tools if t["name"] == "lookup_schema")
        assert tool["lazy"] is True and tool["activated"] is True

    def test_broken_registry_degrades_to_empty(self):
        assert tools_from_registry(object()) == []
        assert tools_from_registry(None) == []


class TestWriteReport:
    """落盘:runs/{run_id}.assembly.json,同 run_id 覆盖。"""

    def test_write_and_overwrite(self, tmp_path):
        configure_trace_store(tmp_path)
        first = build_report(SimpleNamespace(run_id="r1", complexity="simple"))
        path = write_report("r1", first)
        assert path == tmp_path / "runs" / "r1.assembly.json"
        assert json.loads(path.read_text(encoding="utf-8"))["complexity"] == "simple"

        write_report("r1", build_report(SimpleNamespace(run_id="r1", complexity="complex")))
        assert json.loads(path.read_text(encoding="utf-8"))["complexity"] == "complex"
        assert len(list((tmp_path / "runs").glob("*.assembly.json"))) == 1

    def test_unconfigured_store_is_noop(self):
        assert write_report("r1", {"run_id": "r1"}) is None

    def test_tracer_finish_writes_dump_next_to_log(self, tmp_path):
        configure_trace_store(tmp_path)
        tracer = create_tracer("r1")
        tracer.start_run({"question": "有多少贷款？"})
        report = build_report(SimpleNamespace(run_id="r1", datasource="demo"))
        tracer.finish({"verdict": "OK"}, assembly=report)

        runs = tmp_path / "runs"
        assert (runs / "r1.log").exists()
        assert json.loads((runs / "r1.assembly.json").read_text(encoding="utf-8")) == report

    def test_finish_without_assembly_leaves_no_dump(self, tmp_path):
        """没给清单就不落文件(与 .log 同为 tracer 产物,不是每 run 必写)。"""
        configure_trace_store(tmp_path)
        tracer = create_tracer("r1")
        tracer.start_run({"question": "q"})
        tracer.finish({"verdict": "OK"})
        assert not list((tmp_path / "runs").glob("*.assembly.json"))


class TestTrimPairsManifests:
    """保留策略与 .log 同步:被裁的 run 日志连同清单一起删。"""

    @staticmethod
    def _seed_pair(runs_dir: Path, run_id: str) -> None:
        (runs_dir / f"{run_id}.log").write_text("x", encoding="utf-8")
        (runs_dir / f"{run_id}.assembly.json").write_text("{}", encoding="utf-8")

    def test_trim_removes_stale_log_with_its_manifest(self, tmp_path):
        configure_trace_store(tmp_path)
        runs = tmp_path / "runs"
        runs.mkdir(parents=True)
        for i in range(MAX_RUN_LOGS):
            self._seed_pair(runs, f"old{i:03d}")

        tracer = create_tracer("new")
        tracer.start_run({"question": "q"})
        tracer.finish({}, assembly={"run_id": "new", "datasource": ""})

        logs = sorted(p.name for p in runs.glob("*.log"))
        manifests = sorted(p.name for p in runs.glob("*.assembly.json"))
        assert len(logs) == MAX_RUN_LOGS and len(manifests) == MAX_RUN_LOGS
        assert "new.log" in logs and "new.assembly.json" in manifests
        # 最老的 run 及其清单被成对裁掉,不留孤儿清单
        assert "old000.log" not in logs
        assert "old000.assembly.json" not in manifests

    def test_orphan_manifests_bounded_by_same_cap(self, tmp_path):
        """只落了清单(无日志)的异常路径同样有界,不会无限堆积。"""
        configure_trace_store(tmp_path)
        runs = tmp_path / "runs"
        runs.mkdir(parents=True)
        for i in range(MAX_RUN_LOGS + 5):
            (runs / f"orphan{i:03d}.assembly.json").write_text("{}", encoding="utf-8")

        tracer = create_tracer("new")
        tracer.start_run({"question": "q"})
        tracer.finish({})

        assert len(list(runs.glob("*.assembly.json"))) == MAX_RUN_LOGS
