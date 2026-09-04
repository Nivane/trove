"""Confirm-draft node — admin approves a pending semantic draft in chat.

Semantic-first refusal (refuse.py) writes a pending metric/field draft to
``semantic_drafts.yml``. This node lets an **admin** confirm that draft
directly in the conversation instead of leaving the chat for the admin
console:

  - non-admin → guidance message (admin-only operation)
  - no pending draft for the datasource → guidance message
  - otherwise: ``SemanticManager.confirm_draft`` applies the most recent
    pending draft to ``semantics.yml``, then the **previous question** is
    substituted and the query pipeline re-runs — the now-covered question
    compiles and answers in the same turn.

Routing happens via the ``confirm`` intent (see workflow.intent
``has_strong_confirm``); this node performs the role/authority checks the
intent layer cannot see.

Node shape: ``make_confirm_draft(kb, connectors) -> async def confirm_draft(state) -> dict``
returns a partial state update.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L
from trove.workflow.intent import last_user_question
from trove.workflow.state import WorkflowState

logger = logging.getLogger(__name__)


def make_confirm_draft(
    kb: Any | None = None,
    connectors: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the confirm_draft node bound to KB + datasource registry.

    Args:
        kb: Optional KbService — enables reading/writing the pending draft
            via SemanticManager (semantic_drafts.yml + semantics.yml).
        connectors: ConnectorRegistry for resolving the adapter dialect
            used by confirm_draft's expression validation.
    """

    async def confirm_draft(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}

        if not state.is_admin:
            return {"intent_answer": L(
                state.lang,
                "确认语义草稿仅限管理员操作。请到管理端「语义模型」中确认该草稿，"
                "确认后系统会立即重答原问题。",
                "Confirming a semantic draft is an admin-only operation. Please "
                "confirm the draft in the admin console — the question is re-answered "
                "immediately after confirmation.",
            )}

        datasource = state.datasource or ""
        if not datasource or kb is None:
            return {"intent_answer": L(
                state.lang,
                "缺少数据源上下文，无法确认草稿。请在选中数据源后重试，或到管理端确认。",
                "Missing datasource context; cannot confirm a draft. Retry with a "
                "datasource selected, or confirm in the admin console.",
            )}

        from trove.services.semantic_layer.manage import SemanticManager

        try:
            pending = SemanticManager(kb).drafts(datasource)["pending"]
        except Exception as e:
            logger.warning("Draft listing failed (%s): %s", datasource, e)
            pending = []
        if not pending:
            return {"intent_answer": L(
                state.lang,
                "当前没有待确认的草稿。若刚才的问题被拒绝，请重发问题以重新生成草稿。",
                "No pending drafts to confirm. Re-ask the refused question to "
                "regenerate a draft first.",
            )}

        dialect = "sqlite"
        if connectors is not None:
            try:
                adapter = await connectors.get(state.datasource or None)
                dialect = adapter.dialect() or "sqlite"
            except Exception:
                pass

        draft = pending[-1]  # most recent pending draft (the one just refused)
        try:
            await SemanticManager(kb).confirm_draft(
                datasource, draft["id"], dialect=dialect)
        except Exception as e:
            logger.warning("Draft confirm failed (%s): %s", datasource, e)
            return {"intent_answer": L(
                state.lang,
                f"草稿确认失败：{e}。请到管理端检查该草稿。",
                f"Draft confirm failed: {e}. Check the draft in the admin console.",
            )}

        prev = last_user_question(state.history)
        if prev and prev != state.question:
            # 确认生效 → 用上一问重跑管线,同轮完成"确认后立即重答"
            return {
                "question": prev,
                "rewritten_question": state.question,
                "intent": "query",
                "intent_evidence": {
                    "confirm_draft": True,
                    "draft_kind": draft.get("kind"),
                    "draft_name": draft.get("name"),
                    "substituted": True,
                },
            }

        return {"intent_answer": L(
            state.lang,
            "已确认草稿（可以重发原问题，系统将直接回答）。",
            "Draft confirmed — re-ask the original question and it will be answered.",
        )}

    return confirm_draft
