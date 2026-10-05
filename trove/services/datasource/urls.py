"""Datasource URL parsing — CLI --datasource scheme:// forms.

Supported schemes:
  mysql://[user[:password]@]host[:port]/database      (default port 3306)
  doris://[user[:password]@]host[:port]/database     (default port 9030)
  postgres://[user[:password]@]host[:port]/database  (default port 5432)
  postgresql://...                                    (同上,别名)
  clickhouse://[user[:password]@]host[:port]/database (default port 8123)
  snowflake://[user[:password]@]account/database/schema?warehouse=…&role=…&private_key_file=…
  bigquery://<project>/<dataset>?location=US[&service_account_file=/path/key.json]
  sqlite://path/to/file.db | sqlite://:memory:
  duckdb://path/to/file.duckdb | duckdb://:memory:
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from trove.core.types import DatasourceConfig
from trove.core.errors import DatasourceError

DEFAULT_PORTS = {
    "mysql": 3306,
    "doris": 9030,
    "postgres": 5432,
    "clickhouse": 8123,
}

FILE_SCHEMES = ("sqlite", "duckdb")

#: 云仓(账号/项目 + 库/数据集,没有 host[:port])。**刻意不进 ``DEFAULT_PORTS``**:
#: 那张表是「host 形状」的注册表,进去就会让上面那条解析路径接住一个它读不懂
#: 的连接串(netloc 不是主机名、path 不是单段库名),错在更晚的地方。
#: 两种云仓的 path 形状不同(雪花两段、BigQuery 一段),各自的切分见
#: ``_cloud_url_parts`` / ``_bigquery_url_parts``。
CLOUD_SCHEMES = ("snowflake", "bigquery")

# 同一个东西的两个拼法:``postgresql`` 是 libpq/psycopg 的标准 URI 拼法
# (任何一份 PG 连接串文档、本仓 CI 与集成测试的 PG_TEST_URL 都用它),
# ``postgres`` 是短写。规范名是 ``postgres`` —— 别名在这里归一,不让
# 下游到处判两个值。
SCHEME_ALIASES = {"postgresql": "postgres"}


def _cloud_url_parts(parsed: Any, scheme: str, url: str) -> dict[str, Any]:
    """雪花形状的云仓切分:账号(netloc)+ ``/<database>/<schema>`` + query 参数。

    与 host 形状的两处不同,都是刻意的:

    * 主机位是**账号标识**,而 ``urlparse().hostname`` 会把 netloc 折成小写
      (账号名会就此变脸)—— 账号从**原始 netloc** 取(剥 userinfo、只做
      百分号解码),大小写原样保留;
    * path 是**两段**:这个数据源的身份 = 一个模式(库只是它所在的位置),
      少一段就不知道查哪儿 —— 与 MySQL 的「一段 = 库」同级,只是多一层。

    warehouse / role / private_key_file 走 query string,不占 path:
    它们都有服务端/驱动缺省,不是连接身份的组成部分。

    BigQuery 的另一种云仓形状见 ``_bigquery_url_parts``(一段 path、
    不同的 query 键 —— 两家的身份结构真的不同,没有可共用的切分)。
    """
    segments = parsed.path.strip("/").split("/")
    if len(segments) != 2 or not all(segments):
        raise DatasourceError(
            message=f"Invalid {scheme} URL (expected /<database>/<schema>): {url}",
            datasource="",
        )
    database, schema = segments
    account = unquote(parsed.netloc.rpartition("@")[2])
    if not account:
        raise DatasourceError(
            message=f"Invalid {scheme} URL (missing account): {url}",
            datasource="",
        )
    params: dict[str, Any] = {
        "account": account,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": database,
        "schema": schema,
    }
    query = parse_qs(parsed.query)
    for key in ("warehouse", "role", "private_key_file"):
        value = query.get(key, [""])[0]
        if value:
            params[key] = value
    return params


def _bigquery_url_parts(parsed: Any, scheme: str, url: str) -> dict[str, Any]:
    """BigQuery 形状的云仓切分:项目(netloc)+ ``/<dataset>`` + query 参数。

    与雪花形状的两处不同:

    * 身份 = **一个 dataset**(项目只是它所在的位置)—— path 一段;
    * 凭据**不是** user/password:BigQuery 只认 service account 文件或
      ADC,所以带 userinfo 的连接串被**拒绝**而不是静默丢掉 —— 一组无效的
      凭据出现在连接串里必须响亮地失败,不能装作读进去了。

    location / service_account_file 走 query string:项目与 dataset 各有
    自己的默认 location(不是连接身份的组成部分),file 留空 = 走 ADC。
    """
    if parsed.username or parsed.password:
        raise DatasourceError(
            message=f"Invalid {scheme} URL (user:password is not a BigQuery "
                    f"auth mechanism — use ?service_account_file=… or ADC): {url}",
            datasource="",
        )
    segments = parsed.path.strip("/").split("/")
    if len(segments) != 1 or not segments[0]:
        raise DatasourceError(
            message=f"Invalid {scheme} URL (expected /<dataset>): {url}",
            datasource="",
        )
    # 项目从**原始 netloc** 取(``.hostname`` 会折小写)—— 与雪花取账号
    # 同一条:GCP 项目 ID 本就限制小写,但"上游约定"不该是唯一防线。
    project = unquote(parsed.netloc.rpartition("@")[2])
    if not project:
        raise DatasourceError(
            message=f"Invalid {scheme} URL (missing project): {url}",
            datasource="",
        )
    params: dict[str, Any] = {"project": project, "dataset": segments[0]}
    query = parse_qs(parsed.query)
    for key in ("location", "service_account_file"):
        value = query.get(key, [""])[0]
        if value:
            params[key] = value
    return params


def _cloud_datasource_name(scheme: str, params: dict[str, Any]) -> str:
    """云仓数据源名 = 身份的两段。

    同一个库里两个模式(数据集)是两个可查单元,只拿库/项目名当名字会让
    第二个注册撞上「已存在」(管理端按名字去重)。
    """
    if scheme == "bigquery":
        return f"{params['project']}.{params['dataset']}"
    return f"{params['database']}.{params['schema']}"


def _build_cloud_url(cfg: DatasourceConfig, params: dict[str, Any]) -> str:
    """:func:`build_url` 的雪花分支 —— 与 ``_cloud_url_parts`` 严格互逆。"""
    # 账号照原样的大小写回写(解析侧同 —— 折小写会让账号名变脸),只做百分号
    # 编码兜住非 URL 安全字符(如 ``@``):不编码的话它会被当成 userinfo 分隔符,
    # 再解析回来就只剩半截账号。
    account = quote(params.get("account", ""), safe="")
    database = params.get("database", "")
    schema = params.get("schema", "")
    if not account or not database or not schema:
        raise DatasourceError(
            message=f"cannot build URL for {cfg.type} datasource: "
                    f"missing account/database/schema",
            datasource=cfg.name,
        )
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
    # 固定参数顺序:round-trip 才是**逐字节**稳定(编辑对话框预填的场景:
    # 原样回显,不因一次往返就换个拼法)。``safe="/"``:路径分隔符在 query 里
    # 合法,不编码 —— 编码了手写的 ``private_key_file=/keys/x.p8`` 回显时会
    # 变成 ``%2Fkeys%2Fx.p8``,同一个意思换了个拼法。
    query = "&".join(
        f"{key}={quote(str(params[key]), safe='/')}"
        for key in ("warehouse", "role", "private_key_file")
        if params.get(key)
    )
    suffix = f"?{query}" if query else ""
    return f"{cfg.type}://{auth}{account}/{database}/{schema}{suffix}"


def _build_bigquery_url(cfg: DatasourceConfig, params: dict[str, Any]) -> str:
    """:func:`build_url` 的 BigQuery 分支 —— 与 ``_bigquery_url_parts`` 严格互逆。"""
    project = quote(params.get("project", ""), safe="")
    dataset = params.get("dataset", "")
    if not project or not dataset:
        raise DatasourceError(
            message=f"cannot build URL for {cfg.type} datasource: "
                    f"missing project/dataset",
            datasource=cfg.name,
        )
    # 固定参数顺序 + ``safe="/"``:与雪花分支同一条理由 —— round-trip 逐字节
    # 稳定(编辑对话框预填时原样回显,路径分隔符不编码)。
    query = "&".join(
        f"{key}={quote(str(params[key]), safe='/')}"
        for key in ("location", "service_account_file")
        if params.get(key)
    )
    suffix = f"?{query}" if query else ""
    return f"{cfg.type}://{project}/{dataset}{suffix}"


#: 云仓 scheme → 各自的切分 / 构造函数。加一种云仓 = 两张表各加一行 +
#: 上面两个函数各写一个(身份结构不同的东西,共用切分只会让两边都变形)。
_CLOUD_PARSERS = {
    "snowflake": _cloud_url_parts,
    "bigquery": _bigquery_url_parts,
}

_CLOUD_BUILDERS = {
    "snowflake": _build_cloud_url,
    "bigquery": _build_bigquery_url,
}


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

    if scheme in CLOUD_SCHEMES:
        params = _CLOUD_PARSERS[scheme](parsed, scheme, url)
        cfg = DatasourceConfig(
            name=_cloud_datasource_name(scheme, params),
            type=scheme,
            connection_params=params,
            default=True,
        )
        cfg.vector_backend = "sqlite"   # 云仓旁挂不了 pgvector,向量回本地
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
    if cfg.type in CLOUD_SCHEMES:
        return _CLOUD_BUILDERS[cfg.type](cfg, params)
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
