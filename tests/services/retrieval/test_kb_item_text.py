"""``kb_item_text`` 的表条目文本契约(单一实现,全量索引与增量写入共用)。

table 的文本**只有 JSON 兜底一条路** —— ``fts_item_text("table", …)`` 按
设计恒返回空串。而 schema_notes.yml 是探测产物:未加引号的日期
(``stats.min: 1993-01-01``)会被 YAML 解析成 ``datetime.date``,裸
``json.dumps`` 遇它就抛 —— 一个日期足以让**整份文件**的索引中止
(``index_file`` 在 ``delete_source`` 之前就抛,旧文档原地留下),而检索
看上去一切正常。回归:表条目带日期仍须产出文本。
"""

import datetime
import json

from trove.services.kb.backends.pg_hybrid import PgHybridKbBackend
from trove.services.retrieval.indexer import kb_item_text

# 形状取自真实 schema_notes.yml 的 table 条目(探测输出:stats.min/max 是日期)
TABLE_WITH_DATES = {
    "name": "account",
    "description": "银行账户",
    "columns": [
        {"name": "date", "type": "DATE",
         "stats": {"min": datetime.date(1993, 1, 1), "max": datetime.date(1997, 12, 29)}},
    ],
    "row_count": 4500,
}

TABLE_WITHOUT_DATES = {
    "name": "account",
    "description": "银行账户",
    "columns": [{"name": "account_id", "type": "INTEGER"}],
    "row_count": 4500,
}


def test_table_entry_with_yaml_dates_is_indexable():
    """日期不得让表条目失去文本(此前 TypeError: date is not JSON serializable)。"""
    text = kb_item_text("table", TABLE_WITH_DATES)

    assert text
    assert "account" in text


def test_table_entry_without_dates_keeps_plain_json_text():
    """无日期条目逐字节不变 —— default=str 只救会抛的那批,不改存量文本。"""
    assert kb_item_text("table", TABLE_WITHOUT_DATES) == json.dumps(
        TABLE_WITHOUT_DATES, ensure_ascii=False)


class FakeStore:
    """只记调用:这两个钩子就是 index_file 的全部外部接触面。"""

    def __init__(self):
        self.deleted: list[tuple[str, str]] = []
        self.indexed: list[list] = []

    async def delete_source(self, datasource, source_file):
        self.deleted.append((datasource, source_file))

    async def index_many(self, docs):
        self.indexed.append(list(docs))


async def test_index_file_writes_date_carrying_table_entry():
    """文件级:带日期的表条目要真的落成文档,而不是整份文件静默不索引。"""
    store = FakeStore()
    backend = PgHybridKbBackend(kb=None, store=store)

    await backend.index_file(
        "mysql_fin", "schema_notes.yml",
        [("table", "account", TABLE_WITH_DATES)],
    )

    assert store.deleted == [("mysql_fin", "schema_notes.yml")]
    assert len(store.indexed) == 1
    assert [d.item_key for d in store.indexed[0]] == ["account"]
    assert store.indexed[0][0].content
