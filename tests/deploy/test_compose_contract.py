"""compose 契约测试:首次上手的承诺必须能从文件本身验证。

零 Docker daemon、零网络:只解析 `docker-compose.yml` 与随仓库 KB——
`docker compose up --build` 之后的「第一问不被拒」依赖的正是这两者的名字对得上。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"


def _compose_text() -> str:
    return COMPOSE_PATH.read_text(encoding="utf-8")


def _compose() -> dict:
    return yaml.safe_load(_compose_text())


def _find_postgres_url(command: list[str]) -> str:
    for part in command:
        if isinstance(part, str) and part.startswith(("postgres://", "postgresql://")):
            return part
    raise AssertionError(f"command 里没有 postgres URL: {command!r}")


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def test_compose_default_datasource_has_repo_kb() -> None:
    """后端连的库名必须与随仓库的 KB 目录同名(ONB-1)。"""
    backend = _compose()["services"]["backend"]
    db = _db_name(_find_postgres_url(backend["command"]))
    assert (ROOT / ".trove" / "kb" / db / "semantics.yml").is_file(), (
        f"compose 默认数据源 {db!r} 没有随仓库的语义模型;首问会被「未建模」拒绝"
    )


def test_compose_db_name_is_consistent_across_services() -> None:
    """POSTGRES_DB / db-init / backend / healthcheck 说的是同一个库(ONB-1)。"""
    services = _compose()["services"]
    postgres_db = services["postgres"]["environment"]["POSTGRES_DB"]
    init_db = _db_name(_find_postgres_url(services["db-init"]["command"]))
    backend_db = _db_name(_find_postgres_url(services["backend"]["command"]))
    healthcheck = " ".join(services["postgres"]["healthcheck"]["test"])
    assert f"-d {postgres_db}" in healthcheck, healthcheck
    assert postgres_db == init_db == backend_db, (
        postgres_db,
        init_db,
        backend_db,
    )


def test_credential_mount_guidance_uncomments_to_valid_yaml() -> None:
    """文档让你「取消注释挂载 conf」——取消后必须是合法 YAML(ONB-2)。

    挂载项若写在 `environment:` 下,取消注释会把 mapping 与 list 混写,
    `docker compose config` 直接报错。这里模拟用户取消注释,再整体解析。
    """
    lines = _compose_text().splitlines()
    hits = [i for i, line in enumerate(lines) if "/root/.trove/conf" in line]
    assert hits, "docker-compose.yml 必须保留凭证挂载指引"

    patched: list[str] = []
    for i, line in enumerate(lines):
        if i not in hits:
            patched.append(line)
            continue
        stripped = line.strip()
        assert stripped.startswith("#"), f"挂载指引应是注释行: {line!r}"
        indent = line[: len(line) - len(line.lstrip())]
        patched.append(indent + stripped[1:].lstrip())

    data = yaml.safe_load("\n".join(patched))
    volumes = data["services"]["backend"]["volumes"]
    assert any(".trove/conf:/root/.trove/conf:ro" in str(v) for v in volumes), volumes


def test_root_env_is_declared_explicitly() -> None:
    """根 `.env` 不会自己进容器;compose 必须显式声明且缺失时跳过(ONB-3)。"""
    backend = _compose()["services"]["backend"]
    env_files = backend.get("env_file") or []
    assert env_files, "backend 需要显式 env_file 声明,否则根 .env 的承诺不成立"

    normalized = []
    for entry in env_files:
        if isinstance(entry, str):
            normalized.append((entry, True))
        else:
            normalized.append((entry.get("path"), entry.get("required", True)))
    assert (".env", False) in normalized, (
        f"根 .env 应按 required: false 声明(缺失时 compose 仍可起): {normalized!r}"
    )
