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


class TestBirdCsvEdgeCases:
    """BIRD 官方 CSV 的真实形态:column_name 兜底、非文本列的单位/格式说明、全角冒号。"""

    def test_column_name_falls_back_when_description_empty(self, tmp_path):
        """district.csv 的 A4:描述只写在 column_name 列,column_description 为空。"""
        (tmp_path / "district.csv").write_text(
            "original_column_name,column_name,column_description,data_format,value_description\n"
            "A4,number of inhabitants,,text,\n"
            "A11,average salary,average salary,integer,\n",
            encoding="utf-8",
        )
        docs = load_docs_tables(tmp_path)
        assert docs["district"]["A4"]["description"] == "number of inhabitants"
        assert docs["district"]["A11"]["description"] == "average salary"

    def test_unit_and_format_notes_are_not_enums(self, tmp_path):
        """数值/日期列的 value_description 是单位或格式说明(unit：US dollar、
        in the form YYMMDD),不是枚举——只有文本列的才作枚举导入。"""
        (tmp_path / "loan.csv").write_text(
            "original_column_name,column_description,data_format,value_description\n"
            "amount,approved amount,integer,unit：US dollar\n"
            "date,approval date,date,in the form YYMMDD\n"
            "status,repayment status,text,'A' stands for contract finished\n",
            encoding="utf-8",
        )
        docs = load_docs_tables(tmp_path)
        assert docs["loan"]["amount"]["enums"] == []
        assert docs["loan"]["date"]["enums"] == []
        assert docs["loan"]["status"]["enums"] == ["'A' stands for contract finished"]

    def test_full_width_colon_normalized_to_eq(self, tmp_path):
        """client.gender 的 value_description 用全角冒号(F：female),归一成 = 便于解析。"""
        (tmp_path / "client.csv").write_text(
            "original_column_name,column_description,data_format,value_description\n"
            'gender,gender,text,"F：female \nM：male"\n',
            encoding="utf-8",
        )
        docs = load_docs_tables(tmp_path)
        assert docs["client"]["gender"]["enums"] == ["F=female\nM=male"]
