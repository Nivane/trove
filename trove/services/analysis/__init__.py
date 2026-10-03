"""分析服务 —— 确定性分解数学 + 编排 + 渲染(语义优先的分析柱)。

分层:
- ``decompose``  纯数学(贡献/ shift-share / 残差),零 I/O;
- ``expr_tree``  metric 表达式 → 组件树骨架 + 比率识别,只读模型;
- ``render``     payload → 图表(瀑布),纯函数;
- ``engine``     编排(唯一 I/O 处,runner 注入)+ ``analysis_payload``。

图内入口仍是 ``trove.workflow.nodes.attribution.make_attribution``
(瘦身为适配器);本包不 import 数据源注册表/连接器,只读姿态由
``tests/services/analysis/test_readonly_posture.py`` 守卫。
"""

from trove.services.analysis.decompose import (
    breakdown_signal,
    contribution,
    num,
    ratio_share,
    residual,
    shift_share,
    signed_children,
)
from trove.services.analysis.engine import (
    AnalysisEngine,
    AnalysisLimits,
    AnalysisOutcome,
    AnalysisRequest,
    analysis_payload,
)
from trove.services.analysis.expr_tree import (
    collect_components,
    metric_components,
    metric_ratio_parts,
)
from trove.services.analysis.render import ratio_waterfall_chart, waterfall_chart

__all__ = [
    "AnalysisEngine",
    "AnalysisLimits",
    "AnalysisOutcome",
    "AnalysisRequest",
    "analysis_payload",
    "breakdown_signal",
    "collect_components",
    "contribution",
    "metric_components",
    "metric_ratio_parts",
    "num",
    "ratio_share",
    "ratio_waterfall_chart",
    "residual",
    "shift_share",
    "signed_children",
    "waterfall_chart",
]
