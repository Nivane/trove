"""Action pillar — org response templates, proposals, approvals, outbound.

The pipeline is: a decision rule fires → a **proposal** is built from a
confirmed template (frozen payload) → an admin approves it → the dispatcher
POSTs it to a named channel → the delivery receipt lands in the store.

Nothing here can write to a business datasource: the package takes no
connector registry and imports nothing from ``services/datasource`` (the
read-only posture guard test pins it against the source).
"""

from trove.services.action.dispatcher import ActionDispatcher, DispatchResult
from trove.services.action.models import (
    PROPOSAL_STATUSES,
    ActionProposal,
    ActionTemplate,
    Approval,
    Delivery,
)
from trove.services.action.propose import ProposalError, build_proposal
from trove.services.action.service import ActionService
from trove.services.action.store import ActionStore
from trove.services.action.template_render import (
    TEMPLATE_VARIABLES,
    RenderError,
    render_payload,
)
from trove.services.action.templates import ActionTemplateService

__all__ = [
    "PROPOSAL_STATUSES",
    "TEMPLATE_VARIABLES",
    "ActionDispatcher",
    "ActionProposal",
    "ActionService",
    "ActionStore",
    "ActionTemplate",
    "ActionTemplateService",
    "Approval",
    "Delivery",
    "DispatchResult",
    "ProposalError",
    "RenderError",
    "build_proposal",
    "render_payload",
]
