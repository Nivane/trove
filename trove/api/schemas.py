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

    kind: metric | field | dataset; action: upsert | delete。
    payload 为平铺友好结构,confirm 时转换为 OSSIE 文档(见
    services/semantic_layer/manage.py)。
    """

    kind: Literal["metric", "field", "dataset"]
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
