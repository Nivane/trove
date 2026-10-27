#!/usr/bin/env python3
"""生成 API 的 OpenAPI 快照 —— 前端类型的唯一来源。

`frontend/src/api/types.ts` 手写的 90 多个类型与后端载荷逐字段人肉对齐，
而 `app.openapi()` 此前从未被任何测试或产物消费（生成的
`openapi.d.ts` 烂在 31 条路径且无人使用）。这个脚本把链路接通：

    Pydantic 模型 → app.openapi() → frontend/src/api/openapi.json
                  → openapi-typescript → frontend/src/api/openapi.d.ts

`tests/architecture/test_api_contracts.py` 用同一个 `build_openapi_document()`
做漂移门：模型改了而快照没重新生成 → 红。

零网络 / 零 LLM / 零业务库：只构造 app 的路由表与 Pydantic schema。
`create_app` 在 `auth` 不是 ``NullAuth`` 时才挂管理端路由，所以这里注入一个
**最小 auth 替身**（不是真 `AuthService` —— 那会按 `TROVE_STORAGE_URL`
开一个真实存储，离线生成不该有这种外部依赖）。替身不实现任何鉴权语义，
OpenAPI 生成过程中不会有请求经过它。

    uv run python scripts/gen_openapi.py            # 写快照
    uv run python scripts/gen_openapi.py --check    # 只比对（漂移 → 退出码 1）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "frontend" / "src" / "api" / "openapi.json"

#: 管理端路由至少要有这么多条 —— 替身没被 `isinstance(NullAuth)` 认成替身时，
#: 生成结果会静默少掉整个管理面（快照变小，漂移门却"通过"了）。
_MIN_ADMIN_PATHS = 20


class _OfflineAuth:
    """仅为让 `create_app` 挂上管理端路由的 duck-type 替身。

    `create_app` 只用 ``isinstance(auth, NullAuth)`` 决定挂不挂管理端，
    再把对象交给 ``Policy(auth)``；两者都不在这条路径上调用其方法。
    真的实现方法反而是风险：将来有人在这里做请求，会拿到一个"永远放行"
    的 auth。方法一律抛错，把误用变成响亮的失败。
    """

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(
            f"offline OpenAPI generation must not call auth.{name}()"
        )


def build_openapi_document() -> dict[str, Any]:
    """构造并返回 OpenAPI 文档（与活服务同一套路由表）。"""
    from trove.api.app import create_app

    app = create_app({"auth": _OfflineAuth()})
    doc = app.openapi()
    paths = doc.get("paths") or {}
    admin_paths = [p for p in paths if p.startswith("/v1/admin/")]
    if len(admin_paths) < _MIN_ADMIN_PATHS:
        raise RuntimeError(
            f"only {len(admin_paths)} admin paths in the generated document; "
            "the offline auth stand-in was probably rejected by create_app"
        )
    return doc


def render(doc: dict[str, Any]) -> str:
    """规范化序列化 —— 排序键 + 2 空格缩进 + 末尾换行。

    JSON 的键序不是语义，但快照是逐字节比对的产物：不排序就会把
    FastAPI/机器差异变成假漂移。"""
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="只比对已提交的快照，不写文件（漂移 → 退出码 1）",
    )
    args = parser.parse_args(argv)

    rendered = render(build_openapi_document())
    if args.check:
        current = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.exists() else ""
        if current != rendered:
            print(
                f"OpenAPI snapshot is stale: {SNAPSHOT.relative_to(ROOT)}\n"
                "Run `uv run python scripts/gen_openapi.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("OpenAPI snapshot is up to date.")
        return 0

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(rendered, encoding="utf-8")
    print(f"Wrote {SNAPSHOT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
