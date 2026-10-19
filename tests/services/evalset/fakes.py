"""测试写入 helper——只走 EpisodeStore 的公开写方法，不碰私有 schema。"""
from __future__ import annotations

from pathlib import Path

from trove.services.memory.episode import EpisodeStore
from trove.services.memory.models import MemoryScope


async def seed_episodes(path: Path, rows: list[dict]) -> None:
    store = EpisodeStore(path)
    try:
        for row in rows:
            # EpisodeStore.record 的修正史按 list[str] 存(嵌入文本要 join),
            # 测试行允许给任意形状的修正记录,这里统一成字符串。
            corrections = [str(c) for c in (row.get("corrections") or [])]
            await store.record(
                MemoryScope(datasource="demo", user_id="u1"),
                question=row["question"],
                sql=row.get("sql", ""),
                verdict=row.get("verdict", ""),
                correction_history=corrections,
            )
    finally:
        await store.dispose()
