"""身份与权限执行(authz)。

- :mod:`~trove.services.authz.policy` —— 策略唯一实现(主体模型与判定规则)。
- :mod:`~trove.services.authz.enforcer` —— 执行层强制点(执行 SQL 前的最后一米)。
- :mod:`~trove.services.authz.masking` —— 字段级脱敏(结果集后置改写,设计 P4)。
"""

from trove.services.authz.enforcer import AuthzDecision, Authorizer
from trove.services.authz.masking import Masker, MaskingError
from trove.services.authz.policy import (
    Policy,
    Principal,
    principal_from_wire,
    principal_to_wire,
)

__all__ = [
    "AuthzDecision",
    "Authorizer",
    "Masker",
    "MaskingError",
    "Policy",
    "Principal",
    "principal_from_wire",
    "principal_to_wire",
]
