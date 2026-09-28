"""漂移治理 —— 把两套已经写好的检测收敛成一条可消费的证据流。

**要解决的问题**:Trove 有两套漂移检测,各自都对,但输出无法相加,下游
(API / CLI / 门禁 / 影响面)要为每种形状各写一遍;而且两条路都止步于
「报出一组名字」—— 报完就结束,无法区分「新漂移」与「已知未处理」。

**这一层提供**:

- :mod:`~trove.services.drift.models` —— 四级分类学与唯一证据形态
  :class:`DriftItem`;原报告降为 ``detail``,不丢。
- :mod:`~trove.services.drift.adapters` —— 两个检测器 → :class:`DriftItem`
  的映射,含 ``skipped`` 的显式传递(I3:空报告 ≠ 无漂移)。

**四级分类学**(判据:语义模型对物理世界做过的承诺是否仍成立)::

    L1  结构漂移   对象存在          KB schema_notes vs 活库
    L2  引用漂移   引用可达          semantics.yml vs catalog 快照
    L3  语义漂移   取值可锚定        需活库探测
    L4  口径漂移   —(上游逻辑变更)   不检测,仅承接外部声明

L4 **明确不检测**。把它写进分类学而不是留在实现者脑子里,是为了让"为什么
上游改了逻辑我们不知道"这个问题有一个写下来的答案:那是人的声明,不是
可确定性判定的物理事实。
"""

from trove.services.drift.adapters import (
    collect,
    from_schema_drift,
    from_semantic_drift,
)
from trove.services.drift.models import (
    ALLOW,
    BLOCK,
    L1,
    L2,
    L3,
    L4,
    LEVELS,
    RUN_OK,
    RUN_SKIPPED,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_ACKNOWLEDGED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
    WARN,
    DriftItem,
    DriftReport,
    DriftRow,
    GateDecision,
    ImpactSet,
    SemanticChange,
    normalize_subject,
)

__all__ = [
    # models
    "L1", "L2", "L3", "L4", "LEVELS",
    "RUN_OK", "RUN_SKIPPED",
    "SEVERITY_INFO", "SEVERITY_WARNING", "SEVERITY_CRITICAL",
    "STATUS_OPEN", "STATUS_ACKNOWLEDGED", "STATUS_RESOLVED", "STATUS_WAIVED",
    "ALLOW", "WARN", "BLOCK",
    "DriftItem", "DriftReport", "DriftRow", "ImpactSet",
    "SemanticChange", "GateDecision", "normalize_subject",
    # adapters
    "collect", "from_schema_drift", "from_semantic_drift",
]
