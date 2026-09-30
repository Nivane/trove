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

    # 缺省 True 的含义是**不抑制**,不是「强制开」:真正决定录不录的是 .env 里的
    # LANGFUSE_* 凭证(没人会误配),这个键的职责是**撤回** —— enabled: false 时
    # 即便凭证在也不录(不想把问句文本送出本机时,配置上必须真的拦得住)。
    #
    # 这个缺省必须与全局闸门 observability._suppressed 的缺省一致,否则「配置文件
    # 里没写 observability 段」会变成静默停录 —— 观测系统最坏的失败形态就是不报错
    # 地什么都不记。装了包只用 ~/.trove/conf/agent.yml 的用户正好落在这一档
    # (搜索顺序见 CONFIG_SEARCH_PATHS)。
    enabled: bool = True
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
class AuthzConfig:
    """执行前授权门(设计 §7.2)。

    ``table_enforcement``: 表级判定(A3)的档位。

    - ``warn``(默认)—— 命中声明之外的表只记日志/metric,放行。设计 §8.2
      选它的理由:存量 grants 只到数据源级,直接 enforce 会让**正常查询大面积
      403**,把一次安全改进做成事故。先跑一周收集「哪些表会被拒」。
    - ``enforce`` —— 拒绝。

    数据源级判定(A2)与「必须有主体」(A1)**不受这个开关影响**,恒为拒绝。
    读错这个开关只是让 A3 变宽,不会关掉鉴权。

    ``require_principal``: 执行路径缺主体时是否拒绝。**默认 true**,且不提供
    「缺主体就放行」的档位 —— 那正是 I2 要防的东西。本开关只影响**是否装配**
    强制点,供不接语义层的嵌入场景显式关掉整层;生产恒为 true。
    """

    table_enforcement: str = "warn"
    require_principal: bool = True


@dataclass
class MaskingConfig:
    """字段级脱敏的部署配置(设计 §7.2)。

    ``enabled``: 是否装配脱敏节点。**默认 true** —— 与 ``authz.require_principal``
    同一个取向:默认值是「保护开着」。关掉它是嵌入场景的显式选择,不是省事的
    缺省。注意关掉只影响**这一步**,不改变任何声明(模型里的 ``mask`` 照旧
    解析、照旧序列化,重新打开即生效)。

    ``hash_salt_ref``: 部署级 salt 引用(``env:NAME``),模型级
    ``masking.hash_salt_ref`` 优先。两处都解析不出来而确有 ``hash`` 字段要应用
    → 拒绝执行(§10:不得降级为明文)。**这是引用不是值**:salt 走环境变量 /
    secrets,不进 YAML、不进 git 版本。
    """

    enabled: bool = True
    hash_salt_ref: str = ""


@dataclass
class BudgetConfig:
    """执行画像的成本轨(设计 §6.3 / §5.2)。

    与 ``explain_row_guard`` / ``explain_max_rows`` / ``explain_hard_max_rows``
    的关系:那三个键**已被本组取代**(留痕不删,见 ``load_config`` 的兼容读取),
    数值原封不动搬过来 —— 设计 §9.2 明说「现有软/硬上限的数值」不修改。

    取代的理由是那套键名把「守卫在做什么」写成了「用哪个手段做」:三档处置一直
    都在,只是第 1 档(EXPLAIN)有时拿不到依据,而拿不到时旧实现在放行。现在
    手段是可替换的(§5.2 降级链),配置该描述的是**阈值与方向**。

    ``on_unestimable``: 估算不可得时的方向。``degrade``(默认)—— 加 LIMIT
    降级执行 + 留痕;``reject`` —— 直接拒绝。设计 §8.3 C 选 degrade 的理由:
    过严的护栏会被绕过(用户去直连库),那连观测都没有了。
    """

    timeout_ms: int = 30_000
    soft_scan_rows: int = 50_000_000
    hard_scan_rows: int = 1_000_000_000
    #: 估算不可得时的保守预算(设计 §11 R2:默认 20GB 可能过松,上线后按
    #: estimated/scanned 的差值校准,逐步收紧)
    assume_max_scan_bytes: int = 20 * 1024**3
    on_unestimable: str = "degrade"


@dataclass
class EvalConfig:
    """离线评测回归门(**默认开**,与其余能力的默认关相反)。

    ``gate_enabled``: 唯一的开关。置 false 才跳过;本地手动跑
    scripts/eval_gate.py 不受它约束(除非显式 --ci)。

    **为什么默认值是 True**:本门跑在 CI/离线,零 LLM 零网络零数据库,
    误判的代价只是 CI 变红重跑一次。零成本的门没有理由默认关 —— 默认关
    的门等于没有门。留 False 的话,任何一处配置缺失(整段 ``eval:`` 被删、
    换机器、嵌进别的项目)都会让门**静默退回关闭**而没有任何信号;要关它
    就该是一次显式动作。

    ``questions_path`` / ``baseline_path``: 可复现基线产物(固定问题集 +
    基线结果)。由 scripts/build_eval_baseline.py 从 BIRD dev.json 重建,
    或(没有数据集时)复用已有问题集、只迁移结果。
    ``min_n``: 当前结果样本量下限(低于则数据不足,不判回归)。
    ``tolerances``: 单指标容差覆盖(同 eval_gate --tol)。
    """

    gate_enabled: bool = True
    questions_path: str = "eval/baseline/questions.jsonl"
    baseline_path: str = "eval/baseline/results.jsonl"
    min_n: int = 0
    tolerances: dict[str, str] = field(default_factory=dict)


@dataclass
class AttributionConfig:
    """Business-level attribution / root-cause analysis settings.

    ``enabled``: attribution intent must fire for any cost to be incurred;
    ordinary retrieval never touches the attribution node.
    ``max_hops``: drill-down depth cap (1 = dimension breakdown only,
    2 = drill into the top contributor for one more level).
    ``max_dimensions``: candidate-dimension cap in the attribution plan.
    ``probe_dimensions``: data-driven dimension pre-selection — probe each
        candidate dim (≥2) with both-period GROUP BY and pick the one with
        the largest Σ|delta| as the primary breakdown (LLM order no longer
        trusted blindly). Extra cost = (n_dims − 1) × 2 cheap queries.
    ``ratio_decomposition``: for ratio/rate metrics (AVG, A/B, SAFE_DIVIDE),
        decompose the overall change into within-group (rate) effect +
        composition (mix/structure) effect + interaction (shift-share), and
        attribute each group accordingly — instead of the wrong "additive"
        contribution math. Degrades to share attribution when either period
        has no data.
    """

    enabled: bool = True
    max_hops: int = 2
    max_dimensions: int = 3
    probe_dimensions: bool = True
    ratio_decomposition: bool = True


@dataclass
class AssetsConfig:
    """可信查询资产的台账配置(设计稿 §9.2 的 ``agent.assets.*``)。

    只管**统计**那一侧(``runs`` / ``p50_ms`` 落在独立库里)。治理字段
    (``owner`` / ``approved_by`` / ``status``)是资产属性、随 ``examples.yml``
    走 git,没有可配项;排序降权的阈值属于治理策略,不在这里。

    ``usage_enabled``: 落账总开关。默认**开** —— 统计是纯增量的旁路,
    失败也不进主链路(I2),关掉它就等于放弃了「哪些资产从没被用过」这个
    唯一能回答"该淘汰谁"的数据。

    ``events_retention_days``: 明细表保留期(天),``<=0`` = 不清理。分位数
    只能从明细算,但只增不减的表最后没人敢动。清理后 ``p50_ms`` 是**保留窗口
    内**的 p50,``runs`` 是终身计数、不随之回退。
    """

    usage_enabled: bool = True
    events_retention_days: int = 90


@dataclass
class AgentConfig:
    """Top-level agent configuration."""

    def __post_init__(self) -> None:
        # home 在此归一化为绝对路径。下游有多个消费者直接拿 Path(config.home)
        # 用(JobStore / MemoryService / 检索 indexer / trace store),而字面量
        # "~" 在 POSIX 里只是普通目录名 —— Path("~/.trove") 是**相对**路径,
        # 会相对 cwd 解析,于是从仓库根跑一次就在项目下建出 ./~/.trove/,
        # trace 与 run 日志静默写进那里(真家目录的 traces.jsonl 停在原地)。
        # 收在配置这一处,所有现有与未来的消费者同时免疫,不必逐个补 expanduser。
        self.home = str(Path(self.home).expanduser())

    home: str = "~/.trove"
    target: str = ""  # default model e.g. "openai/gpt-4o"
    model_fast: str = ""  # 快速档模型: simple/standard 复杂度走此模型(未配置 = 不分档,全走 target)
    # 每节点模型覆盖(按节点名,如 query_sketch/gen_sql/reflect/insights):优先于复杂度
    # 分档选模——query_sketch 用便宜模型、reflect 用强模型的典型配置。空 = 不覆盖。
    node_models: dict[str, str] = field(default_factory=dict)
    language: str = "zh"  # 交互语言: zh / en(不按问题语言自动检测)
    semantic_layer_path: str = ""  # OSSIE 语义层目录(相对项目根),空 = 关闭
    # KB 语义文件 git 版本管理(语义即代码):KB YAML 写操作(init/learn/草稿
    # 确认·驳回·自动应用/lesson 确认/删除)后自动 commit,git log 即审计历史。
    # best-effort:KB 不在 git 工作树内/无变更/失败 → 静默跳过,绝不影响写入。
    git_kb: bool = True
    date_parser: bool = True  # 时间解析节点:确定性规则解析相对时间(zh/en),未命中静默透传
    fast_path: bool = True  # 确定性模板快径:命中即跳过 query_sketch/生成/裁决
    reflect_skip: str = "simple"  # 规则全过后跳 LLM 裁决档位: simple / standard / all / off
    explain_semantics: bool = False  # 生成 SQL 后 LLM 说明语义(执行前展示给用户)
    hitl: bool = False  # 执行前人工确认(LangGraph interrupt; 需 checkpointer)
    insights: bool = False  # 执行后 LLM 基于结果生成洞察
    conclusion: bool = False  # 执行后 LLM 用一句话生成结论摘要(置于回答开头)
    chart_llm: bool = False  # 图表 LLM 判定:是否画图 + 类型 + 维度/度量列(失败回退确定性推断)
    result_cache: bool = False  # 精确问题结果缓存(进程内存;命中直接返回已验证答案,跳过 HITL 确认)
    decompose_llm_judge: bool = True  # 多任务拆解 LLM 判断层:规则未命中但疑似多步时花一次 LLM 判断;false = 纯正则门控
    # 结果限制(管理台可配):答案表格单次展示行数 / 查询结果行数上限
    result_display_rows: int = 50
    result_max_rows: int = 1000
    # API 速率限制(进程内令牌桶,按 user):每分钟请求数 / 每日请求配额。
    # 0 = 关闭。防单用户耗尽 LLM 成本(见 trove/services/ratelimit.py)。
    api_rate_per_minute: int = 30
    api_daily_quota: int = 300
    # EXPLAIN 行数估算守卫(默认开):每次最终执行前 EXPLAIN 估算最重算子
    # 行数,按三档处置——
    #   est ≤ explain_max_rows:放行;
    #   explain_max_rows < est ≤ explain_hard_max_rows:打回 gen_sql 加
    #     LIMIT/收窄后重生成;
    #   est > explain_hard_max_rows:直接拒绝(不烧 LLM 重生成循环)。
    # fail-open(无法解析方言/EXPLAIN 失败 → 放行):本守卫是纵深防御的
    # 体验层,不是安全边界(真正的边界在数据库侧只读角色 + LIMIT/LEAST)。
    # 见 trove/services/sql/row_guard.py。
    explain_row_guard: bool = True
    explain_max_rows: int = 50_000_000
    explain_hard_max_rows: int = 1_000_000_000
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
    # 业务级归因/根因分析:为什么类问题的多跳下钻 + 贡献率 + 瀑布图。
    attribution: AttributionConfig = field(default_factory=AttributionConfig)
    # 离线评测回归门配置(opt-in;默认不进 CI)。见 EvalConfig。
    eval: EvalConfig = field(default_factory=EvalConfig)
    # 执行前授权门:表级判定的档位 + 是否要求主体。见 AuthzConfig(默认 warn)。
    authz: AuthzConfig = field(default_factory=AuthzConfig)
    # 字段级脱敏:开关 + 部署级 salt 引用。见 MaskingConfig(默认开)。
    masking: MaskingConfig = field(default_factory=MaskingConfig)
    # 执行画像的成本轨:阈值 + 估算不可得时的方向。见 BudgetConfig。
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    # 可信查询资产的**统计**侧(台账库的位置与保留期)。见 AssetsConfig。
    # 治理字段本身没有可配项 —— 它们是资产属性,随 examples.yml 走 git。
    assets: AssetsConfig = field(default_factory=AssetsConfig)
    config_mutable: bool = True
    providers: list[ProviderConfig] = field(default_factory=list)
    datasources: list[DatasourceServiceConfig] = field(default_factory=list)
    tracing: TracingConfig = field(default_factory=TracingConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    # 定时任务调度 tick 轮询间隔(秒);serve 内置后台 tick 用。<=0 = 关闭内置调度。
    scheduler_poll_seconds: int = 30
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

        # Parse attribution (business-level root-cause analysis)
        attr_raw = agent_section.get("attribution", {}) or {}
        attribution = AttributionConfig(
            enabled=bool(attr_raw.get("enabled", True)),
            max_hops=max(1, int(attr_raw.get("max_hops", 2))),
            max_dimensions=max(1, int(attr_raw.get("max_dimensions", 3))),
            probe_dimensions=bool(attr_raw.get("probe_dimensions", True)),
            ratio_decomposition=bool(attr_raw.get("ratio_decomposition", True)),
        )

        # Parse eval gate (top-level section, not under agent:)
        eval_raw = resolved.get("eval", {}) or {}
        budget_raw = agent_section.get("budget", {}) or {}
        eval_conf = EvalConfig(
            # 缺省 True —— 与 EvalConfig 的默认值同口径。整段 eval: 缺失时
            # 门仍开着:关它必须是显式写 false,不能靠"配置不在"顺手关掉。
            gate_enabled=bool(eval_raw.get("gate_enabled", True)),
            questions_path=str(eval_raw.get("questions_path", "eval/baseline/questions.jsonl")),
            baseline_path=str(eval_raw.get("baseline_path", "eval/baseline/results.jsonl")),
            min_n=max(0, int(eval_raw.get("min_n", 0))),
            tolerances={
                str(k): str(v)
                for k, v in (eval_raw.get("tolerances", {}) or {}).items()
            },
        )

        budget_conf = BudgetConfig(
            timeout_ms=max(1000, int(budget_raw.get("timeout_ms", agent_section.get(
                # 超时只有一个来源:budget.timeout_ms 缺席时沿用顶层的
                # agent.timeout_ms —— 执行画像把「查询超时」收进了预算,
                # 但存量配置写在顶层,搬过来而不是留两处
                "timeout_ms", 30_000)))),
            soft_scan_rows=max(1000, int(budget_raw.get(
                "soft_scan_rows",
                # 兼容读取:agent.explain_max_rows 已被 agent.budget.* 取代
                # (留痕不删,数值原封不动)。两个键都在时以 budget 为准 ——
                # 阈值只能有一个来源,两处各存一份迟早漂移。
                agent_section.get("explain_max_rows", 50_000_000)))),
            hard_scan_rows=max(1000, int(budget_raw.get(
                "hard_scan_rows",
                agent_section.get("explain_hard_max_rows", 1_000_000_000)))),
            assume_max_scan_bytes=max(0, int(
                budget_raw.get("assume_max_scan_bytes", 20 * 1024**3))),
            on_unestimable=str(
                budget_raw.get("on_unestimable", "degrade")).strip().lower(),
        )

        # 执行前授权门 + 字段级脱敏(设计 §7.2)。
        # 两段都取自**顶层**(与 eval 同款,设计 §7.2 的示例就是顶层键),同时
        # 兼容仓库主流的 ``agent:`` 内嵌写法 —— 两种写法读到的都是同一份意图,
        # 只认一种会让另一种静默失效,而"配置写了不生效"是排障最贵的一类。
        #
        # ⚠️ authz 这一段是**补 P3 的漏**:``AuthzConfig`` 当时加了字段与默认值,
        # 却没有在加载器里读 YAML,于是 ``agent.yml`` 里写的 table_enforcement
        # 永远不生效(恒取默认 warn)。masking 接进来时顺手补上 —— 它比 masking
        # 更危险:一个写了 ``enforce`` 的部署以为自己开着表级判定,其实没有。
        authz_raw = resolved.get("authz", {}) or agent_section.get("authz", {}) or {}
        authz_conf = AuthzConfig(
            table_enforcement=str(
                authz_raw.get("table_enforcement", "warn")).strip().lower(),
            require_principal=bool(authz_raw.get("require_principal", True)),
        )
        masking_raw = resolved.get("masking", {}) or agent_section.get("masking", {}) or {}
        masking_conf = MaskingConfig(
            enabled=bool(masking_raw.get("enabled", True)),
            hash_salt_ref=str(masking_raw.get("hash_salt_ref", "") or "").strip(),
        )
        # 可信查询资产的台账(设计 §9.2)。同样**顶层与 ``agent:`` 内嵌两种写法
        # 都认**。这一段的写法是照着上面 authz 的教训来的:加了字段与默认值却
        # 不在加载器里读 YAML,配置就永远不生效(恒取默认),而那是排障最贵的
        # 一类问题。``<=0`` = 不清理明细,与 AssetsConfig 的语义一致。
        assets_raw = resolved.get("assets", {}) or agent_section.get("assets", {}) or {}
        assets_conf = AssetsConfig(
            usage_enabled=bool(assets_raw.get("usage_enabled", True)),
            events_retention_days=max(
                0, int(assets_raw.get("events_retention_days", 90))),
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
            git_kb=agent_section.get("git_kb", True),
            date_parser=agent_section.get("date_parser", True),
            fast_path=agent_section.get("fast_path", True),
            reflect_skip=agent_section.get("reflect_skip", "simple"),
            explain_semantics=agent_section.get("explain_semantics", False),
            hitl=agent_section.get("hitl", False),
            insights=agent_section.get("insights", False),
            conclusion=agent_section.get("conclusion", False),
            chart_llm=agent_section.get("chart_llm", False),
            result_cache=agent_section.get("result_cache", False),
            decompose_llm_judge=agent_section.get("decompose_llm_judge", True),
            result_display_rows=max(1, min(500, int(agent_section.get("result_display_rows", 50)))),
            result_max_rows=max(1, min(50000, int(agent_section.get("result_max_rows", 1000)))),
            api_rate_per_minute=max(0, int(agent_section.get("api_rate_per_minute", 30))),
            api_daily_quota=max(0, int(agent_section.get("api_daily_quota", 300))),
            explain_row_guard=bool(agent_section.get("explain_row_guard", True)),
            explain_max_rows=max(
                1000, int(agent_section.get("explain_max_rows", 50_000_000))),
            explain_hard_max_rows=max(
                1000, int(agent_section.get("explain_hard_max_rows", 1_000_000_000))),
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
            attribution=attribution,
            eval=eval_conf,
            budget=budget_conf,
            authz=authz_conf,
            masking=masking_conf,
            assets=assets_conf,
            config_mutable=agent_section.get("config_mutable", True),
            providers=providers,
            datasources=datasources,
            tracing=tracing,
            retention=retention,
            scheduler_poll_seconds=max(
                0, int(agent_section.get("scheduler_poll_seconds", 30))),
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
