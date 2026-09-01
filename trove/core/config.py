"""Configuration loading and management.

Loads agent.yml (global config with credentials) and
.trove/config.yml (project-level config with whitelisted keys).
Resolves ${ENV_VAR} placeholders at load time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from trove.core.errors import ConfigError
from trove.core.logging import get_logger
from trove.services.memory.models import MemoryConfig

logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────

ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")

# Keys allowed in project-level .trove/config.yml
PROJECT_CONFIG_WHITELIST = {
    "target",
    "default_datasource",
    "project_name",
    "scheduler",
}

# Config file search order
CONFIG_SEARCH_PATHS = [
    "./conf/agent.yml",
    "~/.trove/conf/agent.yml",
]


# ── Config Dataclasses ───────────────────────────────────


@dataclass
class ProviderConfig:
    """LLM Provider configuration."""

    name: str
    litellm_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class DatasourceServiceConfig:
    """Datasource service config (from agent.yml)."""

    name: str
    type: str
    connection: dict[str, Any] = field(default_factory=dict)


@dataclass
class TracingConfig:
    """Observability / tracing configuration."""

    enabled: bool = False
    providers: list[dict[str, Any]] = field(default_factory=list)
    capture: dict[str, bool] = field(default_factory=dict)


@dataclass
class RetentionConfig:
    """会话保留策略(配额清理)。0 = 关闭对应机制。"""

    max_sessions_per_user: int = 100  # 每用户会话数配额;0 = 关闭配额清理
    active_grace_min: int = 10  # 最近有更新的会话豁免窗口(分钟)
    max_checkpoints_per_thread: int = 50  # 单线程 checkpoint 深度上限
    sweep_interval_hours: int = 24  # 周期清理间隔;<=0 = 关闭周期清理


@dataclass
class AgentConfig:
    """Top-level agent configuration."""

    home: str = "~/.trove"
    target: str = ""  # default model e.g. "openai/gpt-4o"
    model_fast: str = ""  # 快速档模型: simple/standard 复杂度走此模型(未配置 = 不分档,全走 target)
    # 每节点模型覆盖(按节点名,如 query_sketch/gen_sql/reflect/insights):优先于复杂度
    # 分档选模——query_sketch 用便宜模型、reflect 用强模型的典型配置。空 = 不覆盖。
    node_models: dict[str, str] = field(default_factory=dict)
    language: str = "zh"  # 交互语言: zh / en(不按问题语言自动检测)
    semantic_layer_path: str = ""  # OSSIE 语义层目录(相对项目根),空 = 关闭
    # 语义优先(Phase B):语义模型是唯一可答边界——未覆盖=拒绝+反问扩展;
    # 无模型=拒绝并提示 /kb init(决策 2/3)。旧裸表路径已从查询图删除。
    semantic_first: bool = True
    date_parser: bool = True  # 时间解析节点:确定性规则解析相对时间(zh/en),未命中静默透传
    fast_path: bool = True  # 确定性模板快径:命中即跳过 query_sketch/生成/裁决
    reflect_skip: str = "simple"  # 规则全过后跳 LLM 裁决档位: simple / standard / all / off
    explain_semantics: bool = False  # 生成 SQL 后 LLM 说明语义(执行前展示给用户)
    hitl: bool = False  # 执行前人工确认(LangGraph interrupt; 需 checkpointer)
    insights: bool = False  # 执行后 LLM 基于结果生成洞察
    conclusion: bool = False  # 执行后 LLM 用一句话生成结论摘要(置于回答开头)
    result_cache: bool = False  # 精确问题结果缓存(进程内存;命中直接返回已验证答案,跳过 HITL 确认)
    decompose_llm_judge: bool = True  # 多任务拆解 LLM 判断层:规则未命中但疑似多步时花一次 LLM 判断;false = 纯正则门控
    # 结果限制(管理台可配):答案表格单次展示行数 / 查询结果行数上限
    result_display_rows: int = 50
    result_max_rows: int = 1000
    # EXPLAIN 行数估算守卫(默认关,开则每次最终执行前 EXPLAIN 估算最重算子
    # 行数,超 explain_max_rows 打回 gen_sql 加 LIMIT/收窄。fail-open:无法
    # 解析方言/EXPLAIN 失败 → 放行)。见 trove/services/sql/row_guard.py。
    explain_row_guard: bool = False
    explain_max_rows: int = 50_000_000
    # Prompt caching:对支持显式断点的 provider(anthropic)在 system / 稳定前缀
    # / 工具定义上打 cache_control 断点;OpenAI 系自动缓存无需断点,其他
    # provider 由 gateway 剥掉断点(行为等价,只是没有缓存收益)。默认开——
    # 纯收益开关,provider 不支持时自动 no-op。
    prompt_caching: bool = True
    # 上下文预算覆盖(可空 = 用内置默认):gen prompt 可选块(few-shot/术语/
    # 教训/计划/历史)与 schema 段的 token 上限,按复杂度分档 simple/standard/
    # complex。配置时整体覆盖对应档位,未配置项回落内置默认。示例:
    #   context_budget_tokens: {simple: 1500, standard: 2500, complex: 8000}
    #   schema_budget_tokens:   {simple: 900,  standard: 1800, complex: 6000}
    # 200k/1m 窗口下可按模型窗口比例放大,而不是写死固定档位。
    context_budget_tokens: dict[str, int] = field(default_factory=dict)
    schema_budget_tokens: dict[str, int] = field(default_factory=dict)
    # 记忆子系统配置(统一 memory facade):情景记忆/自动示例/偏好提取/
    # 自动晋升/画像注入等渐进开关。见 trove.services.memory.models.MemoryConfig。
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    config_mutable: bool = True
    providers: list[ProviderConfig] = field(default_factory=list)
    datasources: list[DatasourceServiceConfig] = field(default_factory=list)
    tracing: TracingConfig = field(default_factory=TracingConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    def model_for(self, complexity: str) -> str:
        """复杂度分档选模: simple/standard → model_fast(未配置 → target);complex 及未知 → target。

        复杂度分级(grade_complexity)驱动的降本开关:简单查询不需要推理模型。
        """
        if self.model_fast and complexity in ("simple", "standard"):
            return self.model_fast
        return self.target or "openai/gpt-4o"

    def model_for_node(self, node: str, complexity: str) -> str:
        """每节点模型覆盖(优先) → 复杂度分档选模。

        ``node_models`` 里给某节点(query_sketch/gen_sql/reflect/insights/...)配了
        模型就用它——query_sketch 便宜、reflect 强模型的典型配置;没配则回落
        ``model_for`` 的复杂度分档。
        """
        if node:
            pinned = self.node_models.get(node) or self.node_models.get(node.lower())
            if pinned:
                return pinned
        return self.model_for(complexity)


@dataclass
class ProjectConfig:
    """Project-level configuration (whitelisted keys only)."""

    target: str = ""
    default_datasource: str = ""
    project_name: str = ""
    scheduler: str = ""


# ── ConfigLoader ─────────────────────────────────────────


class ConfigLoader:
    """Loads and resolves agent config from file hierarchy."""

    @staticmethod
    def resolve_env_vars(value: str) -> str:
        """Replace ${ENV_VAR} patterns with environment variable values.

        Args:
            value: String potentially containing ${VAR} placeholders.

        Returns:
            String with placeholders replaced by env var values.
        """
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, "")

        return ENV_VAR_PATTERN.sub(_replace, value)

    @staticmethod
    def resolve_env_vars_in_dict(data: dict[str, Any]) -> dict[str, Any]:
        """Recursively resolve ${ENV_VAR} in all string values of a dict."""
        resolved = {}
        for key, value in data.items():
            if isinstance(value, str):
                resolved[key] = ConfigLoader.resolve_env_vars(value)
            elif isinstance(value, dict):
                resolved[key] = ConfigLoader.resolve_env_vars_in_dict(value)
            elif isinstance(value, list):
                resolved[key] = [
                    ConfigLoader.resolve_env_vars_in_dict(v) if isinstance(v, dict)
                    else ConfigLoader.resolve_env_vars(v) if isinstance(v, str)
                    else v
                    for v in value
                ]
            else:
                resolved[key] = value
        return resolved

    @classmethod
    def find_config_file(cls, explicit_path: str | None = None) -> Path | None:
        """Find agent.yml using the config search order.

        Args:
            explicit_path: If provided, only look at this path.

        Returns:
            Path to the config file, or None if not found.
        """
        if explicit_path:
            p = Path(explicit_path).expanduser().resolve()
            return p if p.exists() else None

        for search_path in CONFIG_SEARCH_PATHS:
            p = Path(search_path).expanduser().resolve()
            if p.exists():
                return p

        return None

    @classmethod
    def load_agent_config(
        cls,
        config_path: str | None = None,
    ) -> AgentConfig:
        """Load agent.yml and return a resolved AgentConfig.

        Args:
            config_path: Explicit path to agent.yml. If None, search order applies.

        Returns:
            Resolved AgentConfig with env vars substituted.

        Raises:
            ConfigError: If no config file is found or parsing fails.
        """
        path = cls.find_config_file(config_path)
        if path is None:
            logger.warning("No agent.yml found; using empty config")
            return AgentConfig()

        try:
            raw_text = path.read_text(encoding="utf-8")
            raw_data = yaml.safe_load(raw_text) or {}
        except (yaml.YAMLError, OSError) as e:
            raise ConfigError(
                message=f"Failed to parse {path}: {e}",
                details={"path": str(path)},
            ) from e

        # Resolve ${ENV_VAR} throughout
        resolved = cls.resolve_env_vars_in_dict(raw_data)

        agent_section = resolved.get("agent", resolved)

        # Parse providers
        providers = []
        for p in agent_section.get("providers", []):
            providers.append(ProviderConfig(
                name=p.get("name", ""),
                litellm_params=p.get("litellm_params", {}),
            ))

        # Parse datasources from services
        services = agent_section.get("services", {})
        datasources = []
        for ds in services.get("datasources", []):
            datasources.append(DatasourceServiceConfig(
                name=ds.get("name", ""),
                type=ds.get("type", ""),
                connection=ds.get("connection", {}),
            ))

        # Parse tracing
        obs = agent_section.get("observability", {})
        tracing_raw = obs.get("tracing", {})
        tracing = TracingConfig(
            enabled=tracing_raw.get("enabled", False),
            providers=tracing_raw.get("providers", []),
            capture=obs.get("capture", {}),
        )

        # Parse retention
        retention_raw = agent_section.get("retention", {})
        retention = RetentionConfig(
            max_sessions_per_user=int(retention_raw.get("max_sessions_per_user", 100)),
            active_grace_min=int(retention_raw.get("active_grace_min", 10)),
            max_checkpoints_per_thread=int(retention_raw.get("max_checkpoints_per_thread", 50)),
            sweep_interval_hours=int(retention_raw.get("sweep_interval_hours", 24)),
        )

        # Parse memory subsystem
        mem_raw = agent_section.get("memory", {}) or {}
        mem_retention_raw = mem_raw.get("retention_days", {}) or {}
        retention_days: dict[str, int | None] = {}
        for k in ("episodes", "preferences", "facts", "retrieval_log", "lessons"):
            if k in mem_retention_raw and mem_retention_raw[k] is not None:
                try:
                    retention_days[k] = int(mem_retention_raw[k])
                except (TypeError, ValueError):
                    retention_days[k] = None
        memory = MemoryConfig(
            enabled=mem_raw.get("enabled", True),
            episodes=mem_raw.get("episodes", True),
            auto_examples=mem_raw.get("auto_examples", True),
            auto_preferences=mem_raw.get("auto_preferences", True),
            promotion=mem_raw.get("promotion", False),
            promotion_threshold=float(mem_raw.get("promotion_threshold", 0.8)),
            profile_boost=mem_raw.get("profile_boost", False),
            schema_drift_check=mem_raw.get("schema_drift_check", True),
            retention_days=retention_days,
        )

        return AgentConfig(
            home=agent_section.get("home", "~/.trove"),
            target=agent_section.get("target", ""),
            model_fast=agent_section.get("model_fast", ""),
            node_models={
                str(k).lower(): str(v)
                for k, v in (agent_section.get("node_models", {}) or {}).items()
                if str(k) and str(v)
            },
            language=agent_section.get("language", "zh"),
            semantic_layer_path=agent_section.get("semantic_layer_path", ""),
            semantic_first=agent_section.get("semantic_first", True),
            date_parser=agent_section.get("date_parser", True),
            fast_path=agent_section.get("fast_path", True),
            reflect_skip=agent_section.get("reflect_skip", "simple"),
            explain_semantics=agent_section.get("explain_semantics", False),
            hitl=agent_section.get("hitl", False),
            insights=agent_section.get("insights", False),
            conclusion=agent_section.get("conclusion", False),
            result_cache=agent_section.get("result_cache", False),
            result_display_rows=max(1, min(500, int(agent_section.get("result_display_rows", 50)))),
            result_max_rows=max(1, min(50000, int(agent_section.get("result_max_rows", 1000)))),
            explain_row_guard=bool(agent_section.get("explain_row_guard", False)),
            explain_max_rows=max(
                1000, int(agent_section.get("explain_max_rows", 50_000_000))),
            prompt_caching=agent_section.get("prompt_caching", True),
            context_budget_tokens={
                str(k): max(0, int(v))
                for k, v in (agent_section.get("context_budget_tokens", {}) or {}).items()
            },
            schema_budget_tokens={
                str(k): max(0, int(v))
                for k, v in (agent_section.get("schema_budget_tokens", {}) or {}).items()
            },
            memory=memory,
            config_mutable=agent_section.get("config_mutable", True),
            providers=providers,
            datasources=datasources,
            tracing=tracing,
            retention=retention,
            raw=resolved,
        )

    @classmethod
    def load_project_config(cls, project_root: str | Path = ".") -> ProjectConfig:
        """Load .trove/config.yml with whitelist enforcement.

        Only whitelisted keys (PROJECT_CONFIG_WHITELIST) are read.
        Any non-whitelisted keys are silently dropped.

        Args:
            project_root: Path to the project directory.

        Returns:
            ProjectConfig with only whitelisted keys populated.
        """
        config_file = Path(project_root) / ".trove" / "config.yml"
        if not config_file.exists():
            return ProjectConfig()

        try:
            raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.warning("Failed to read %s: %s", config_file, e)
            return ProjectConfig()

        # Only accept whitelisted keys
        filtered = {k: v for k, v in raw.items() if k in PROJECT_CONFIG_WHITELIST}
        return ProjectConfig(
            target=str(filtered.get("target", "")),
            default_datasource=str(filtered.get("default_datasource", "")),
            project_name=str(filtered.get("project_name", "")),
            scheduler=str(filtered.get("scheduler", "")),
        )
