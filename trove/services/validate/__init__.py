"""Extension-surface dry run — ``trove validate`` (zero LLM, CI-able).

See ``service.run_validate`` for the contract: collect the checkers each
surface already has, add the cross-surface verdict nothing else can give
(will this actually take effect?), and never judge the same file twice with
two different implementations.
"""

from trove.services.validate.models import Issue, MountPreview, ValidateReport
from trove.services.validate.service import (
    SKILL_NODES,
    run_validate,
    skill_node_call_sites,
)

__all__ = [
    "Issue",
    "MountPreview",
    "SKILL_NODES",
    "ValidateReport",
    "run_validate",
    "skill_node_call_sites",
]
