"""身份与权限执行(authz)。

- :mod:`~trove.services.authz.policy` —— 策略唯一实现(主体模型与判定规则)。
- :mod:`~trove.services.authz.enforcer` —— 执行层强制点(执行 SQL 前的最后一米)。

字段级脱敏 ``Masker`` 见设计文档
``2026-09-28-agent-identity-masking-design.md`` 的 P4。
"""

from trove.services.authz.enforcer import AuthzDecision, Authorizer
from trove.services.authz.policy import (
    Policy,
    Principal,
    principal_from_wire,
    principal_to_wire,
)

__all__ = [
    "AuthzDecision",
    "Authorizer",
    "Policy",
    "Principal",
    "principal_from_wire",
    "principal_to_wire",
]
