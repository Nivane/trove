"""Semantic layer models shared by parsers and consumers.

Models mirror the Apache Ossie core spec (v0.2.0.dev0) we consume:
datasets (with fields/primary keys/unique keys), relationships (the
declared join graph), metrics (business phrase → aggregate SQL
expression), plus the extensibility/context surface (custom_extensions,
ai_context.examples, labels). Metrics map 1:1 onto TermHit for retrieval
(see kb.service.search_terms); datasets and relationships are the
deterministic structure layer that later stages compile queries from
(see services/kb/semantic_gen.py).
"""
from dataclasses import dataclass, field

#: 字段级 ``values`` 的条数上限(结构事实:该列的实际取值)。
#: 与 ``services.kb.enum_probe.VALUE_PROBE_LIMIT`` 同一个界 —— 探测侧
#: 只探这么多,装载侧也只收这么多(超出的部分截断,坏形状由 lint 拦)。
MAX_FIELD_VALUES = 100


def _clean_values(raw: object) -> list[str]:
    """``values`` 净化:非空 string 列表,去空白,≤ :data:`MAX_FIELD_VALUES`。

    容忍缺省(``None`` → ``[]``,存量模型走这条)与裸标量
    (``values: Sokolov`` → 单元素列表,与 ``_masking_policy`` 的
    ``bypass_scopes`` 同法);其它坏形状(映射/嵌套容器)忽略而不是猜 ——
    "猜"会让一份写错的文件静默变成一份能跑的模型。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for v in raw:
        if v is None or isinstance(v, (dict, list, tuple, set)):
            continue
        s = str(v).strip()
        if s:
            out.append(s)
    return out[:MAX_FIELD_VALUES]


def _clean_names(raw: object) -> list[str]:
    """名字列表净化:非空 string、去空白、保序去重。

    容忍缺省(``None`` → ``[]``)与裸标量(``datasets: loan`` → 单元素列表,
    与 ``_clean_values`` 同法);映射/嵌套容器忽略而不是猜 —— 猜会让一份写错
    的文件静默变成一份能跑的主题域。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for v in raw:
        if v is None or isinstance(v, (dict, list, tuple, set)):
            continue
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out


def _clean_extensions(raw: list | None) -> list[dict]:
    """custom_extensions 净化:只保留 {vendor_name, data} 形式且 vendor_name
    非空的条目(空 vendor_name 是坏条目,lint 也会标)。"""
    out: list[dict] = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        vendor = str(e.get("vendor_name") or "").strip()
        if not vendor:
            continue
        out.append({"vendor_name": vendor, "data": e.get("data", "")})
    return out


@dataclass
class SemanticMetric:
    """A business metric with synonyms, SQL expression and source datasets.

    datasets: logical dataset names referenced by the expression
        (`dataset.field`); empty means table-agnostic (no anchoring).

    metric_type: OSSIE ``type`` — "" | "simple" | "derived" | "ratio"
        (ratio 按 derived 处理)。derived/ratio 的表达式可引用其他已声明
        metric 名(MetricFlow 风格),编译期递归内联。

    datatype: OSSIE DataType of the metric's value (Decimal/Float/...).
    examples: OSSIE ai_context.examples — 该度量的示例问句(AI 上下文)。
    custom_extensions: OSSIE vendor 扩展(vendor_name + data,透传保留)。
    """

    name: str
    expression: str
    synonyms: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    definition: str = ""
    metric_type: str = ""
    # metric 级行级过滤(measure filter):如 ``status = 'A'``。编译期并入
    # WHERE(列须解析到本 metric 锚定数据集的已声明字段,否则保守 MISS)。
    # 建模约束:对已声明 enum 字段的等值过滤应走维度过滤(lint 拦截)。
    filter: str = ""
    # 聚合时间维度:该 metric 按哪个时间字段聚合(``loan.date`` 或裸列名)。
    # 时间范围注入时优先用它,解决"matched 内多时间字段无法判定"的覆盖损失。
    agg_time_dimension: str = ""
    # 非加性标记:count distinct / ratio 等不可再加总的度量。lint 用于
    # 警告"被其他度量再聚合",本身不阻断编译。
    non_additive: bool = False
    datatype: str | None = None
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)


@dataclass
class SemanticField:
    """A row-level attribute (dimension / filter) on a dataset.

    expression: scalar (non-aggregate) SQL expression, dialect-picked.
    is_time: OSSIE temporal-role flag — defaults to True for temporal
        datatypes (Date/Time/DateTime/DateTimeTz) unless overridden.
    semantic_role: Palantir 风格属性角色 —— identifier / measure /
        dimension / enum / time。让字段候选检索与编译器不用猜"这列
        能不能聚合/该不该分组"(P5.1)。
    enum_display: 枚举列的 ``code → 人类可读词`` 字典(过滤值锚定用,
        "POPLATEK MESICNE" → "monthly")。运行时把人类值归一成 code。
    values: 该列的实际取值(structural fact,probe 产物);见下。
    label: OSSIE ``label`` — 分类标签(AI/UI 归类用,透传)。
    examples: OSSIE ai_context.examples — 该字段的示例问句。
    custom_extensions: OSSIE vendor 扩展(透传保留)。
    """

    name: str
    expression: str
    datatype: str | None = None
    is_time: bool = False
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    semantic_role: str = ""  # identifier | measure | dimension | enum | time
    enum_display: dict[str, str] = field(default_factory=dict)
    # 值语义字典(多标签):``{code: [业务别名]}``——扩展示例问法词到存储值
    # 的确定性桥("weekly issuance" → POPLATEK TYDNE)。kb init 从 schema_notes
    # 的 ``CODE=label`` 标注/值别名沉淀,或建模期人工/证据标注。编译器
    # ``_enum_code_for`` 在 enum_display 基础上并入这些别名做词级匹配;
    # 多 code 同命中 → 保守 MISS(值歧义,不猜)。
    value_aliases: dict[str, list[str]] = field(default_factory=dict)
    #: 该列的**实际取值**(``SELECT DISTINCT`` 探测产物,≤ MAX_FIELD_VALUES)。
    #: 纯结构事实,不是语义声明:它不提升 ``semantic_role``、不进提示词渲染、
    #: 不参与占比/条件构造,只回答"这个字面量属于哪一列"(值路由用)。
    #: 缺省空列表 = 未探测/取值域超界(存量模型全落这一侧,行为不变)。
    values: list[str] = field(default_factory=list)
    label: str = ""
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)
    #: 字段级脱敏声明(设计 §5.5):``""`` | ``none`` | ``partial`` | ``hash``
    #: | ``null``。空串与 ``none`` 同义(存量兼容,A11)——缺省不是 ``partial``
    #: 之类「有效果」的值:一个没声明过的模型不该在升级后开始改写结果。
    mask: str = ""


@dataclass
class SemanticRelationship:
    """A declared join edge: ``from_`` = many side, ``to`` = one side.

    from_columns/to_columns are ordered key pairs (composite joins
    supported); the many→one direction maps onto the OSSIE
    ``relationships`` block and the MetricFlow convention that avoids
    fan-out joins.

    cardinality: 从 ``to`` 侧看 ``from`` 侧(ER 惯例)——
        "1:N"=一个 to 对应多个 from(默认,由 many→one 构造推断)、
        "1:1"、显式 "M:N" 表示多对多。

    fan_out: M:N 边的显式豁免方式——空(默认)= 编译期拒 fan-out
        (行倍增,回交 LLM + 规则链兜底);``dedup`` = 编译期把 from
        侧包成 ``SELECT DISTINCT *`` 子查询,消除关联/桥接表里的整行
        重复(精确重复行是唯一安全、免额外建模的去重情形);
        ``bridge:<dataset>`` 保留(未来:走已预聚合桥表,支持同键多行)。
        genuinely 多对多(同键多行且每行语义不同)应建模为 1:N + 中间
        表(编译器完全支持),而不是豁免。
    """

    name: str
    from_: str
    to: str
    from_columns: list[str] = field(default_factory=list)
    to_columns: list[str] = field(default_factory=list)
    cardinality: str = ""  # ""(安全,默认) | "1:N" | "1:1" | "M:N"
    fan_out: str = ""  # "" | "dedup" | "bridge:<dataset>"
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)


@dataclass
class SemanticDataset:
    """A logical dataset: physical table + declared fields + keys.

    unique_keys: OSSIE ``unique_keys`` — 数组的数组,每个都是唯一键
        (单列或复合)。与 primary_key 同构,消费端可作为联表/去重依据。

    row_filter: 数据集级行级安全(RLS)声明——一条恒真的布尔 SQL 谓词
        (如 ``status = 'A'`` / ``region = 'EU'``),编译期注入到**每个**
        引用该数据集的 FROM/JOIN 的顶层 WHERE(数据集 JOIN 均为内连接,
        顶层过滤与联前过滤等价)。列须解析到本数据集的已声明字段(lint
        拦截);空 = 无行级限制。这是**声明层**授权,不替代数据库侧只读
        角色/行级安全(执行期 allowlist 仍兜底)。
    """

    name: str
    source: str = ""  # physical table reference (schema.table)
    primary_key: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    row_filter: str = ""
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    fields: list[SemanticField] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)


@dataclass
class TimeSpine:
    """模型级时间轴声明(OSSIE ``time_spine``):空档补全的时间序列。

    field: 声明的时间字段(``dataset.field`` 或裸列名)。
    granularity: 序列粒度(year/quarter/month/week/day)。
    fill: 缺期填充策略——"none"(默认,显示 NULL) | "0" | "previous"。
    """

    field: str = ""
    granularity: str = "month"
    fill: str = "none"


@dataclass
class MaskingPolicy:
    """模型级脱敏策略(设计 §5.5 / §6.1)。

    ``default_policy``: 不持 ``bypass_scopes`` 的主体的待遇 —— ``apply``
    (默认,脱敏) | ``bypass``(原样)。admin **不自动 bypass**(§5.5):
    看原文要显式持 scope,这样「谁签发过看得见原文的凭证」在 token 侧
    就有记录。

    ``hash_salt_ref``: 指向 salt 的**引用**(如 ``env:TROVE_MASK_SALT``),
    不是 salt 本身 —— 值走 secrets,不进 YAML、不进 git 版本。模型级为空
    时运行时回落到部署配置 ``masking.hash_salt_ref``;两处都解析不出来而
    又有 ``hash`` 字段在场 → 拒绝执行(§10:不得降级为明文)。

    ``bypass_scopes`` 用**原始集合求交**判定,不走 ``scopes_allow`` ——
    「空 scopes = 不限」那条规则是给路由门的存量兼容准备的,方向是宽;
    PII 披露没有下层兜底(§8.1 判据),空 scopes 在这里必须意味着
    「没有 pii」,否则每一个存量 token 和 ``on_behalf_of`` 重放都会
    直接看到原文。
    """

    default_policy: str = "apply"
    bypass_scopes: list[str] = field(default_factory=list)
    hash_salt_ref: str = ""


@dataclass
class TopicDomain:
    """业务主题域 —— 语义模型之上的**分组与收敛边界**(Trove 扩展,非 OSSIE)。

    形态学参照:Datus subject tree / Databricks Genie space —— 用户按业务主题
    进入,问数范围被主题收敛。

    ``datasets`` 是**权威作用域**:主题域命中时,schema linking 的数据集锚定
    被限制在这个集合内(检索/指标扩展都不得越界)。名字必须解析到**同一份
    文档**里已声明的 dataset(lint 硬拦),所以作用域永远不是"悬空引用"。

    ``metrics`` 可选:声明该主题对外口径的度量,须已声明且锚定在 datasets
    之内(否则主题内编译必然 MISS,lint 拦)。空 = 不限制(主题只是范围)。

    ``synonyms`` 供检索/前端展示用(路由仍由调用方显式指定,不做自动路由);
    ``examples`` 是主题的示例问句(前端起始提问用)。

    ``custom_extensions`` 与其它实体同款(OSSIE vendor 扩展,透传保留)。
    """

    name: str
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)


@dataclass
class SemanticModel:
    """One parsed semantic model (OSSIE `semantic_model` entry).

    version: OSSIE 文档级 ``version``(semantic_model 上方,透传)。
    examples: model 级 ai_context.examples(示例问句)。
    custom_extensions: model 级 vendor 扩展(透传保留)。
    time_spine: 模型级时间轴声明(见 TimeSpine);空 = 不启用空档填充。
    topics: 业务主题域(见 TopicDomain);空 = 未分组(存量模型走这条,
        行为与不启用主题域完全一致)。
    """

    name: str = ""
    description: str = ""
    instructions: str = ""  # model-level ai_context.instructions
    metrics: list[SemanticMetric] = field(default_factory=list)
    datasets: list[SemanticDataset] = field(default_factory=list)
    relationships: list[SemanticRelationship] = field(default_factory=list)
    version: str = ""
    examples: list[str] = field(default_factory=list)
    custom_extensions: list[dict] = field(default_factory=list)
    time_spine: TimeSpine | None = None
    #: 模型级脱敏策略(见 MaskingPolicy);缺省 = 不脱敏(存量兼容,A11)
    masking: MaskingPolicy = field(default_factory=MaskingPolicy)
    #: 业务主题域(见 TopicDomain);空 = 未启用主题分组(存量模型走这条)
    topics: list[TopicDomain] = field(default_factory=list)
