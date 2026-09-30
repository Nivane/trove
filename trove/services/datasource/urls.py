"""Datasource URL parsing — CLI --datasource scheme:// forms.

Supported schemes:
  mysql://[user[:password]@]host[:port]/database      (default port 3306)
  doris://[user[:password]@]host[:port]/database     (default port 9030)
  postgres://[user[:password]@]host[:port]/database  (default port 5432)
  postgresql://...                                    (同上,别名)
  clickhouse://[user[:password]@]host[:port]/database (default port 8123)
  sqlite://path/to/file.db | sqlite://:memory:
  duckdb://path/to/file.duckdb | duckdb://:memory:
"""

from __future__ import annotations

from urllib.parse import quote, unquote, urlparse

from trove.core.types import DatasourceConfig
from trove.core.errors import DatasourceError

DEFAULT_PORTS = {
    "mysql": 3306,
    "doris": 9030,
    "postgres": 5432,
    "clickhouse": 8123,
}

FILE_SCHEMES = ("sqlite", "duckdb")

# 同一个东西的两个拼法:``postgresql`` 是 libpq/psycopg 的标准 URI 拼法
# (任何一份 PG 连接串文档、本仓 CI 与集成测试的 PG_TEST_URL 都用它),
# ``postgres`` 是短写。规范名是 ``postgres`` —— 别名在这里归一,不让
# 下游到处判两个值。
SCHEME_ALIASES = {"postgresql": "postgres"}


def parse_datasource_url(url: str) -> DatasourceConfig:
    """Parse a scheme:// URL into a DatasourceConfig.

    Raises:
        DatasourceError: Unknown scheme, missing database, or invalid port.
    """
    parsed = urlparse(url)
    scheme = SCHEME_ALIASES.get(parsed.scheme.lower(), parsed.scheme.lower())

    if scheme in DEFAULT_PORTS:
        if not parsed.hostname:
            raise DatasourceError(
                message=f"Invalid {scheme} URL (missing host): {url}",
                datasource="",
            )
        database = parsed.path.lstrip("/")
        if not database:
            raise DatasourceError(
                message=f"Invalid {scheme} URL (missing database name): {url}",
                datasource="",
            )
        try:
            port = parsed.port or DEFAULT_PORTS[scheme]
        except ValueError as e:
            raise DatasourceError(
                message=f"Invalid {scheme} URL (bad port): {url}",
                datasource="",
            ) from e
        cfg = DatasourceConfig(
            name=database,
            type=scheme,
            connection_params={
                "host": parsed.hostname,
                "port": port,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "database": database,
            },
            default=True,
        )
        # 默认向量后端:postgres 业务库 → pgvector(同实例,dsn 运行时推导);
        # 其它业务库无 pgvector 依托 → sqlite 本地向量(与 DatasourceConfig
        # 的全局默认解耦,保证持久化的配置准确)。
        if scheme == "postgres":
            cfg.vector_backend = "pgvector"
        else:
            cfg.vector_backend = "sqlite"
        return cfg

    if scheme in FILE_SCHEMES:
        # sqlite:///abs/path → netloc "" + path "/abs/path"
        # sqlite://:memory:  → netloc ":memory:" + path ""
        path = parsed.netloc + parsed.path if parsed.netloc else parsed.path
        if not path:
            raise DatasourceError(
                message=f"Invalid {scheme} URL (missing path): {url}",
                datasource="",
            )
        cfg = DatasourceConfig(
            name=scheme,
            type=scheme,
            connection_params={"path": path},
            default=True,
        )
        cfg.vector_backend = "sqlite"
        return cfg

    raise DatasourceError(
        message=f"Unsupported datasource scheme '{scheme}' in: {url}",
        datasource="",
    )


def build_url(cfg: DatasourceConfig) -> str:
    """Reverse of :func:`parse_datasource_url` — reconstruct a scheme:// URL.

    Powers the admin edit dialog (prefill) from the persisted config. The
    round-trip is lossless: user/password are URL-encoded, credentials
    (stored separately in datasources.yml) are merged before building.
    """
    params = {**cfg.connection_params, **cfg.credentials}
    if cfg.type == "demo":
        return "demo"
    if cfg.type in DEFAULT_PORTS:
        host = params.get("host", "")
        port = params.get("port") or DEFAULT_PORTS[cfg.type]
        user = quote(params.get("user", ""), safe="")
        password = quote(params.get("password", ""), safe="")
        if user and password:
            auth = f"{user}:{password}@"
        elif user:
            auth = f"{user}@"
        elif password:
            auth = f":{password}@"
        else:
            auth = ""
        database = params.get("database", "")
        return f"{cfg.type}://{auth}{host}:{port}/{database}"
    if cfg.type in FILE_SCHEMES:
        path = params.get("path", "")
        if not path:
            raise DatasourceError(
                message=f"cannot build URL for {cfg.type} datasource: missing path",
                datasource=cfg.name,
            )
        # 绝对路径 .db → sqlite:///abs.db;:memory: → sqlite://:memory:
        return f"{cfg.type}://{path}"
    raise DatasourceError(
        message=f"cannot build URL for unsupported type {cfg.type!r}",
        datasource=cfg.name,
    )
