"""Extensions — 扩展资产的**信封面**(设计稿《一切接缝皆契约》支柱一/E1)。

「扩展」在 trove 里此前没有名字:每种资产(skill/规则/preset/…)有自己的
文件契约与读取路径。信封把它们统一成一个编译产物:kind/name/source/state
+ **推导出的** capabilities(非作者声明)+ 节点级挂点 + 来源链 + 响亮列出的
推导不出的引用。消费面:``trove validate``(envelopes 节)与
``trove extensions list|show``。

零 LLM、零网络;纯读。
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
