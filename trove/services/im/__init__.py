"""IM 塑形与投递 —— **中立包**(行动包与 jobs 包都不许 import 对方)。

- :mod:`trove.services.im.shape` — 纯塑形:``CHANNEL_KINDS`` + ``shape_payload``
  (嵌套 IM 外壳模板层表达不了,所以必须是代码)。
- :mod:`trove.services.im.transport` — 可注入 transport + 默认 httpx POST。
"""

from trove.services.im.shape import (
    CHANNEL_KINDS,
    MAX_TEXT,
    channel_kind,
    render_text,
    shape_payload,
    title_of,
)
from trove.services.im.transport import Transport, deliver, post_json

__all__ = [
    "CHANNEL_KINDS",
    "MAX_TEXT",
    "Transport",
    "channel_kind",
    "deliver",
    "post_json",
    "render_text",
    "shape_payload",
    "title_of",
]
