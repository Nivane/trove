"""Pydantic request/response models for the Trove HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Literal


class ChatRequest(BaseModel):
    """POST /v1/chat body."""

    session_id: str | None = None  # omitted → a new session is created
    question: str = Field(min_length=1)
    workflow: str = "reflection"
    datasource: str | None = None  # target datasource (None = registry default)
    #: 主题域(可选):该数据源 semantics.yml 里声明的 topic 名。非空时问数
    #: 范围收敛到该域声明的 datasets;域不存在/域过期 → 显式拒绝(不是回落
    #: 全量)。空 = 不收敛,行为与不启用主题域完全一致。
    topic: str = ""
    #: 重放(设计 §5.6):admin 以目标用户身份看数据。``user:42`` / ``42``。
    #: 仅 admin、仅自己的会话;目标必须是已存在的用户。见 routers/chat._replay_subject
    on_behalf_of: str | None = None


class ResumeRequest(BaseModel):
    """POST /v1/sessions/{id}/resume body (HITL decision)."""

    decision: Any = Field(description="HITL 决定:yes/approve 或 no/reject,或任意 resume 载荷")
    workflow: str = "reflection"
    #: 与 chat 同参数:**HITL 中断必须带着重放一起续跑** —— 中断前是按目标
    #: 用户跑的一半流程,续跑时不带重放就会用发起人自己的权限把剩下半程跑完
    #: (一次运行里换身份,比整个跑错更糟)。
    on_behalf_of: str | None = None


class SemanticQueryFilter(BaseModel):
    """单个行级过滤条件(field/op/value)。"""

    field: str
    op: str = "="
    value: Any = None


class SemanticQueryRequest(BaseModel):
    """POST /v1/semantic/query body — 声明式语义查询。

    metrics/dimensions/time_grain/filters 全部须解析到已声明模型条目;
    任一不解析 → 422(严格,不猜测)。
    """

    datasource: str | None = None  # None = registry default
    metrics: list[str] = Field(min_length=1)
    dimensions: list[str] = Field(default_factory=list)
    time_grain: dict[str, Any] | None = None
    filters: list[SemanticQueryFilter] = Field(default_factory=list)
    order_by: list[dict[str, Any]] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)


class SessionCreateResponse(BaseModel):
    session_id: str


class RenameRequest(BaseModel):
    """POST /v1/sessions/{id}/title body."""

    title: str = ""


class PinRequest(BaseModel):
    """POST /v1/sessions/{id}/pin body."""

    pinned: bool = True


class TermCreate(BaseModel):
    """POST /v1/kb/terms body (flat request; converted to an OSSIE semantic_model metric on write)."""

    term: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    mapping: str = ""
    tables: list[str] = Field(default_factory=list)
    definition: str = ""


class ExampleCreate(BaseModel):
    """POST /v1/kb/examples body (examples.yml entry)."""

    question: str = Field(min_length=1)
    sql: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class LessonCreate(BaseModel):
    """POST /v1/kb/lessons body (Hint Bank entry, pending until confirmed)."""

    pattern: str = Field(min_length=1)
    note: str = Field(min_length=1)
    sql_snippet: str = ""


class LessonRatingCreate(BaseModel):
    """POST /v1/kb/ratings body (user up/down vote on a question->answer).

    Stored as a pending lesson keyed by `question` with aggregated
    upvotes/downvotes for the admin console to review.

    ``run_id`` (optional) links the rating to the Langfuse trace that
    produced the answer — used to write a user-rating score on it.
    """

    question: str = Field(min_length=1)
    note: str = ""
    sql_snippet: str = ""
    run_id: str = ""
    vote: Literal[1, -1] = Field(description="1 = upvote, -1 = downvote")



class LessonConfirmResponse(BaseModel):
    confirmed: int


class SkillCreate(BaseModel):
    """POST /v1/admin/skills/draft body — admin-authored org skill (pending).

    Mirrors the KB lessons/examples gate: drafts never reach prompts or the
    load_skill tool until an admin confirms.
    """

    name: str = Field(min_length=1, description="lowercase-hyphen name (safe file dir)")
    description: str = Field(min_length=1)
    triggers: dict[str, Any] = Field(default_factory=dict)
    tier: Literal["required", "available"] = "available"
    lang: Literal["en", "zh"] = "en"
    source: str = "admin"
    body: str = Field(min_length=1)


class SkillLlmDraftRequest(BaseModel):
    """POST /v1/admin/skills/llm-draft body — LLM drafts the skill body."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    node: str = ""  # empty = global (applies to all nodes)
    purpose: str = Field(min_length=1)
    lang: Literal["en", "zh"] = "en"


class SkillTierUpdate(BaseModel):
    tier: Literal["required", "available"]


class SkillBodyUpdate(BaseModel):
    """PUT /v1/admin/skills/{name}/body — 整篇替换正文(frontmatter 不动)。

    正文重写是内容变更:修订号 +1,并自动提交一条 ``skills: body <name>``
    的 git commit(审计史即回滚点)。
    """

    body: str = Field(min_length=1)


class SkillRollbackRequest(BaseModel):
    """POST /v1/admin/skills/{name}/rollback — 回滚到指定历史 commit。

    回滚以**一条新 commit** 落盘(历史不改写),修订号继续前进;
    ``message`` 留空时服务端生成 ``skills: rollback <name> to <sha8>``。
    """

    sha: str = Field(min_length=1)
    message: str = ""


class FactCreate(BaseModel):
    """POST /v1/facts body — user-level memory (preference / caliber)."""

    datasource: str = Field(min_length=1)
    fact: str = Field(min_length=1)


class FactPatch(BaseModel):
    """PATCH /v1/facts/{id} body (both optional)."""

    fact: str | None = Field(default=None, min_length=1)
    datasource: str | None = Field(default=None, min_length=1)


class SemanticDraftCreate(BaseModel):
    """POST /v1/admin/semantic/{ds}/drafts body (审批流草稿).

    kind: metric | field | dataset | topic;action: upsert | delete。
    payload 为平铺友好结构,confirm 时转换为 OSSIE 文档(见
    services/semantic_layer/manage.py)。
    """

    kind: Literal["metric", "field", "dataset", "topic"]
    action: Literal["upsert", "delete"]
    name: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class SemanticRollbackRequest(BaseModel):
    """POST /v1/admin/semantic/{ds}/rollback body — 回滚到指定 commit。

    sha 必须是该数据源 KB 文件的真实历史 commit(git log 返回的 sha)。
    """

    sha: str = Field(min_length=1, description="目标 commit sha")
    message: str = ""  # 可选:rollback 提交的自定义消息


# ── 语义工作台 P2 地基:validate / preview / batch 契约 ────────
#
# 三个契约先钉死形状再写端点(方案「后端端点」):DiffCard props 见
# SemanticDraftDiff,issue_items 见 SemanticIssueItem,批量结果见
# SemanticBatchResultItem。字段名即前端可直接消费的形状,勿随意改名。


class SemanticIssueTarget(BaseModel):
    """问题定位到的实体(前端按 kind 分流跳转/分组)。"""

    kind: str = ""   # metric | field | dataset | relationship | topic | document | unknown
    name: str = ""   # metric/关系/主题域名,field 为 dataset.field,dataset 为数据集名


class SemanticIssueItem(BaseModel):
    """结构化问题条目(契约:manage 的 issues / validate 共用同一种形状)。

    ``code`` 是稳定契约(前端按 code 分流 UI),``message`` 是 lint 原文,
    ``hint`` 是一句话修复建议。未分类的问题落 ``code="lint"``,不丢弃。
    """

    severity: Literal["error", "warning"]
    code: str
    target: SemanticIssueTarget
    message: str
    hint: str = ""


class SemanticValidateRequest(BaseModel):
    """POST /v1/admin/semantic/{ds}/validate body —— 草稿干跑校验。

    纯函数:与 confirm 走同一条应用路径(语法 + 锚定 + 文档 lint),
    不写任何文件。``payload`` 与创建草稿时同形。
    """

    kind: Literal["metric", "field", "dataset", "topic"]
    action: Literal["upsert", "delete"]
    name: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class SemanticNormalized(BaseModel):
    """规范化结果(SQLGlot 渲染);解析失败或非表达式草稿时为空串。"""

    expression: str = ""


class SemanticValidateResponse(BaseModel):
    """``ok`` 取「能不能过写盘门禁」语义。

    今天的门禁(lint issues 非空即拒)不分 error/warning 级别,**warning
    同样拦 confirm**,所以 errors 与 warnings 任一非空 → ok=False;
    两个列表只是**原因分类**,不是「致命/不致命」。
    """

    ok: bool
    errors: list[SemanticIssueItem] = Field(default_factory=list)
    warnings: list[SemanticIssueItem] = Field(default_factory=list)
    normalized: SemanticNormalized = Field(default_factory=SemanticNormalized)


class SemanticDiffRow(BaseModel):
    """DiffCard 的一行(与设计稿原型同形:``{f, before, after, changed}``)。"""

    f: str
    before: str
    after: str
    changed: bool


class SemanticDraftDiff(BaseModel):
    """草稿 diff —— before/after 由**服务端**算(carryover 语义只在服务端)。

    ``before``/``after`` 是平铺后的实体视图(不存在 → null);
    ``error`` 为干跑失败原因(超契约补的键,便于前端显示坏草稿卡片)。
    """

    kind: str
    name: str
    action: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    fields: list[SemanticDiffRow] = Field(default_factory=list)
    error: str | None = None


class SemanticPreviewQuery(BaseModel):
    """预览时的查询参数(与 POST /v1/semantic/query 同语义)。"""

    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    time_grain: dict[str, Any] | None = None
    filters: list[SemanticQueryFilter] = Field(default_factory=list)
    order_by: list[dict[str, Any]] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1)


class SemanticPreviewRequest(BaseModel):
    """POST /v1/admin/semantic/{ds}/preview body —— 草稿试跑(零副作用)。

    草稿应用到内存副本 → 编译 → 有界执行(与 /semantic/query 同一条链,
    含脱敏)。``query.metrics`` 留空且草稿是 metric upsert 时,默认用草稿
    自身的指标名试跑。
    """

    kind: Literal["metric", "field", "dataset"]
    action: Literal["upsert", "delete"]
    name: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    query: SemanticPreviewQuery = Field(default_factory=SemanticPreviewQuery)


class SemanticPreviewResponse(BaseModel):
    """与 POST /v1/semantic/query 同形 + ``warnings``(草稿的 lint 警告)。"""

    sql: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    masking_applied: dict[str, Any] | None = None
    warnings: list[SemanticIssueItem] = Field(default_factory=list)


class SemanticBatchRequest(BaseModel):
    """POST /v1/admin/semantic/{ds}/drafts/batch body —— 批量审批。

    逐条独立(一条失败不影响其余),``note`` 落到每条审计明细。
    """

    ids: list[str] = Field(min_length=1)
    action: Literal["confirm", "reject"]
    note: str = ""


class SemanticBatchResultItem(BaseModel):
    """单条草稿的批处理结果(契约:``{id, ok, error}``)。"""

    id: str
    ok: bool
    error: str | None = None


class SemanticBatchResponse(BaseModel):
    """批量结果 —— ``applied``/``failed`` 与 results 计数一致(便于直接渲染)。"""

    results: list[SemanticBatchResultItem] = Field(default_factory=list)
    applied: int = 0
    failed: int = 0


# ── 语义变更评审契约（设计 §7.2）───────────────────────────
# 「变更 = 合并单元」的 HTTP 面；409/422 的机器可读码在路由层，形状在此钉死。


class SemanticChangePayload(BaseModel):
    """一条 payload 变更（与草稿 payload 同形）。"""

    kind: Literal["metric", "field", "dataset", "topic"]
    action: Literal["upsert", "delete"]
    name: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    note: str = ""


class SemanticChangeCreate(BaseModel):
    payloads: list[SemanticChangePayload] = Field(min_length=1)
    question: str = ""
    note: str = ""


class SemanticChangeReject(BaseModel):
    reason: str = Field(min_length=1)


class LoginRequest(BaseModel):
    """POST /v1/auth/login body."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class UserCreate(BaseModel):
    """POST /v1/admin/users body."""

    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    role: str = "user"
    display_name: str = ""


class UserPatch(BaseModel):
    """PATCH /v1/admin/users/{id} body (all fields optional)."""

    password: str | None = Field(default=None, min_length=1)
    role: str | None = None
    display_name: str | None = None
    disabled: bool | None = None


class TokenCreate(BaseModel):
    """POST /v1/admin/users/{id}/tokens body."""

    label: str = ""
    ttl_hours: int | None = Field(default=None, ge=1)
    scopes: list[str] = Field(
        default_factory=list,
        description=(
            "Token 作用域 allowlist(如 [\"query\"]);空 = 不限(用户全部权限)"
        ),
    )


class DatasourcesPut(BaseModel):
    """PUT /v1/admin/users/{id}/datasources body."""

    datasources: list[str] = Field(default_factory=list)


class TopicGrantsPut(BaseModel):
    """PUT /v1/admin/users/{id}/topic-grants body.

    三态都要原样保留 —— 塌陷成空字典会让「取消收窄」变成「全部隐藏」:

    * ``None``  = 不收窄(可见数据源的全部主题域);
    * ``{}``    = 显式收窄到一个域都不见;
    * ``{"ds": ["topic", ...]}`` = 严格清单,未列出的数据源 = 该源无域。
    """

    topic_grants: dict[str, list[str]] | None = None


class JobCreate(BaseModel):
    """POST /v1/admin/jobs body — a scheduled question (cron/interval + alert)."""

    question: str = Field(min_length=1)
    schedule: str = Field(min_length=1, description="cron expr or interval minutes")
    schedule_type: Literal["interval", "cron"] = "interval"
    name: str = ""
    datasource: str = "demo"
    workflow: str = "reflection"
    alert_expr: str = ""
    alert_channel: str = ""
    alert_cooldown_min: int = Field(default=30, ge=0)
    decision_rule: str = Field(
        default="",
        description="Decision rule id from the datasource's decisions.yml; "
                    "non-empty runs the deterministic decision engine instead "
                    "of the NL pipeline (alert_expr is then unused)",
    )
    topic: str = Field(
        default="",
        description="Topic domain (declared in the datasource's semantic "
                    "model) to scope the scheduled question to; empty = "
                    "unrestricted. Checked at write time: an unknown or "
                    "expired domain is a 400, never a job that fails the "
                    "same way on every tick",
    )
    scan_spec: dict[str, Any] = Field(
        default_factory=dict,
        description="Active-scan spec (B6): {metrics, dimensions, window, "
                    "grain, mode, lookback, k, top_k, hypotheses}. Non-empty "
                    "runs the deterministic scan instead of the NL pipeline "
                    "(decision_rule wins when both are set). Validated at "
                    "write time — metrics are checked against the datasource's "
                    "semantic model, so a dangling reference is a 400, never "
                    "a job that fails the same way on every tick",
    )


class JobPatch(BaseModel):
    """PATCH /v1/admin/jobs/{id} body (all fields optional)."""

    name: str | None = None
    question: str | None = Field(default=None, min_length=1)
    schedule: str | None = None
    schedule_type: Literal["interval", "cron"] | None = None
    datasource: str | None = None
    workflow: str | None = None
    alert_expr: str | None = None
    alert_channel: str | None = None
    alert_cooldown_min: int | None = Field(default=None, ge=0)
    decision_rule: str | None = None
    topic: str | None = None
    scan_spec: dict[str, Any] | None = None
    enabled: bool | None = None


class SubscriptionCreate(BaseModel):
    """POST /v1/admin/jobs/{id}/subscriptions body — 把一个用户订到任务的报告上."""

    subscriber: str = Field(min_length=1, description="username")
    channel: str = Field(
        default="",
        description="console | webhook:<url>；空 = 继承任务的 alert_channel"
                    "（再退到 console）",
    )
    mode: Literal["always", "alert_only"] = "always"


class SubscriptionPatch(BaseModel):
    """PATCH 订阅 body（全部可选）——管理员或订阅者本人。"""

    channel: str | None = None
    mode: Literal["always", "alert_only"] | None = None
    enabled: bool | None = None


class DecisionDocBody(BaseModel):
    """PUT /v1/admin/decisions body — replaces a datasource's whole
    ``decisions.yml``.

    ``rules`` is intentionally untyped: the rule schema is owned by
    ``services.decision.rules`` (which owns the lint messages), and mirroring
    it here would give the API a second, drifting copy. The document is
    parsed by the same ``parse_document`` the file reader uses, so an
    invalid body fails identically however it arrives.

    ``text`` is the raw YAML file, for callers that edit the document as
    text. It exists so the admin editor needs no YAML parser of its own.
    Exactly one of the two.

    Note the asymmetry: the **read** (``GET /admin/decisions/raw``) is the
    file verbatim, comments included, but the **write** goes through
    ``KbService._write_doc`` — the KB's single write entry point, which
    re-stamps ``_meta``. So a save canonicalizes the file and the comments
    in it do not survive. Bypassing that to preserve them would leave the
    digest stale, which the next read would report as "a human edited this".
    """

    datasource: str = Field(min_length=1)
    rules: list[dict[str, Any]] | None = None
    text: str | None = None
    version: int = 1
    message: str = ""


class DecisionSimulateBody(BaseModel):
    """POST /v1/admin/decisions/{rule_id}/simulate body — what-if 场景。

    数据来源两条,都**不触业务库**:``source="verdict"``(默认)重放
    最近一条 verdict 的 ``evidence.rows`` —— 判定当时判的就是那份数字;
    ``source="caller"`` 用这里给的 ``current``/``baseline`` maps(哪来的
    数字由调用方负责,端点只管在同一条判定内核上重跑)。

    ``scenario`` 的键集封闭(``dim``/``field``/``mode``/``value``,
    见 ``services.decision.whatif``):未知键/未知 field/mode 在端点
    拒绝 —— 静默忽略一条调整会让「模拟过了」与「模拟的是别的场景」
    看起来一样。结构校验在 whatif 层(它拥有那些消息)。

    ``impacts`` = {组件: 变化量},给了才算驱动器树的根级总变化(树取自
    重放 verdict 的证据;不可分解节点不会硬凑总数,见 ``simulate_tree``)。
    """

    scenario: list[dict[str, Any]] = Field(default_factory=list)
    source: Literal["verdict", "caller"] = "verdict"
    current: dict[str, float | None] | None = None
    baseline: dict[str, float | None] | None = None
    row_count: int | None = Field(default=None, ge=0)
    impacts: dict[str, float] | None = None


class SettingsUpdate(BaseModel):
    """PUT /v1/admin/settings body — partial flat updates keyed by the
    settings schema (e.g. `llm.default_model`, `app.hitl`)."""

    values: dict[str, Any] = Field(default_factory=dict)


class DriftResolveRequest(BaseModel):
    """POST /v1/admin/drift/{id}/resolve | /waive body.

    ``reason`` 必填且非空 —— 无理由的 resolve 等于删记录:条目会从待办里
    消失,而「为什么它可以不管了」没有任何地方留下。豁免尤其需要它:豁免
    是**对立场的断言**,再次检出不会重开,所以理由就是这条决定的全部痕迹。

    ``by`` 只在没有登录身份时(测试替身)才被采用;真实请求以 token 里的
    用户名为准 —— 让请求体自称操作者,审计就等于没有审计。
    """

    reason: str = Field(min_length=1)
    by: str = ""


class DriftDeclareRequest(BaseModel):
    """POST /v1/admin/drift/external body — 承接 L4 口径漂移。

    L4(上游改了聚合逻辑/口径)没有检测器:物理 schema 可以完全不变,任何
    确定性比对都看不见。所以这条路径的存在不是权宜之计,是分类学的一部分
    —— 系统能做的只有承接并留痕。

    ``subject`` 会过 ``normalize_subject``,与检测器产出共用同一套规范化,
    否则它与 ``touched_subjects`` 的交集永远为空、门禁形同虚设。
    """

    datasource: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    kind: str = "semantics_changed"
    detail: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["info", "warning", "critical"] = "warning"
    author: str = ""


class ActionTemplateCreate(BaseModel):
    """POST /v1/admin/actions/templates body — admin-authored response template.

    Created as a *pending* draft (same gate as skills): a decision rule may
    not reference it until an admin confirms it. ``target.channel`` names a
    channel configured under ``agent.action.channels`` — templates never
    carry a bare URL, so reviewing one never means reviewing a credential.

    ``payload_template`` is a JSON document with closed-set ``{{variable}}``
    holes; it is rendered once against sample values at create time, so a
    typo'd variable is a 400 here rather than a surprise on the night the
    rule first fires.
    """

    name: str = Field(min_length=1, description="lowercase-hyphen name (safe file dir)")
    title: str = Field(min_length=1)
    description: str = ""
    action_type: Literal["notify", "webhook"] = "notify"
    target: dict[str, Any] = Field(default_factory=dict)  # {channel, resource}
    risk: Literal["low", "medium", "high"] = "low"
    payload_template: str = Field(min_length=1)


class ActionDecision(BaseModel):
    """POST /v1/admin/actions/proposals/{id}/{decision} body.

    ``comment`` is optional everywhere but is the only place a rejection
    reason lives; the audit row keeps it verbatim.
    """

    comment: str = ""


class ActionBatchRequest(BaseModel):
    """POST /v1/admin/actions/proposals/batch body —— 批量审批。

    ``decision`` 故意是裸 ``str`` 而不是 ``Literal``:允许动词集由路由校验,
    非法值报 400 并把允许值清单写进 detail —— ``Literal`` 会让 pydantic
    抢先抛 422,文案里没有"允许哪些"。逐条独立(一条失败不影响其余),
    ``comment`` 落到每条审计明细。
    """

    ids: list[str] = Field(min_length=1)
    decision: str
    comment: str = ""


class PresetApplyRequest(BaseModel):
    """POST /v1/admin/presets/{name}/apply body — 套用到哪个数据源。

    套用**只产 pending 草稿**(技能门 / 决策草稿 / 语义审批流),不写任何
    生效文件;逐条确认后才生效。
    """

    datasource: str = Field(min_length=1)


class PresetBody(BaseModel):
    """PUT /v1/admin/presets/{name} body — 写入一份**组织** preset。

    与决策文档同一手法:``preset`` 是结构化映射,``text`` 是原始 YAML;两者
    恰好给一个。preset 的契约(闭键集/必填字段)由 ``services.presets``
    拥有 —— 这里不镜像一份会漂移的副本,坏 body 的报错就是校验器自己的话。
    """

    preset: dict[str, Any] | None = None
    text: str | None = None
    message: str = ""


class PresetRollbackRequest(BaseModel):
    """POST /v1/admin/presets/{name}/rollback body(回滚 = 一次新的修订)。"""

    sha: str = Field(min_length=1)
    message: str = ""
