"""鉴权测试助手:给图/链路测试一个**真实的**本机身份。

设计 §9.4 记过这次迁移的成本:「``execute_sql`` 默认拒绝会打断现有测试……
测试夹具统一注入 ``Principal(role="admin")``。**这是一次性成本,不能靠默认
放行绕过**(否则 I2 形同虚设)。」本模块就是那笔一次性成本,只付一次。

**这不是「为了让测试过而放行」。** 这些测试模拟的正是 CLI / 嵌入式调用 ——
没有远程身份的本机运行,它在生产里拿到的就是 ``Policy.local_admin()``
(见 ``agent/session._principal_wire`` 的第一条分支)。夹具给的是一条真实
身份,不是一条豁免。

要测**拒绝**路径的测试,显式传 ``principal=None``(或自己构造受限主体)——
那正是 I2 的断言面。
"""

from __future__ import annotations

from typing import Any

from trove.services.authz.policy import Policy, principal_to_wire

__all__ = ["local_admin_principal"]


def local_admin_principal() -> dict[str, Any]:
    """本机可信身份的 wire 形状 —— 可直接塞进 ``WorkflowState(principal=...)``。

    走生产代码(``Policy.local_admin`` + ``principal_to_wire``)而不是在夹具里
    手搓一个 dict:手搓的那份不会跟着生产形态演进,而形态一旦漂移
    (``principal_from_wire`` 认不出),测试会用**没有主体**的身份跑,
    于是每一轮都被拒 —— 失败信息指向 I2,离真正的原因很远。
    """
    return principal_to_wire(Policy.local_admin())
