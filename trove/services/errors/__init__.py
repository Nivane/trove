"""Error classification subsystem — deterministic ErrorClass + recovery matrix.

Public API:
  - ``classify_error`` — fold (exception, text, context) into one ErrorClass.
  - ``tag_error``      — prefix a raw error string with ``[ERR:<id>]``.
  - ``is_transient``   — "retry this SQL call" decision (executor layer).
  - ``validate_arguments`` — shallow JSON-schema check of tool call args.
  - ``present_error``  — internal failure → user-facing copy + machine detail.
"""

from trove.services.errors.classify import (
    CLASSES,
    DETERMINISTIC_DEAD_END,
    ClassifiedError,
    ErrorClass,
    RecoveryAction,
    classify_error,
    is_transient,
    tag_error,
    validate_arguments,
)
from trove.services.errors.present import present_error

__all__ = [
    "CLASSES",
    "DETERMINISTIC_DEAD_END",
    "ClassifiedError",
    "ErrorClass",
    "RecoveryAction",
    "classify_error",
    "is_transient",
    "present_error",
    "tag_error",
    "validate_arguments",
]