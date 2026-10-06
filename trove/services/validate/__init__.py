"""Extension-surface dry run — ``trove validate`` (zero LLM, CI-able).

See ``service.run_validate`` for the contract: collect the checkers each
surface already has, add the cross-surface verdict nothing else can give
(will this actually take effect?), and never judge the same file twice with
two different implementations.
"""

from trove.services.validate.models import Issue, MountPreview, ValidateReport
from trove.services.validate.service import (
    SKILL_NODES,
    combined_exit_code,
    run_validate,
    run_validate_with_dryrun,
    skill_node_call_sites,
)

__all__ = [
    "Issue",
    "MountPreview",
    "SKILL_NODES",
    "ValidateReport",
    "combined_exit_code",
    "run_validate",
    "run_validate_with_dryrun",
    "skill_node_call_sites",
]
