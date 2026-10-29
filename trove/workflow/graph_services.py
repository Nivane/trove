"""Service bundle + shared authz/egress helpers for the graph builders.

``GraphServices`` is bound into node closures at graph build time and is used
by both ``graphs.py`` (topology) and ``gen_chain.py`` (generation); it lives
below both so the import direction stays acyclic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trove.core.config import AgentConfig
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.kb.service import KbService


@dataclass
class GraphServices:
    """Service bundle bound into node closures at graph build time."""

    llm: LLMGateway
    catalog: CatalogService | None = None
    connectors: ConnectorRegistry | None = None
    config: AgentConfig | None = None
    kb: KbService | None = None  # optional knowledge base enhancement
    user_facts: Any | None = None  # optional user-level memory (per-user facts)
    semantic_layer: Any | None = None  # optional live semantic provider (OSSIE)
    lineage: Any | None = None  # optional data lineage service (metadata + execute recording)
    memory: Any | None = None  # optional unified memory facade (episodes/prefs/profile)
    skills: Any | None = None  # optional org skill assets (methodology, admin-managed)
    # optional LLM-egress redactor (services/authz/egress.EgressRedactor);
    # None → built lazily from semantic_layer + config by ``_egress_redactor``.
    egress: Any | None = None


def _egress_redactor(services: GraphServices):
    """GraphServices → 出境脱敏口(缺失时按语义层 + 活配置构造一次)。

    每轮注册表/每次装配共享同一个 redactor;它本身无状态,配置经 callable
    现读(管理端热改 ``egress`` 在下一问生效)。
    """
    redactor = services.egress
    if redactor is None:
        from trove.services.authz.egress import EgressRedactor

        redactor = EgressRedactor(
            semantic_provider=services.semantic_layer,
            config=lambda: services.config or AgentConfig(),
        )
        services.egress = redactor
    return redactor


def _build_authorizer(services: "GraphServices"):
    """执行层强制点(设计 §5.3 / G2)。``None`` = 未装配,不判。

    构造本体在 ``authz.enforcer.build_authorizer``:直执行路由
    (``/v1/semantic/query``)用同一份工厂,两处口径(A3 基准 / warn 兜底)
    因此只有一处实现。
    """
    from trove.services.authz.enforcer import build_authorizer

    return build_authorizer(
        services.config or AgentConfig(), services.semantic_layer)
