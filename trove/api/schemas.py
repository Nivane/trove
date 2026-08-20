"""Pydantic request/response models for the Trove HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any


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


class LessonConfirmResponse(BaseModel):
    confirmed: int
