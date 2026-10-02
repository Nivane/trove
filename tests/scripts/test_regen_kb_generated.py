"""regen_kb_generated 一致性脚本测试:分类器 + 端到端(tmp KB,零 LLM)。

脚本的职责是「把存量 KB 里生成器拥有的产物对齐到当前生成器」,所以两条
底线各测一遍:
- **该重建的重建**(噪音名归一、率值 SUM 剔除、问句对齐);
- **不该动的绝不动**(人工 metric / LLM 合成 few-shot / 带人工键的条目)。
"""

import yaml

from scripts.regen_kb_generated import (
    _foreign_keys,
    _is_generator_owned,
    _is_stale_generated,
    regen_examples,
    regen_metrics,
)

_DATASETS = {"loan": {"loan_id", "amount"}, "district": {"district_id", "A10"}}


class TestClassifiers:
    def test_generator_owned_simple_aggregates(self):
        assert _is_generator_owned("SUM(loan.amount)", _DATASETS)
        assert _is_generator_owned("COUNT(loan.loan_id)", _DATASETS)
        assert _is_generator_owned("AVG(EXTRACT(YEAR FROM loan.amount))", _DATASETS)

    def test_not_generator_owned(self):
        # MAX 不在生成器的产出形状里(人工 metric 靠这条存活)
        assert not _is_generator_owned("MAX(loan.amount)", _DATASETS)
        # 未声明的表 / 不在 fields 里的列
        assert not _is_generator_owned("SUM(ghost.amount)", _DATASETS)
        assert not _is_generator_owned("SUM(loan.ghost)", _DATASETS)
        # 复合表达式
        assert not _is_generator_owned("SUM(loan.amount) / COUNT(*)", _DATASETS)

    def test_stale_generated_template_signature(self):
        entry = {"sql": "SELECT SUM(A10) FROM district",
                 "tags": ["district", "A10", "aggregation"]}
        assert _is_stale_generated(entry)
        # 合成条目(tags 描述性/长度不符)→ 不动
        assert not _is_stale_generated(
            {"sql": "SELECT SUM(A10) FROM district", "tags": ["district", "aggregation"]})
        assert not _is_stale_generated(
            {"sql": "SELECT SUM(A10) FROM district", "tags": ["district", "HAVING"]})
        # 多表 SQL 不是生成器形状
        assert not _is_stale_generated(
            {"sql": "SELECT SUM(t.amount) FROM trans t JOIN loan l ON t.a = l.a",
             "tags": ["trans", "A10", "aggregation"]})

    def test_foreign_keys_flags_hand_edits(self):
        assert _foreign_keys({"name": "m", "expression": {}, "custom_extensions": {}}) == [
            "custom_extensions"]
        assert _foreign_keys(
            {"name": "m", "ai_context": {"synonyms": [], "instructions": "x"}}) == [
                "ai_context.instructions"]
        assert _foreign_keys({"name": "m", "ai_context": {"synonyms": []}}) == []


def _write_kb(tmp_path):
    (tmp_path / "schema_notes.yml").write_text(yaml.safe_dump({
        "tables": [{
            "name": "district",
            "description": "districts",
            "columns": [
                {"name": "district_id", "type": "int", "description": "District ID"},
                {"name": "A9", "type": "int",
                 "description": "Number of cities, ranging from 1 to 11."},
                {"name": "A10", "type": "double",
                 "description": "Unemployment rate in 1995, mostly populated with 1% NULL."},
            ],
        }],
    }, sort_keys=False), encoding="utf-8")
    (tmp_path / "semantics.yml").write_text(yaml.safe_dump({
        "semantic_model": [{
            "name": "t",
            "datasets": [{
                "name": "district",
                "fields": [{"name": "district_id"}, {"name": "A9"}, {"name": "A10"}],
            }],
            "metrics": [
                {"name": "total Number of cities, ranging from 1 to 11.",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                              "expression": "SUM(district.A9)"}]}},
                {"name": "total Unemployment rate in 1995",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                              "expression": "SUM(district.A10)"}]}},
                {"name": "hand_written",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                              "expression": "MAX(district.A9)"}]}},
            ],
        }],
        "version": "0.2.0.dev0",
    }, sort_keys=False), encoding="utf-8")


class TestRegenMetrics:
    def test_rename_drop_and_keep(self, tmp_path):
        _write_kb(tmp_path)
        doc, diff, changed = regen_metrics(tmp_path, "en")
        assert changed
        metrics = doc["semantic_model"][0]["metrics"]
        names = [m["name"] for m in metrics]
        # 噪音名归一
        assert "total Number of cities" in names
        # 率值 SUM 剔除(生成器不再产出 → 该条消失,但 AVG 还在)
        assert "total Unemployment rate in 1995" not in names
        assert "average Unemployment rate in 1995" in names
        # 人工 metric 一字不动
        assert "hand_written" in names

    def test_idempotent(self, tmp_path):
        _write_kb(tmp_path)
        doc, _, _ = regen_metrics(tmp_path, "en")
        (tmp_path / "semantics.yml").write_text(
            yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        _, _, changed = regen_metrics(tmp_path, "en")
        assert not changed

    def test_foreign_key_aborts(self, tmp_path):
        _write_kb(tmp_path)
        doc = yaml.safe_load((tmp_path / "semantics.yml").read_text())
        doc["semantic_model"][0]["metrics"][0]["custom_extensions"] = {"x": 1}
        (tmp_path / "semantics.yml").write_text(
            yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        try:
            regen_metrics(tmp_path, "en")
        except SystemExit as e:
            assert "人工加工" in str(e)
        else:  # pragma: no cover
            raise AssertionError("带人工键的生成器条目必须报错退出,而不是静默重建")


def _write_examples(tmp_path):
    (tmp_path / "schema_notes.yml").write_text(yaml.safe_dump({
        "tables": [{
            "name": "district",
            "description": "districts",
            "columns": [
                {"name": "A9", "type": "int",
                 "description": "Number of cities, ranging from 1 to 11."},
                {"name": "A10", "type": "double",
                 "description": "Unemployment rate in 1995"},
                {"name": "A11", "type": "text",
                 "description": "Card type; lowercase text values."},
            ],
        }],
    }, sort_keys=False), encoding="utf-8")
    (tmp_path / "examples.yml").write_text(yaml.safe_dump({
        "examples": [
            {"template": True, "aggregate": True,
             "question": "What is the maximum Number of cities, ranging from 1 to 11.?",
             "sql": "SELECT MAX(A9) FROM district", "tags": ["district", "A9", "aggregation"]},
            # 生成器不再产出的率值 SUM 模板 → 陈旧剔除
            {"template": True, "aggregate": True,
             "question": "What is the total Unemployment rate in 1995?",
             "sql": "SELECT SUM(A10) FROM district", "tags": ["district", "A10", "aggregation"]},
            # 文本列的 GROUP BY 模板(旧问句) → 应被对齐到生成器现措辞
            {"template": True, "aggregate": True,
             "question": "How many district records are there for each Card type?",
             "sql": "SELECT A11, COUNT(*) FROM district GROUP BY A11",
             "tags": ["district", "group", "aggregation"]},
            # LLM 合成条目:与确定性模板同标志,不重建
            {"template": True, "question": "Which districts are largest?",
             "sql": "SELECT district_id FROM district ORDER BY A9 DESC LIMIT 5",
             "tags": ["district", "order"]},
            # 合成条目复用模板 SQL 但 tags 不同(demo 的真实形态):
            # 只按 SQL 对齐会把自然问句换成机械问句 —— 必须原样保留
            {"template": True, "question": "How many cities are there of each type?",
             "sql": "SELECT A11, COUNT(*) FROM district GROUP BY A11",
             "tags": ["district", "aggregation"]},
            # 自动捕获条目:一字不动(即便 SQL 与模板重合)
            {"template": False, "pending": True, "question": "城市最多的地区",
             "sql": "SELECT MAX(A9) FROM district", "tags": ["auto"]},
        ],
    }, sort_keys=False), encoding="utf-8")


def _by_question(doc):
    return {e["question"]: e for e in doc["examples"]}


class TestRegenExamples:
    def test_align_synthetic_untouched(self, tmp_path):
        _write_examples(tmp_path)
        doc, _, changed = regen_examples(tmp_path, "en")
        assert changed
        questions = _by_question(doc)
        # 对齐:问句换成生成器现在的措辞(聚合族 + GROUP BY 族)
        assert "What is the maximum Number of cities?" in questions
        assert "What is the maximum Number of cities, ranging from 1 to 11.?" not in questions
        assert "How many district records are there for each Card type; lowercase text values.?" in questions
        assert "How many district records are there for each Card type?" not in questions
        # 合成的多表条目原样
        assert "Which districts are largest?" in questions
        # 非 template 条目原样(与模板同 SQL 也不动)
        assert "城市最多的地区" in questions
        # 合成条目复用模板 SQL 但 tags 不同 → 自然问句原样保留(不被机械问句覆盖)
        assert "How many cities are there of each type?" in questions

    def test_stale_sum_over_rate_dropped(self, tmp_path):
        """率值列 SUM 模板(生成器不再产出)被剔除,其余保留。"""
        _write_examples(tmp_path)
        doc, _, _ = regen_examples(tmp_path, "en")
        sqls = [e["sql"] for e in doc["examples"]]
        assert "SELECT SUM(A10) FROM district" not in sqls
        assert "SELECT MAX(A9) FROM district" in sqls
        assert len(doc["examples"]) == 5

    def test_idempotent(self, tmp_path):
        _write_examples(tmp_path)
        doc, _, _ = regen_examples(tmp_path, "en")
        (tmp_path / "examples.yml").write_text(
            yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        _, _, changed = regen_examples(tmp_path, "en")
        assert not changed
