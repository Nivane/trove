"""JSON 安全化:DB 原生值 → ``json.dumps`` 接受的值。

适配器把驱动交回的类型原样透出(MySQL/PG 的 ``Decimal``、``date`` /
``datetime``,JSON 列的 dict/list 等)。**数学路径**自己 float 化不受影响
(``rows_to_map`` / ``rows_to_numden`` / ``num``),但**记录路径**会把这些值
放进 payload —— 2026-10-05 的线上形态就是这条链断在最窄的一环:归因证据行
里的 ``Decimal`` 让 ``SessionStore.save_session`` 的 ``json.dumps`` 抛
TypeError,生成器死在 ``done`` 事件之前,用户看到「流中断」,答案既没送达
也没落库(SSE 侧本有 ``default=str`` 兜底,但落库先炸,轮不到它)。

边界分工:进 payload 的值先过这里(**保真**:Decimal → 数字、date → ISO
字符串);交付/落库边界的 ``json.dumps(default=str)`` 是最后一张网(**绝不
抛**),两者都要有 —— 只靠网会把数字降级成 ``"Decimal('1.5')"`` 字符串,
只靠这里则下一个没见过的类型仍能掐断回答。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def json_safe(value: Any) -> Any:
    """→ JSON 可序列化的等价值(容器递归)。

    - None / str / bool / int 原样(json 原生类型);
    - float 原样,非有限(NaN/±inf)→ ``None`` —— json 会把它写成 ``NaN``
      这种非法 JSON 字面量,静默产出前端 ``JSON.parse`` 拒绝的负载;
    - ``Decimal`` → float(非有限 → ``None``);
    - ``date`` / ``datetime`` → ISO 字符串;
    - list/tuple → 逐项;dict → 逐值(键 ``str`` 化);
    - 其余 → ``str(value)``(兜底,不抛)。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return str(value)
