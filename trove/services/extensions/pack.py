"""扩展资产包(pack)的形态与校验链 —— 导出物的契约层。

一个 pack 是一个**目录**(冻死的决策 D4,不做默认归档格式):

    <pack>/
      manifest.yml          清单:版本门 + 资产清单(逐文件 sha256) + 来源
      files/<path>          资产文件**原样**镜像(逻辑路径相对 ``.trove/``)

为什么是目录:企业间交换资产时要看的是"改了什么" —— 目录可 diff、可 git、
可 review;逐文件 sha256 让 import 能**点名**报"这个文件被改过",而不是
笼统的"包坏了"。归档只是传输形态,需要时一行 tar。

版本门(``pack_schema``,照 ``StorageSchemaTooNew`` 的精神):高于本代码
认识的版本 → 拒载。旧代码读新包是最静默的一种分歧 —— 清单里多出的字段
没人读,少读的那部分资产看起来"导入成功"了,只是少了一半。因此**版本门
先于逐文件校验**:一个不理解版本的包,不进入"它说了什么"的解读。

本模块是纯文件 + 纯函数:不认识 skills / decisions / presets 的**语义**
(那是 ``PresetService.export_pack / import_pack`` 的事),只认识包的形状
与三条合法路径前缀;写入面(落 pending / 冲撞策略)一律不在这里。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from trove.core.logging import get_logger

logger = get_logger(__name__)

#: 当前代码认识的包 schema 版本。更高 = 拒载(见 :class:`PackSchemaTooNew`)。
PACK_SCHEMA = 1

MANIFEST_FILE = "manifest.yml"
FILES_DIR = "files"

#: manifest 顶层闭键集(与实施稿 §3j 的清单形状一一对应;未知键拒载 ——
#: 与 preset / skill / rule 同一套闭键纪律,写错了没人告诉你的日子不过了)。
MANIFEST_KEYS = (
    "pack_schema", "name", "exported_at", "tool_version",
    "kinds", "files", "provenance",
)
PROVENANCE_KEYS = ("origin", "git_rev")
FILE_KEYS = ("path", "sha256")

#: 资产种类 —— 逻辑路径的形状约定(``kind_of`` 是唯一判据)。
KINDS = ("skills", "decisions", "presets")

#: provenance.origin 的两种形态:本机导出 / 从另一份包再导出(可追溯来源)。
_ORIGIN_RE = re.compile(r"^(local|export:[A-Za-z0-9][A-Za-z0-9._-]*)$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ── 异常 ─────────────────────────────────────────────────


class PackError(Exception):
    """包层面的错误(读写 / 校验)。"""


class PackInvalid(PackError):
    """清单结构不合法(闭键集 / 类型 / 路径安全性)—— 拒载。"""


class PackSchemaTooNew(PackError):
    """包比代码新 —— 拒载(:class:`StorageSchemaTooNew` 的同款精神)。

    与存储版本门同一条理由:旧代码读新格式**不报错**、只是悄悄少读几样
    东西,而"看起来成功"正是最不可接受的结局。
    """

    def __init__(self, manifest_version: int, supported: int = PACK_SCHEMA) -> None:
        self.manifest_version = manifest_version
        self.supported = supported
        super().__init__(
            f"pack_schema {manifest_version} 高于本代码支持的 {supported} —— "
            "拒载(旧代码读新包会静默丢失它不认识的资产;请升级 trove 后重试)")


class PackTampered(PackError):
    """逐文件 sha256 对不上(内容被改 / 文件缺失)—— 点名到文件。"""

    def __init__(self, missing: list[str] | tuple[str, ...] = (),
                 modified: list[str] | tuple[str, ...] = ()) -> None:
        self.missing = tuple(missing)
        self.modified = tuple(modified)
        parts: list[str] = []
        if self.missing:
            parts.append(f"文件缺失: {', '.join(self.missing)}")
        if self.modified:
            parts.append(f"内容被改: {', '.join(self.modified)}")
        super().__init__("包校验失败 —— " + "; ".join(parts) + "(拒载,未落任何资产)")


# ── 小工具 ───────────────────────────────────────────────


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_tool_version() -> str:
    """导出器版本号(``trove.__version__``);取不到就空字符串 —— 不编。"""
    try:
        import trove

        return str(getattr(trove, "__version__", "") or "")
    except Exception:  # noqa: BLE001 — 版本号缺失不该让导出失败
        return ""


def git_rev_for(path: str | Path) -> str:
    """``path`` 所在 git 仓库的短 HEAD;不在仓库 / 无 git → ``""``。

    来源必须**如实**:拿不到就空着,不编一个看起来像版本号的字符串。
    v1 只取 HEAD,不取工作区状态 —— 逐文件 sha256 才是内容的事实源,
    ``git_rev`` 是给"这份包从哪个提交来的"提供一个可回查的锚点。
    """
    d = Path(path).resolve()
    root: Path | None = None
    while True:
        if (d / ".git").exists():
            root = d
            break
        if d.parent == d:
            break
        d = d.parent
    if root is None:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def is_valid_relpath(path: str) -> bool:
    """逻辑路径必须相对、无 ``..``/空段/反斜杠 —— 包的写入面不得越出包根。"""
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        return False
    return all(p and p not in (".", "..") for p in path.split("/"))


def _is_skill_file(name: str) -> bool:
    return name == "SKILL.md" or (name.startswith("SKILL.") and name.endswith(".md"))


def kind_of(path: str) -> str | None:
    """逻辑路径 → 资产种类;认不出 → ``None``(import 会点名拒绝该文件)。

    三条合法形状(与导出面的收集规则一一对应):
    ``skills/<name>/SKILL.md``(含 ``SKILL.<lang>.md`` 覆盖)、
    ``kb/<ds>/decisions.yml``、``presets/<name>/preset.yml``。
    """
    parts = path.split("/")
    if len(parts) != 3:
        return None
    head, mid, tail = parts
    if head == "skills" and _is_skill_file(tail):
        return "skills"
    if head == "kb" and tail == "decisions.yml":
        return "decisions"
    if head == "presets" and tail == "preset.yml":
        return "presets"
    return None


def _as_text(value: Any) -> str:
    """YAML 时间戳会被 safe_load 解析成 ``datetime`` —— 归一回字符串。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


# ── 清单 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PackedFile:
    """清单里的一条文件记录。``size`` 只在读回时补齐,不进 manifest。"""

    path: str
    sha256: str
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class PackManifest:
    """``manifest.yml`` 的内存形态(结构已校验)。"""

    name: str
    files: tuple[PackedFile, ...]
    pack_schema: int = PACK_SCHEMA
    exported_at: str = ""
    tool_version: str = ""
    origin: str = "local"
    git_rev: str = ""

    @property
    def kinds(self) -> tuple[str, ...]:
        """资产种类 = 从文件路径**推导**(声明与清单必须一致,见 from_dict)。"""
        return tuple(sorted({k for k in (kind_of(f.path) for f in self.files) if k}))

    def file(self, path: str) -> PackedFile | None:
        return next((f for f in self.files if f.path == path), None)

    def to_dict(self) -> dict[str, Any]:
        """写盘形状 —— 键序 = 实施稿 §3j 的清单(人读的 diff 要对齐)。"""
        return {
            "pack_schema": self.pack_schema,
            "name": self.name,
            "exported_at": self.exported_at,
            "tool_version": self.tool_version,
            "kinds": list(self.kinds),
            "files": [f.to_dict() for f in self.files],
            "provenance": {"origin": self.origin, "git_rev": self.git_rev},
        }

    @classmethod
    def from_dict(cls, data: Any) -> PackManifest:
        """校验 + 读入。任何结构问题一律 :class:`PackInvalid` /
        :class:`PackSchemaTooNew`,消息必须说清**哪一条不对**。"""
        if not isinstance(data, dict):
            raise PackInvalid(
                f"{MANIFEST_FILE} 顶层必须是映射,得到 {type(data).__name__}")
        unknown = sorted(str(k) for k in data if k not in MANIFEST_KEYS)
        if unknown:
            raise PackInvalid(
                f"manifest: 未知键 {', '.join(unknown)};"
                f"合法键: {', '.join(MANIFEST_KEYS)}")
        missing = [k for k in MANIFEST_KEYS if k not in data]
        if missing:
            raise PackInvalid(f"manifest: 缺少必填键 {', '.join(missing)}")

        schema = data["pack_schema"]
        if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
            raise PackInvalid(
                f"manifest: pack_schema 必须是 >=1 的整数,得到 {schema!r}")
        if schema > PACK_SCHEMA:
            raise PackSchemaTooNew(schema)

        name = str(data["name"] or "")
        if not _NAME_RE.match(name):
            raise PackInvalid(f"manifest: 包名不合法: {name!r}")

        provenance = data["provenance"]
        if not isinstance(provenance, dict):
            raise PackInvalid("manifest: provenance 必须是映射(来源必须可查)")
        unknown_prov = sorted(str(k) for k in provenance if k not in PROVENANCE_KEYS)
        if unknown_prov:
            raise PackInvalid(
                f"manifest.provenance: 未知键 {', '.join(unknown_prov)};"
                f"合法键: {', '.join(PROVENANCE_KEYS)}")
        origin = str(provenance.get("origin") or "")
        if not _ORIGIN_RE.match(origin):
            raise PackInvalid(
                f"manifest.provenance.origin {origin!r} 不合法"
                "(合法形态: local | export:<pack>)")

        raw_files = data["files"]
        if not isinstance(raw_files, list) or not raw_files:
            raise PackInvalid("manifest: files 必须是非空列表(空包没有意义)")
        packed: list[PackedFile] = []
        seen: set[str] = set()
        for i, entry in enumerate(raw_files):
            where = f"files[{i}]"
            if not isinstance(entry, dict):
                raise PackInvalid(f"manifest.{where} 必须是映射")
            unknown_f = sorted(str(k) for k in entry if k not in FILE_KEYS)
            if unknown_f:
                raise PackInvalid(
                    f"manifest.{where}: 未知键 {', '.join(unknown_f)};"
                    f"合法键: {', '.join(FILE_KEYS)}")
            path = str(entry.get("path") or "")
            if not is_valid_relpath(path):
                raise PackInvalid(f"manifest.{where}.path 不是安全相对路径: {path!r}")
            digest = str(entry.get("sha256") or "")
            if not _SHA256_RE.match(digest):
                raise PackInvalid(
                    f"manifest.{where}.sha256 不是 64 位十六进制: {digest!r}")
            if path in seen:
                raise PackInvalid(f"manifest: 路径重复: {path}")
            seen.add(path)
            packed.append(PackedFile(path=path, sha256=digest))

        manifest = cls(
            name=name,
            files=tuple(packed),
            pack_schema=schema,
            exported_at=_as_text(data.get("exported_at")),
            tool_version=_as_text(data.get("tool_version")),
            origin=origin,
            git_rev=_as_text(provenance.get("git_rev")),
        )
        if not manifest.exported_at:
            raise PackInvalid("manifest: exported_at 不能为空(导出时间必须可查)")
        if not manifest.tool_version:
            raise PackInvalid("manifest: tool_version 不能为空(导出工具版本必须可查)")

        # kinds 是**推导物**:声明与清单不一致 = 清单自相矛盾,拒载。
        declared = data["kinds"]
        if not isinstance(declared, list):
            raise PackInvalid("manifest: kinds 必须是列表")
        if {str(k) for k in declared} != set(manifest.kinds):
            raise PackInvalid(
                f"manifest: kinds {sorted(str(k) for k in declared)} 与 files "
                f"推导出的 {list(manifest.kinds)} 不一致")
        return manifest


# ── 读 / 写 ──────────────────────────────────────────────


@dataclass
class LoadedPack:
    """读回并**已通过校验**的包:清单 + 逐文件字节(与 sha256 全等)。"""

    root: Path
    manifest: PackManifest
    files: dict[str, bytes]
    extra: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "manifest": self.manifest.to_dict(),
            "files": sorted(self.files),
            "extra": list(self.extra),
        }


def _extra_files(root: Path, declared: set[str]) -> tuple[str, ...]:
    """``files/`` 下未被清单列出的文件 —— 不导入,但要报出来(不是包的一部分)。"""
    base = root / FILES_DIR
    if not base.is_dir():
        return ()
    out = [
        p.relative_to(base).as_posix()
        for p in sorted(base.rglob("*")) if p.is_file()
    ]
    return tuple(p for p in out if p not in declared)


def read_pack(src: str | Path) -> LoadedPack:
    """读一个包并做完整校验:结构 → 版本门 → 逐文件 sha256。

    任何一步失败都**不返回半成品**:包级失败宁可整个拒载(未落任何资产),
    也不做"能读多少读多少"的宽容 —— 半读的包在接收方看起来和完整包一样。
    """
    root = Path(src)
    manifest_path = root / MANIFEST_FILE
    if not manifest_path.is_file():
        raise PackInvalid(f"不是扩展包:缺少 {MANIFEST_FILE}({root})")
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PackInvalid(f"{MANIFEST_FILE} 解析失败: {exc}") from exc
    manifest = PackManifest.from_dict(data)

    files: dict[str, bytes] = {}
    missing: list[str] = []
    modified: list[str] = []
    for entry in manifest.files:
        path = root / FILES_DIR / entry.path
        if not path.is_file():
            missing.append(entry.path)
            continue
        data_b = path.read_bytes()
        if sha256_hex(data_b) != entry.sha256:
            modified.append(entry.path)
            continue
        files[entry.path] = data_b
    if missing or modified:
        raise PackTampered(missing=missing, modified=modified)
    return LoadedPack(root=root, manifest=manifest, files=files,
                      extra=_extra_files(root, set(files)))


def write_pack(
    dest: str | Path,
    *,
    name: str,
    files: Mapping[str, bytes],
    origin: str = "local",
    git_rev: str = "",
    tool_version: str = "",
    exported_at: str = "",
) -> PackManifest:
    """把一批资产字节写成 pack 目录(manifest + ``files/`` 逐字节镜像)。

    写入面保持三条纪律:①空包拒写(没有东西可打包时"成功"比失败更糟);
    ②目标目录非空拒写(导出不该悄悄覆盖一份已有的包);③路径逐条过
    :func:`is_valid_relpath`(包的写入面不得越出包根)。
    """
    dest = Path(dest)
    if not files:
        raise PackError("导出物为空 —— 没有可打包的资产(拒绝生成空包)")
    if not _NAME_RE.match(name or ""):
        raise PackError(f"包名不合法: {name!r}(规范: ^[a-z0-9][a-z0-9._-]*$)")
    if not _ORIGIN_RE.match(origin or ""):
        raise PackError(f"origin 不合法: {origin!r}(合法形态: local | export:<pack>)")
    if dest.exists() and not dest.is_dir():
        raise PackError(f"目标不是目录: {dest}")
    if dest.is_dir() and any(dest.iterdir()):
        raise PackError(f"目标目录非空,拒绝覆盖: {dest}(换一个空目录,或先移走)")

    packed: list[PackedFile] = []
    for rel in sorted(files):
        if not is_valid_relpath(rel):
            raise PackError(f"非法相对路径: {rel!r}")
        data = files[rel]
        if not isinstance(data, (bytes, bytearray)):
            raise PackError(f"{rel}: 只接受字节(导出物 = 文件原样)")
        blob = bytes(data)
        target = dest / FILES_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        packed.append(PackedFile(path=rel, sha256=sha256_hex(blob), size=len(blob)))

    manifest = PackManifest(
        name=name, files=tuple(packed), pack_schema=PACK_SCHEMA,
        exported_at=exported_at or now_iso(),
        tool_version=tool_version or current_tool_version(),
        origin=origin, git_rev=git_rev,
    )
    dest.mkdir(parents=True, exist_ok=True)
    (dest / MANIFEST_FILE).write_text(
        yaml.safe_dump(manifest.to_dict(), allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return manifest


# ── 账本(导出 / 导入的报告形状) ─────────────────────────

#: 导出 / 导入逐条状态的闭集。``conflict`` / ``invalid`` 是**拒载**姿态:
#: 该资产什么都没写;调用方(CLI)据此给退出码。
PACK_STATUSES = (
    "exported",     # 已写入包
    "imported",     # 已落 pending(目标为新建)
    "overwritten",  # --force:已用包内版本覆盖(pending 形态)
    "skipped",      # 无需动作(幂等重放 / 未列出的文件)
    "conflict",     # 目标已有同名资产 —— 拒载并点名
    "invalid",      # 内容过不了校验 —— 拒载并点名
)


@dataclass
class PackItem:
    """一条导出 / 导入结果。``reason`` 永远非空(一条说不清的记录只能靠猜)。"""

    section: str          # skills | decisions | presets | pack
    item: str
    status: str           # PACK_STATUSES
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "item": self.item,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class PackReport:
    """一次导出 / 导入的账本(与 ``ApplyReport`` 同一取向:逐条可读)。"""

    action: str = ""              # export | import
    pack: str = ""
    dest: str = ""
    origin: str = ""
    git_rev: str = ""
    items: list[PackItem] = field(default_factory=list)

    def add(self, section: str, item: str, status: str, reason: str = "") -> PackItem:
        if status not in PACK_STATUSES:
            raise ValueError(f"unknown pack status: {status!r}")
        entry = PackItem(section=section, item=str(item), status=status,
                         reason=reason)
        self.items.append(entry)
        return entry

    @property
    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in PACK_STATUSES}
        for i in self.items:
            out[i.status] = out.get(i.status, 0) + 1
        return out

    @property
    def refused(self) -> int:
        """拒载条目数(conflict + invalid)—— CLI 退出码的判据。"""
        c = self.counts
        return c.get("conflict", 0) + c.get("invalid", 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "pack": self.pack,
            "dest": self.dest,
            "origin": self.origin,
            "git_rev": self.git_rev,
            "counts": self.counts,
            "items": [i.to_dict() for i in self.items],
        }

    def render(self) -> str:
        """CLI/REPL 的一屏文本(与 ``validate`` / ``apply`` 的报告渲染同一取向)。"""
        verb = "导出" if self.action == "export" else "导入"
        head = f"{verb}扩展包 {self.pack!r}"
        if self.origin:
            head += f" (origin={self.origin}" + (
                f", git_rev={self.git_rev})" if self.git_rev else ")")
        head += f" → {self.dest}" if self.action == "export" else f" ← {self.dest}"
        lines = [head]
        status_label = {
            "exported": "已导出", "imported": "已导入", "overwritten": "已覆盖",
            "skipped": "跳过", "conflict": "冲突拒载", "invalid": "无效拒载",
        }
        for i in self.items:
            lines.append(f"  [{status_label.get(i.status, i.status)}] "
                         f"{i.section}/{i.item}: {i.reason}")
        c = self.counts
        summary = [f"{c[s]} {status_label[s]}" for s in PACK_STATUSES if c.get(s)]
        lines.append("合计: " + (" · ".join(summary) or "（空）"))
        if self.action == "import":
            lines.append("（导入物一律 pending —— 逐条确认后才生效）")
        return "\n".join(lines)
