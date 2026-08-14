"""Datasource URL parsing — CLI --datasource scheme:// forms.

Supported schemes:
  mysql://[user[:password]@]host[:port]/database      (default port 3306)
  clickhouse://[user[:password]@]host[:port]/database (default port 8123)
  sqlite://path/to/file.db | sqlite://:memory:
  duckdb://path/to/file.duckdb | duckdb://:memory:
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse

from trove.core.types import DatasourceConfig
from trove.core.errors import DatasourceError

DEFAULT_PORTS = {"mysql": 3306, "clickhouse": 8123}

FILE_SCHEMES = ("sqlite", "duckdb")


def parse_datasource_url(url: str) -> DatasourceConfig:
    """Parse a scheme:// URL into a DatasourceConfig.

    Raises:
        DatasourceError: Unknown scheme, missing database, or invalid port.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

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
        return DatasourceConfig(
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

    if scheme in FILE_SCHEMES:
        # sqlite:///abs/path → netloc "" + path "/abs/path"
        # sqlite://:memory:  → netloc ":memory:" + path ""
        path = parsed.netloc + parsed.path if parsed.netloc else parsed.path
        if not path:
            raise DatasourceError(
                message=f"Invalid {scheme} URL (missing path): {url}",
                datasource="",
            )
        return DatasourceConfig(
            name=scheme,
            type=scheme,
            connection_params={"path": path},
            default=True,
        )

    raise DatasourceError(
        message=f"Unsupported datasource scheme '{scheme}' in: {url}",
        datasource="",
    )
