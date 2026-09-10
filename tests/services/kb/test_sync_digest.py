"""陈旧判定 = 内容摘要,不再是 mtime(C3,design §5 第 7 条)。

改前必红:mtime 相同、内容不同 → 镜像**必须**重新同步。改造前判据是
`row[0] == yml.stat().st_mtime`,于是 `cp -p` / `rsync -t` / 挂载层缓存 /
备份还原都能让一份改过的文件保持 mtime,镜像停在旧内容上,而下游拿它当
"当前 KB"用 —— 这不是精度问题,是判了一个代理指标。
"""

from __future__ import annotations

import hashlib
import json
import os

from trove.services.kb.provenance import dump_asset
from trove.services.kb.service import KbService


def _notes(description: str) -> dict:
    return {
        "tables": [{
            "name": "loan",
            "description": description,
            "columns": [{"name": "amount", "description": "金额", "enums": []}],
            "metrics": [],
        }],
    }


def _write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_asset(doc, "kb_init"), encoding="utf-8")


async def _mirrored_description(kb, ds="demo") -> str:
    """镜像里那条 table 条目的 description(读镜像,不是读 YAML)。"""
    rows = await kb._rows(
        "SELECT payload FROM kb_items WHERE datasource = ? AND kind = 'table'", (ds,),
    )
    return json.loads(rows[0]["payload"])["description"]


class TestDigestPredicate:
    async def test_same_mtime_different_content_resyncs(self, tmp_path):
        """mtime 相同、内容不同 → 必须重新同步(改前必红的那条)。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        _write(notes, _notes("第一版说明"))
        await kb.force_sync("demo")
        assert await _mirrored_description(kb) == "第一版说明"

        # 模拟 cp -p / rsync -t / 挂载层缓存:内容换了,mtime 按原样写回
        stat = notes.stat()
        _write(notes, _notes("人手改过的说明"))
        os.utime(notes, (stat.st_atime, stat.st_mtime))

        # 走懒同步(判据真正生效的那条路);force_sync 按定义无条件重读
        await kb.ensure_synced("demo")
        assert await _mirrored_description(kb) == "人手改过的说明", (
            "mtime 相等不该让同步跳过 —— 判据是内容摘要"
        )

    async def test_unchanged_content_skips_resync(self, tmp_path):
        """内容没变 → 跳过(判据换成摘要,不能变成"每次都重同步")。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        _write(notes, _notes("稳定说明"))
        await kb.force_sync("demo")

        calls: list[str] = []

        async def _spy(datasource, source_file, entries):
            calls.append(source_file)

        kb._index_vectors_for_file = _spy  # type: ignore[method-assign]
        os.utime(notes, (1, 1))  # 只动 mtime,内容不变
        # 用 ensure_synced(懒同步)而不是 force_sync:后者按定义"无条件重读"
        await kb.ensure_synced("demo")
        assert calls == [], "内容没变就该跳过:判据是内容,不是时间"

    async def test_content_change_with_older_mtime_resyncs(self, tmp_path):
        """内容变了且 mtime **变早**(备份还原)→ 也必须重新同步。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        _write(notes, _notes("初版"))
        await kb.force_sync("demo")

        _write(notes, _notes("从备份还原的新内容"))
        os.utime(notes, (1000, 1000))
        await kb.ensure_synced("demo")
        assert await _mirrored_description(kb) == "从备份还原的新内容"

    async def test_digest_and_size_are_recorded(self, tmp_path):
        """表里留下 size/digest:摘要不符时,第一个要看的就是它们。"""
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        _write(notes, _notes("说明"))
        await kb.force_sync("demo")
        row = (await kb._rows(
            "SELECT mtime, size, digest FROM kb_sync WHERE file_path = ?",
            ("demo/schema_notes.yml",),
        ))[0]
        assert row["size"] == notes.stat().st_size
        assert row["digest"] == hashlib.sha256(notes.read_bytes()).hexdigest()
        assert row["mtime"]  # 仍留着,但只作诊断

    async def test_digest_is_of_file_bytes_not_decoded_text(self, tmp_path):
        """摘要是**文件字节**的:CRLF 文件与 LF 文件必须给出不同摘要。

        `read_text` 会把 CRLF 归一成 LF —— 拿"读出来的文本"再编码算摘要,
        磁盘上两份不同的文件会算出同一个值。
        """
        kb = KbService(tmp_path / "proj")
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        notes.parent.mkdir(parents=True, exist_ok=True)
        crlf = dump_asset(_notes("说明"), "kb_init").replace("\n", "\r\n")
        notes.write_bytes(crlf.encode("utf-8"))
        await kb.force_sync("demo")
        row = (await kb._rows("SELECT digest FROM kb_sync", ()))[0]
        assert row["digest"] == hashlib.sha256(crlf.encode("utf-8")).hexdigest()


class TestSyncDigestMigration:
    """旧镜像(2 列 kb_sync)升级:就地补列,**不重建**。

    重建的代价不是重读 YAML,是重算 embedding(计费)。升级不该让每个人
    付一次这笔钱。
    """

    async def _legacy_mirror(self, tmp_path, *, current_mtime: bool):
        """造一个"只差 digest 列"的镜像(多数据源 + FTS 已在,不触发重建)。"""
        import aiosqlite

        kb = KbService(tmp_path / "proj")
        kb.kb_dir.mkdir(parents=True, exist_ok=True)
        notes = kb.kb_dir / "demo" / "schema_notes.yml"
        _write(notes, _notes("说明"))
        mtime = notes.stat().st_mtime if current_mtime else notes.stat().st_mtime - 999
        async with aiosqlite.connect(kb.db_path) as db:
            await db.execute(
                "CREATE TABLE kb_items (id INTEGER PRIMARY KEY, datasource TEXT "
                "NOT NULL, kind TEXT NOT NULL, item_key TEXT NOT NULL, payload TEXT "
                "NOT NULL, source_file TEXT NOT NULL)")
            await db.execute(
                "CREATE VIRTUAL TABLE kb_fts USING fts5(text, datasource UNINDEXED, "
                "kind UNINDEXED, source_file UNINDEXED)")
            await db.execute(
                "CREATE TABLE kb_sync (file_path TEXT PRIMARY KEY, mtime REAL NOT NULL)")
            await db.execute(
                "INSERT INTO kb_items (datasource, kind, item_key, payload, source_file) "
                "VALUES ('demo', 'table', 'loan', '{}', 'schema_notes.yml')")
            await db.execute(
                "INSERT INTO kb_sync (file_path, mtime) VALUES (?, ?)",
                ("demo/schema_notes.yml", mtime),
            )
            await db.commit()
        return kb, notes

    async def _ensure_schema(self, kb):
        import aiosqlite

        async with aiosqlite.connect(kb.db_path) as db:
            await kb._ensure_mirror_schema(db)

    async def test_unchanged_file_gets_digest_backfilled(self, tmp_path):
        """文件没动过 → 摘要就地补上(否则每次升级都触发一轮付费 embedding)。"""
        kb, notes = await self._legacy_mirror(tmp_path, current_mtime=True)
        await self._ensure_schema(kb)
        row = (await kb._rows("SELECT size, digest FROM kb_sync", ()))[0]
        assert row["digest"] == hashlib.sha256(notes.read_bytes()).hexdigest()
        assert row["size"] == notes.stat().st_size

    async def test_changed_file_is_left_for_resync(self, tmp_path):
        """文件动过 → 摘要留空,交给下一次同步正常重读(那正是判据要求的)。"""
        kb, _notes_path = await self._legacy_mirror(tmp_path, current_mtime=False)
        await self._ensure_schema(kb)
        row = (await kb._rows("SELECT size, digest FROM kb_sync", ()))[0]
        assert row["digest"] is None
        assert row["size"] is None

        # 列已经加上:下一次同步能正常写进去(缺列会让 INSERT 直接炸)
        await kb.force_sync("demo")
        row = (await kb._rows("SELECT digest FROM kb_sync", ()))[0]
        assert row["digest"]
        assert await _mirrored_description(kb) == "说明"

    async def test_migration_does_not_reembed_unchanged_files(self, tmp_path):
        """补列不触发重新索引 —— 升级零 embedding 成本。"""
        kb, _notes_path = await self._legacy_mirror(tmp_path, current_mtime=True)
        calls: list[str] = []

        async def _spy(datasource, source_file, entries):
            calls.append(source_file)

        kb._index_vectors_for_file = _spy  # type: ignore[method-assign]
        await kb.ensure_synced("demo")
        assert calls == [], "摘要已补上且与磁盘一致 → 不该重新索引"
