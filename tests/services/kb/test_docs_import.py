"""Docs import tests — official documentation files → KB column descriptions.

/kb init --docs <dir>:每个文件一张表(文件 stem = 表名),列名容错:
BIRD 官方 CSV、通用 CSV、JSON/YAML 列表都可导入。
"""

import json

import yaml

from trove.services.kb.docs_import import load_docs_tables


class TestLoadDocsTables:
    def test_csv_bird_headers(self, tmp_path):
        (tmp_path / "students.csv").write_text(
            "original_column_name,column_description,value_description\n"
            'grade,成绩,"A=优秀; B=良好"\n'
            'county,所在县,\n',
            encoding="utf-8",
        )
        docs = load_docs_tables(tmp_path)
        assert docs["students"]["grade"]["description"] == "成绩"
        assert docs["students"]["grade"]["enums"] == ["A=优秀; B=良好"]
        assert docs["students"]["county"]["description"] == "所在县"

    def test_csv_generic_headers(self, tmp_path):
        (tmp_path / "courses.csv").write_text(
            "name,description\n"
            'title,课程名\n',
            encoding="utf-8",
        )
        docs = load_docs_tables(tmp_path)
        assert docs["courses"]["title"]["description"] == "课程名"
        assert docs["courses"]["title"]["enums"] == []

    def test_json_and_yaml_files(self, tmp_path):
        (tmp_path / "students.json").write_text(
            json.dumps([
                {"name": "grade", "description": "成绩", "enums": []},
            ]),
            encoding="utf-8",
        )
        (tmp_path / "courses.yaml").write_text(
            yaml.safe_dump([
                {"name": "title", "description": "课程名", "enums": ["X=数学"]},
            ]),
            encoding="utf-8",
        )
        docs = load_docs_tables(tmp_path)
        assert docs["students"]["grade"]["description"] == "成绩"
        assert docs["courses"]["title"]["description"] == "课程名"
        assert docs["courses"]["title"]["enums"] == ["X=数学"]

    def test_empty_directory(self, tmp_path):
        assert load_docs_tables(tmp_path) == {}

    def test_unknown_columns_skipped(self, tmp_path):
        (tmp_path / "students.csv").write_text(
            "original_column_name,column_description\n"
            'grade,成绩\n'
            ',只有描述没有列名\n',
            encoding="utf-8",
        )
        docs = load_docs_tables(tmp_path)
        assert len(docs["students"]) == 1
