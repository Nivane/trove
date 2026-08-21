"""Pydantic request/response models for the Trove HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any, Literal


class ChatRequest(BaseModel):
    """POST /v1/chat body."""

    session_id: str | None = None  # omitted → a new session is created
    question: str = Field(min_length=1)
    workflow: str = "reflection"


class ResumeRequest(BaseModel):
    """POST /v1/sessions/{id}/resume body (HITL decision)."""

    decision: Any = Field(description="HITL 决定:yes/approve 或 no/reject,或任意 resume 载荷")
    workflow: str = "reflection"


class SessionCreateResponse(BaseModel):
    session_id: str


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
    """

    question: str = Field(min_length=1)
    note: str = ""
    sql_snippet: str = ""
    vote: Literal[1, -1] = Field(description="1 = upvote, -1 = downvote")



class LessonConfirmResponse(BaseModel):
    confirmed: int


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


class DatasourcesPut(BaseModel):
    """PUT /v1/admin/users/{id}/datasources body."""

    datasources: list[str] = Field(default_factory=list)
