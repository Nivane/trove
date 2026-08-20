"""Error classification subsystem — deterministic ErrorClass + recovery matrix.

Public API:
  - ``classify_error`` — fold (exception, text, context) into one ErrorClass.
  - ``tag_error``      — prefix a raw error string with ``[ERR:<id>]``.
  - ``is_transient``   — "retry this SQL call" decision (executor layer).
  - ``validate_arguments`` — shallow JSON-schema check of tool call args.
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

__all__ = [
    "CLASSES",
    "DETERMINISTIC_DEAD_END",
    "ClassifiedError",
    "ErrorClass",
    "RecoveryAction",
    "classify_error",
    "is_transient",
    "tag_error",
    "validate_arguments",
]