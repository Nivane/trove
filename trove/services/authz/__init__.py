"""身份与权限执行(authz)。

当前只有 :mod:`~trove.services.authz.policy`(策略唯一实现)。
执行层强制点 ``Authorizer`` / 字段级脱敏 ``Masker`` 见设计文档
``2026-09-28-agent-identity-masking-design.md`` 的 P3 / P4。
"""

from trove.services.authz.policy import Policy, Principal

__all__ = ["Policy", "Principal"]
