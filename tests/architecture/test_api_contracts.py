"""API 响应契约的机检面 —— 快照漂移 + 端点类型棘轮。

三件事:

* **单一类型源** —— `frontend/src/api/openapi.json` 必须与
  `scripts/gen_openapi.build_openapi_document()` 的渲染结果逐字节一致。
  模型改了而快照没重新生成 → 这里红（前端的生成类型随即停在同一份旧契约）。
* **响应模型棘轮** —— 两个冻结集合。``UNTYPED_DICT_ENDPOINTS`` 只许变少
  （把 ``-> dict`` 收编成模型是进步）；``TYPED_ENDPOINTS`` 双向冻结
  （模型被删/端点改名同样要在这里登记，不许悄悄退回无契约）。
* **类型确实到达线上** —— TYPED 端点在 OpenAPI 里的 200 响应必须引用
  components 里的模型，而不是 ``type: object``。

改契约时:改模型 → `uv run python scripts/gen_openapi.py` → 前端
`npm run types:generate` → 更新这里的两个集合。全部一起提交。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.dependencies.utils import get_typed_return_annotation
from fastapi.routing import APIRoute, iter_route_contexts

from tests.helpers import arch

ROOT = arch.ROOT
GEN_SCRIPT = ROOT / "scripts" / "gen_openapi.py"
SNAPSHOT = ROOT / "frontend" / "src" / "api" / "openapi.json"

#: 返回 ``-> dict`` 且没有 `response_model` 的端点（方法, 路径）。
#: **只许变少**：新写的裸 dict 端点会让这里红 —— 要么给它一个响应模型，
#: 要么在评审里显式扩大这个集合。搬走一条就两边各改一行。
UNTYPED_DICT_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/v1/admin/actions/proposals"),
    ("POST", "/v1/admin/actions/proposals/batch"),
    ("GET", "/v1/admin/actions/proposals/{proposal_id}"),
    ("GET", "/v1/admin/actions/proposals/{proposal_id}/outcomes"),
    ("POST", "/v1/admin/actions/proposals/{proposal_id}/{decision}"),
    ("GET", "/v1/admin/actions/templates"),
    ("POST", "/v1/admin/actions/templates"),
    ("POST", "/v1/admin/actions/templates/{name}/confirm"),
    ("POST", "/v1/admin/actions/templates/{name}/reject"),
    ("GET", "/v1/admin/audit"),
    ("GET", "/v1/admin/audit/refusal-report"),
    ("GET", "/v1/admin/coverage"),
    ("GET", "/v1/admin/datasources"),
    ("POST", "/v1/admin/datasources"),
    ("POST", "/v1/admin/datasources/test-connection"),
    ("GET", "/v1/admin/datasources/{name}"),
    ("PUT", "/v1/admin/datasources/{name}"),
    ("GET", "/v1/admin/datasources/{name}/evalset"),
    ("POST", "/v1/admin/datasources/{name}/evalset/confirm"),
    ("POST", "/v1/admin/datasources/{name}/evalset/generate"),
    ("GET", "/v1/admin/datasources/{name}/evalset/generate/status"),
    ("POST", "/v1/admin/datasources/{name}/evalset/items"),
    ("GET", "/v1/admin/datasources/{name}/evalset/readiness"),
    ("POST", "/v1/admin/datasources/{name}/evalset/run"),
    ("GET", "/v1/admin/datasources/{name}/evalset/run/status"),
    ("GET", "/v1/admin/datasources/{name}/evalset/runs"),
    ("GET", "/v1/admin/datasources/{name}/evalset/{qid}"),
    ("PATCH", "/v1/admin/datasources/{name}/evalset/{qid}"),
    ("POST", "/v1/admin/datasources/{name}/evalset/{qid}/confirm"),
    ("GET", "/v1/admin/datasources/{name}/evalset/{qid}/preview"),
    ("POST", "/v1/admin/datasources/{name}/evalset/{qid}/reject"),
    ("POST", "/v1/admin/datasources/{name}/evalset/{qid}/retire"),
    ("POST", "/v1/admin/datasources/{name}/evalset/{qid}/vouch"),
    ("DELETE", "/v1/admin/datasources/{name}/kb"),
    ("GET", "/v1/admin/datasources/{name}/kb"),
    ("POST", "/v1/admin/datasources/{name}/kb/distill-history"),
    ("GET", "/v1/admin/datasources/{name}/kb/distill-history/status"),
    ("POST", "/v1/admin/datasources/{name}/kb/init"),
    ("GET", "/v1/admin/datasources/{name}/kb/init/status"),
    ("POST", "/v1/admin/datasources/{name}/kb/reload"),
    ("GET", "/v1/admin/datasources/{name}/kb/reload/status"),
    ("POST", "/v1/admin/datasources/{name}/reconnect"),
    ("GET", "/v1/admin/decisions"),
    ("PUT", "/v1/admin/decisions"),
    ("GET", "/v1/admin/decisions/drafts"),
    ("POST", "/v1/admin/decisions/drafts/{draft_id}/confirm"),
    ("POST", "/v1/admin/decisions/drafts/{draft_id}/reject"),
    ("GET", "/v1/admin/decisions/quality"),
    ("GET", "/v1/admin/decisions/raw"),
    ("GET", "/v1/admin/decisions/verdicts/{verdict_id}"),
    ("GET", "/v1/admin/decisions/{rule_id}"),
    ("POST", "/v1/admin/decisions/{rule_id}/simulate"),
    ("GET", "/v1/admin/decisions/{rule_id}/verdicts"),
    ("GET", "/v1/admin/deliveries"),
    ("GET", "/v1/admin/drift"),
    ("POST", "/v1/admin/drift/check"),
    ("POST", "/v1/admin/drift/external"),
    ("GET", "/v1/admin/drift/runs"),
    ("GET", "/v1/admin/drift/{drift_id}"),
    ("POST", "/v1/admin/drift/{drift_id}/resolve"),
    ("POST", "/v1/admin/drift/{drift_id}/waive"),
    ("GET", "/v1/admin/facts"),
    ("POST", "/v1/admin/index"),
    ("GET", "/v1/admin/jobs"),
    ("POST", "/v1/admin/jobs"),
    ("GET", "/v1/admin/jobs/{job_id}"),
    ("PATCH", "/v1/admin/jobs/{job_id}"),
    ("POST", "/v1/admin/jobs/{job_id}/run"),
    ("GET", "/v1/admin/jobs/{job_id}/runs"),
    ("POST", "/v1/admin/jobs/{job_id}/subscriptions"),
    ("POST", "/v1/admin/kb/lessons/{pattern}/confirm"),
    ("POST", "/v1/admin/kb/lessons/{pattern}/reject"),
    ("GET", "/v1/admin/memory/preferences"),
    ("POST", "/v1/admin/memory/preferences/{draft_id}/confirm"),
    ("POST", "/v1/admin/memory/preferences/{draft_id}/reject"),
    ("GET", "/v1/admin/memory/profile"),
    ("GET", "/v1/admin/presets"),
    ("GET", "/v1/admin/presets/{name}"),
    ("PUT", "/v1/admin/presets/{name}"),
    ("POST", "/v1/admin/presets/{name}/apply"),
    ("GET", "/v1/admin/presets/{name}/history"),
    ("POST", "/v1/admin/presets/{name}/rollback"),
    ("GET", "/v1/admin/quality/overview"),
    ("POST", "/v1/admin/question-coverage/parse-csv"),
    ("POST", "/v1/admin/question-coverage/preflight"),
    ("GET", "/v1/admin/question-coverage/runs"),
    ("POST", "/v1/admin/question-coverage/runs"),
    ("GET", "/v1/admin/question-coverage/runs/{run_id}"),
    ("POST", "/v1/admin/question-coverage/runs/{run_id}/cancel"),
    ("GET", "/v1/admin/question-coverage/runs/{run_id}/diff"),
    ("POST", "/v1/admin/question-coverage/runs/{run_id}/drafts"),
    ("GET", "/v1/admin/question-coverage/runs/{run_id}/status"),
    ("GET", "/v1/admin/runs/{run_id}/record"),
    ("GET", "/v1/admin/semantic/{name}"),
    ("GET", "/v1/admin/semantic/{name}/changes"),
    ("POST", "/v1/admin/semantic/{name}/changes"),
    ("GET", "/v1/admin/semantic/{name}/changes/{change_id}"),
    ("POST", "/v1/admin/semantic/{name}/changes/{change_id}/merge"),
    ("POST", "/v1/admin/semantic/{name}/changes/{change_id}/reject"),
    ("POST", "/v1/admin/semantic/{name}/changes/{change_id}/verify"),
    ("POST", "/v1/admin/semantic/{name}/drafts"),
    ("POST", "/v1/admin/semantic/{name}/drafts/{draft_id}/confirm"),
    ("POST", "/v1/admin/semantic/{name}/drafts/{draft_id}/reject"),
    ("GET", "/v1/admin/semantic/{name}/history"),
    ("GET", "/v1/admin/semantic/{name}/object-graph"),
    ("POST", "/v1/admin/semantic/{name}/rollback"),
    ("GET", "/v1/admin/sessions"),
    ("GET", "/v1/admin/sessions/{session_id}/checkpoints"),
    ("GET", "/v1/admin/sessions/{session_id}/checkpoints/{checkpoint_id}"),
    ("POST", "/v1/admin/sessions/{session_id}/checkpoints/{checkpoint_id}/resume"),
    ("GET", "/v1/admin/settings"),
    ("PUT", "/v1/admin/settings"),
    ("GET", "/v1/admin/skills"),
    ("POST", "/v1/admin/skills/draft"),
    ("POST", "/v1/admin/skills/llm-draft"),
    ("GET", "/v1/admin/skills/org"),
    ("GET", "/v1/admin/skills/{name}/body"),
    ("PUT", "/v1/admin/skills/{name}/body"),
    ("POST", "/v1/admin/skills/{name}/confirm"),
    ("POST", "/v1/admin/skills/{name}/disable"),
    ("POST", "/v1/admin/skills/{name}/enable"),
    ("GET", "/v1/admin/skills/{name}/history"),
    ("POST", "/v1/admin/skills/{name}/reject"),
    ("POST", "/v1/admin/skills/{name}/rollback"),
    ("POST", "/v1/admin/skills/{name}/tier"),
    ("GET", "/v1/admin/subscriptions"),
    ("PATCH", "/v1/admin/subscriptions/{sub_id}"),
    ("GET", "/v1/admin/todos"),
    ("GET", "/v1/admin/users"),
    ("POST", "/v1/admin/users"),
    ("PATCH", "/v1/admin/users/{user_id}"),
    ("GET", "/v1/admin/users/{user_id}/data-grants"),
    ("PUT", "/v1/admin/users/{user_id}/data-grants"),
    ("GET", "/v1/admin/users/{user_id}/datasources"),
    ("PUT", "/v1/admin/users/{user_id}/datasources"),
    ("GET", "/v1/admin/users/{user_id}/tokens"),
    ("POST", "/v1/admin/users/{user_id}/tokens"),
    ("GET", "/v1/admin/users/{user_id}/topic-grants"),
    ("PUT", "/v1/admin/users/{user_id}/topic-grants"),
    ("POST", "/v1/auth/login"),
    ("POST", "/v1/auth/logout"),
    ("GET", "/v1/auth/me"),
    ("GET", "/v1/auth/oidc/status"),
    ("GET", "/v1/catalog/datasources"),
    ("GET", "/v1/catalog/search"),
    ("GET", "/v1/catalog/tables"),
    ("GET", "/v1/catalog/tables/{table_name}"),
    ("GET", "/v1/catalog/tables/{table_name}/ddl"),
    ("POST", "/v1/catalog/upload"),
    ("GET", "/v1/facts"),
    ("POST", "/v1/facts"),
    ("PATCH", "/v1/facts/{fact_id}"),
    ("GET", "/v1/kb/assets"),
    ("GET", "/v1/kb/entries"),
    ("GET", "/v1/kb/examples"),
    ("POST", "/v1/kb/examples"),
    ("POST", "/v1/kb/examples/confirm-one"),
    ("POST", "/v1/kb/examples/draft"),
    ("GET", "/v1/kb/examples/pending"),
    ("POST", "/v1/kb/examples/reject-one"),
    ("GET", "/v1/kb/lessons"),
    ("POST", "/v1/kb/lessons"),
    ("POST", "/v1/kb/lessons/confirm-one"),
    ("POST", "/v1/kb/lessons/reject-one"),
    ("POST", "/v1/kb/ratings"),
    ("GET", "/v1/kb/rules"),
    ("GET", "/v1/kb/status"),
    ("GET", "/v1/kb/tables/{table_name}/notes"),
    ("GET", "/v1/kb/terms"),
    ("POST", "/v1/kb/terms"),
    ("GET", "/v1/lineage/tables/{name}"),
    ("POST", "/v1/sessions/{session_id}/clear"),
    ("POST", "/v1/sessions/{session_id}/compact"),
    ("POST", "/v1/sessions/{session_id}/pin"),
    ("GET", "/v1/sessions/{session_id}/tasks"),
    ("POST", "/v1/sessions/{session_id}/title"),
    ("GET", "/v1/subscriptions"),
    ("PATCH", "/v1/subscriptions/{sub_id}"),
    ("GET", "/v1/subscriptions/{sub_id}/deliveries"),
})

#: 有 `response_model` 的端点（双向冻结）。本批新增三条读端点。
TYPED_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v1/admin/semantic/{name}/drafts/batch"),
    ("POST", "/v1/admin/semantic/{name}/preview"),
    ("POST", "/v1/admin/semantic/{name}/validate"),
    ("POST", "/v1/kb/examples/confirm"),
    ("POST", "/v1/kb/examples/reject"),
    ("POST", "/v1/kb/lessons/confirm"),
    ("GET", "/v1/runs/{run_id}"),
    ("GET", "/v1/sessions"),
    ("POST", "/v1/sessions"),
    ("GET", "/v1/sessions/{session_id}"),
})


def _load_gen_script():
    """按文件加载生成脚本（`scripts/` 不是包，导入一次即复用其构造路径）。"""
    spec = importlib.util.spec_from_file_location("gen_openapi", GEN_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app():
    gen = _load_gen_script()
    from trove.api.app import create_app

    return create_app({"auth": gen._OfflineAuth()})


def _endpoints() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(typed, untyped_dict) —— 与 OpenAPI 生成同一套路由遍历。"""
    typed: set[tuple[str, str]] = set()
    untyped: set[tuple[str, str]] = set()
    for ctx in iter_route_contexts(_app().routes):
        route = ctx.route
        if not isinstance(route, APIRoute):
            continue
        model = route.response_model
        # FastAPI 会为 ``-> dict`` 推断出 ``dict`` 本身（不是 pydantic 模型）
        is_model = (
            model is not None
            and model is not dict
            and isinstance(model, type)
            and hasattr(model, "model_fields")
        )
        returns_dict = get_typed_return_annotation(route.endpoint) is dict
        for method in sorted(route.methods or ()):
            if method in ("HEAD", "OPTIONS"):
                continue
            if is_model:
                typed.add((method, ctx.path))
            elif returns_dict:
                untyped.add((method, ctx.path))
    return typed, untyped


class TestOpenAPISnapshot:
    def test_snapshot_matches_generated_document(self):
        gen = _load_gen_script()
        expected = gen.render(gen.build_openapi_document())
        assert SNAPSHOT.exists(), (
            "frontend/src/api/openapi.json is missing — run "
            "`uv run python scripts/gen_openapi.py`"
        )
        actual = SNAPSHOT.read_text(encoding="utf-8")
        assert actual == expected, (
            "OpenAPI snapshot is stale — run "
            "`uv run python scripts/gen_openapi.py` and commit the result"
        )

    def test_generation_is_deterministic(self):
        gen = _load_gen_script()
        assert gen.render(gen.build_openapi_document()) == gen.render(
            gen.build_openapi_document()
        )

    def test_snapshot_covers_the_admin_surface(self):
        """替身被误当成 NullAuth 时管理端会整块从快照里消失 —— 那会让漂移门
        "通过"一份缩水的契约。"""
        import json

        doc = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        admin_paths = [p for p in doc["paths"] if p.startswith("/v1/admin/")]
        assert len(admin_paths) >= 100


class TestEndpointTypingRatchet:
    def test_untyped_dict_set_is_frozen(self):
        _, untyped = _endpoints()
        assert untyped == UNTYPED_DICT_ENDPOINTS, (
            "untouched dict endpoints drifted. Newly typed endpoint? Remove it "
            "from UNTYPED_DICT_ENDPOINTS and add it to TYPED_ENDPOINTS. "
            "New untyped endpoint? It belongs in a response model — the frozen "
            "set is a ratchet, not a dumping ground."
        )

    def test_typed_set_is_frozen(self):
        typed, _ = _endpoints()
        assert typed == TYPED_ENDPOINTS, (
            "typed endpoints drifted — removing a response model or renaming an "
            "endpoint must be a visible decision, not a silent regression"
        )

    def test_typed_endpoints_reference_named_schemas(self):
        """模型必须真的到达 OpenAPI，而不是被 ``type: object`` 吞掉。"""
        import json

        doc = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        schemas = doc.get("components", {}).get("schemas", {})
        for method, path in sorted(TYPED_ENDPOINTS):
            operation = doc["paths"][path][method.lower()]
            ok_codes = sorted(c for c in operation["responses"] if c.startswith("2"))
            assert ok_codes, (method, path, "no 2xx response declared")
            schema = operation["responses"][ok_codes[0]]["content"]["application/json"]["schema"]
            ref = schema.get("$ref", "")
            assert ref.startswith("#/components/schemas/"), (method, path, schema)
            assert ref.rsplit("/", 1)[-1] in schemas, (method, path, ref)


def test_snapshot_path_is_inside_the_frontend_tree():
    """生成物必须落在前端 TS 能 import 的地方（tsconfig 只含 src/**）。"""
    assert SNAPSHOT == ROOT / "frontend" / "src" / "api" / "openapi.json"
    assert Path(SNAPSHOT).parent.is_dir()
