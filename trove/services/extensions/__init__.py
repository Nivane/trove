"""扩展面服务(E 批):信封(E1)· 装前试跑(E3)· 影响面回放(E4)· 包与来源(E5)。

本包内的模块只做「读现成资产 + 纯函数判定」——零 LLM、零网络;任何写路径
(确认 / 停用 / 导入)都留在各自资产的服务里,不从这里绕过治理门。

信封:把此前没有名字的「扩展」统一成一个编译产物 —— kind/name/source/state
+ **推导出的** capabilities(非作者声明)+ 节点级挂点 + 来源链 + 响亮列出的
推导不出的引用。消费面:``trove validate``(envelopes 节)与
``trove extensions list|show``。
"""

from trove.services.extensions.envelope import (
    DOMAIN_VERSIONS,
    EFFECTS,
    KINDS,
    STATES,
    TARGETS,
    TIERS,
    Capabilities,
    ExtensionEnvelope,
    Mount,
    MountCatalog,
    Provenance,
    build_decision_envelope,
    build_preset_envelope,
    build_skill_envelope,
    clear_cache,
    derive_check_capabilities,
    digest_files,
)
from trove.services.extensions.sources import (
    collect_assets,
    collect_decision_envelopes,
    collect_preset_envelopes,
    collect_skill_envelopes,
)

__all__ = [
    "DOMAIN_VERSIONS",
    "EFFECTS",
    "KINDS",
    "STATES",
    "TARGETS",
    "TIERS",
    "Capabilities",
    "ExtensionEnvelope",
    "Mount",
    "MountCatalog",
    "Provenance",
    "build_decision_envelope",
    "build_preset_envelope",
    "build_skill_envelope",
    "clear_cache",
    "collect_assets",
    "collect_decision_envelopes",
    "collect_preset_envelopes",
    "collect_skill_envelopes",
    "derive_check_capabilities",
    "digest_files",
]
