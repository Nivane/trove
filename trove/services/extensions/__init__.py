"""扩展面服务 —— 信封(E1 车道)与资产包(E5 车道)的公共包。

本文件保持**最小**:只做 re-export,不放逻辑。E5 车道的内容在
:mod:`trove.services.extensions.pack`(包的形态与校验链);
导出 / 导入的资产策略在 ``PresetService.export_pack / import_pack``。
"""

from __future__ import annotations

from trove.services.extensions.pack import (
    FILES_DIR,
    MANIFEST_FILE,
    PACK_SCHEMA,
    PACK_STATUSES,
    LoadedPack,
    PackError,
    PackInvalid,
    PackItem,
    PackManifest,
    PackReport,
    PackSchemaTooNew,
    PackTampered,
    PackedFile,
    kind_of,
    read_pack,
    sha256_hex,
    write_pack,
)

__all__ = [
    "FILES_DIR",
    "MANIFEST_FILE",
    "PACK_SCHEMA",
    "PACK_STATUSES",
    "LoadedPack",
    "PackError",
    "PackInvalid",
    "PackItem",
    "PackManifest",
    "PackReport",
    "PackSchemaTooNew",
    "PackTampered",
    "PackedFile",
    "kind_of",
    "read_pack",
    "sha256_hex",
    "write_pack",
]
