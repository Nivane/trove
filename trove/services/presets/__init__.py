"""预设包(presets)—— 数据源接入模板。

公开面只有三样:契约与结果形状(``models``)、加载/版本化/套用(``service``)、
以及套用状态的闭集。``apply`` 的一切产出都是 pending 草稿,绝不生效。
"""

from trove.services.presets.models import (
    PRESET_KEYS,
    STATUSES,
    ApplyItem,
    ApplyReport,
    Preset,
    PresetError,
    parse_preset,
)
from trove.services.presets.service import PresetService

__all__ = [
    "PRESET_KEYS",
    "STATUSES",
    "ApplyItem",
    "ApplyReport",
    "Preset",
    "PresetError",
    "PresetService",
    "parse_preset",
]
