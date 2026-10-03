"""检索权威分:条目治理状态 → 排序偏置(学 Databricks Ontology 的 curation 加权)。

``authority ∈ [-1, 1]``,由条目 payload 里的治理字段**确定性派生** ——
不写 YAML、不依赖用户输入,和 ``governance_of`` 的推断同源(参考示例的
payload 在镜像装载时已挂上 ``status`` 等治理字段,见
``kb/service.py`` 的 examples.yml 分支)。融合时 ``recall`` 按 α(默认
0.1,配置 ``rrf_authority_alpha``)把它加到归一化 RRF 分上:单条最多移动
±α,足以让同检索分里人工背书的资产优先,又不足以盖过相关性本身。

**为什么只给 example/template 打分**:

- lesson 的下游 ``_rank_lessons`` 已有按净票数的乘性加权
  (``1 + 0.25·votes`` 再乘新鲜度)——同一信号再打一遍分是数两遍;
- term/metric/entity 没有认证层级,语义模型的权威性由编译器本身把关
  (未声明的组件根本编译不过,不靠排序补救)。

``deprecated`` 取负值:退役资产**应被压下去**而不是消失 —— 它仍是文件里
的记录,只是不再该冒充当前口径。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from trove.services.kb.governance import CERTIFIED, DEPRECATED

#: 有认证层级的条目 kind(examples.yml 出来的两类)。
_AUTHORITATIVE_KINDS = ("example", "template")

#: 权威分权重 α 的默认值(配置缺省时;显式 0 = 关闭)。
AUTHORITY_ALPHA = 0.1


def authority_of(kind: str, payload: Mapping[str, Any]) -> float:
    """条目的权威分:certified +1 / deprecated -1 / 其余 0。

    ``status`` 由 ``kb.governance.governance_of`` 在镜像装载时推断并注入
    payload(大小写已归一、certified 必须有人背书),这里只做值到权重的
    映射,不再重新判断 —— 两处各判一次,规则一旦漂移,输出会在"谁读的"
    之间分叉。
    """
    if kind not in _AUTHORITATIVE_KINDS:
        return 0.0
    status = str(payload.get("status") or "")
    if status == CERTIFIED:
        return 1.0
    if status == DEPRECATED:
        return -1.0
    return 0.0
