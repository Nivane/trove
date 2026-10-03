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
    _unwrap_double_cast,
    _write_pending,
    regen_examples,
    regen_metrics,
)
from trove.services.kb.provenance import body_edited

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

    def test_generator_owned_share_shapes(self):
        """条件占比两形 × 两代形态(无 CAST / CAST AS DOUBLE)都认。

        形态换代后旧盘上的无 CAST 度量同属生成器所有:分类器不认它就会
        把它当人工条目(报错或冻结),重建面只剩一半。双向都钉死。
        """
        datasets = {"loan": {"loan_id", "status", "amount"}, "client": {"client_id"}}
        plain_count = (
            "SUM(CASE WHEN loan.status = 'A' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)")
        cast_count = (
            "CAST(SUM(CASE WHEN loan.status = 'A' THEN 1 ELSE 0 END) AS DOUBLE)"
            " * 100.0 / COUNT(*)")
        plain_measure = (
            "SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END)"
            " * 100.0 / NULLIF(SUM(loan.amount), 0)")
        cast_measure = (
            "CAST(SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END)"
            " AS DOUBLE) * 100.0 / NULLIF(SUM(loan.amount), 0)")
        for expr in (plain_count, cast_count, plain_measure, cast_measure):
            assert _is_generator_owned(expr, datasets), expr
        # 大小写/空白不敏感(旧盘可能被人工编辑过排版)
        assert _is_generator_owned(
            "cast( SUM(CASE WHEN loan.status = 'A' THEN 1 ELSE 0 END) as double )"
            " * 100.0 / COUNT(*)", datasets)

    def test_unwrap_double_cast_only_strips_outermost(self):
        """只剥表达式**起始处**那一层 CAST;剥完其余部分逐字保留。"""
        assert _unwrap_double_cast("CAST(SUM(loan.amount) AS DOUBLE)") == "SUM(loan.amount)"
        # 生成器的真实摆法:CAST 只包分子,后面还有算式
        assert _unwrap_double_cast(
            "CAST(SUM(loan.amount) AS DOUBLE) * 100.0 / COUNT(*)") == (
            "SUM(loan.amount) * 100.0 / COUNT(*)")
        # 只剥一层(嵌套 CAST 的内层原样留着)
        assert _unwrap_double_cast("CAST(CAST(x AS DOUBLE) AS DOUBLE)") == "CAST(x AS DOUBLE)"
        assert _unwrap_double_cast("SUM(loan.amount)") == "SUM(loan.amount)"
        assert _unwrap_double_cast("") == ""
        # CAST 不在起始处 / 收尾不是 AS DOUBLE → 不剥
        assert _unwrap_double_cast("100.0 * CAST(SUM(x) AS DOUBLE) / COUNT(*)") == (
            "100.0 * CAST(SUM(x) AS DOUBLE) / COUNT(*)")
        assert _unwrap_double_cast("CAST(SUM(x) AS INTEGER)") == "CAST(SUM(x) AS INTEGER)"
        # 括号不配对 / 内层为空 → 不剥
        assert _unwrap_double_cast("CAST(SUM(x AS DOUBLE)") == "CAST(SUM(x AS DOUBLE)"
        assert _unwrap_double_cast("CAST( AS DOUBLE)") == "CAST( AS DOUBLE)"
        # 引号里的括号不算数
        assert _unwrap_double_cast(
            "CAST(SUM(CASE WHEN a = ')' THEN 1 ELSE 0 END) AS DOUBLE)") == (
            "SUM(CASE WHEN a = ')' THEN 1 ELSE 0 END)")

    def test_cast_wrapped_non_share_shape_not_owned(self):
        """剥壳不扩大认领面:MAX / 比值形态包 CAST 也不是生成器产物。"""
        datasets = {"loan": {"loan_id", "amount"}}
        assert not _is_generator_owned("CAST(MAX(loan.amount) AS DOUBLE)", datasets)
        assert not _is_generator_owned(
            "CAST(SUM(loan.amount) / COUNT(*) AS DOUBLE)", datasets)

    def test_share_shape_not_owned_when_column_undeclared(self):
        """占比形状但表/列不在声明内 → 不认(人工度量不得被误删)。"""
        datasets = {"loan": {"loan_id", "status", "amount"}, "client": {"client_id"}}
        assert not _is_generator_owned(
            "SUM(CASE WHEN ghost.status = 'A' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)",
            datasets)
        assert not _is_generator_owned(
            "SUM(CASE WHEN loan.ghost = 'A' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)",
            datasets)
        assert not _is_generator_owned(
            "SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END)"
            " * 100.0 / NULLIF(SUM(loan.ghost), 0)", datasets)

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


def _write_share_kb(tmp_path):
    """含枚举列的 KB:生成器会产出条件占比度量。"""
    (tmp_path / "schema_notes.yml").write_text(yaml.safe_dump({
        "tables": [{
            "name": "loan",
            "description": "loans",
            "columns": [
                {"name": "loan_id", "type": "int", "description": "Loan identifier"},
                {"name": "status", "type": "varchar", "description": "loan contract status",
                 "enums": ["A=contract finished", "B=contract running"]},
                {"name": "amount", "type": "int", "description": "loan amount"},
            ],
        }],
    }, sort_keys=False), encoding="utf-8")
    (tmp_path / "semantics.yml").write_text(yaml.safe_dump({
        "semantic_model": [{
            "name": "t",
            "datasets": [{
                "name": "loan",
                "fields": [{"name": "loan_id"}, {"name": "status"}, {"name": "amount"}],
            }],
            "metrics": [
                {"name": "hand_written",
                 "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                              "expression": "MAX(loan.amount)"}]}},
            ],
        }],
        "version": "0.2.0.dev0",
    }, sort_keys=False), encoding="utf-8")


class TestRegenShareMetrics:
    """占比度量进重建面:生成器产物被分类器认领,重跑零漂移(防两处口径漂移)。"""

    def test_share_metrics_land_and_are_owned(self, tmp_path):
        _write_share_kb(tmp_path)
        doc, _, changed = regen_metrics(tmp_path, "en")
        assert changed
        exprs = [m["expression"]["dialects"][0]["expression"]
                 for m in doc["semantic_model"][0]["metrics"]]
        assert ("CAST(SUM(CASE WHEN loan.status = 'A' THEN 1 ELSE 0 END) AS DOUBLE)"
                " * 100.0 / COUNT(*)") in exprs
        assert ("CAST(SUM(CASE WHEN loan.status = 'A' THEN loan.amount ELSE 0 END)"
                " AS DOUBLE) * 100.0 / NULLIF(SUM(loan.amount), 0)") in exprs
        assert "hand_written" in [m["name"] for m in doc["semantic_model"][0]["metrics"]]

    def test_regeneration_idempotent(self, tmp_path):
        """重跑零改动 = 分类器认得生成器自己的占比产物(不认得就会重复追加)。"""
        _write_share_kb(tmp_path)
        doc, _, _ = regen_metrics(tmp_path, "en")
        (tmp_path / "semantics.yml").write_text(
            yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        _, _, changed = regen_metrics(tmp_path, "en")
        assert not changed

    def test_legacy_plain_share_metrics_migrated_not_frozen(self, tmp_path):
        """迁移期:盘上留着无 CAST 旧形态 → 仍认作生成器产物,重建为 CAST 形态。

        认不出就会走两条错路:当人工条目字面保留(形态不换代)或触发
        「带生成器不会写的键」报错退出。这里两个方向都钉死。
        """
        _write_share_kb(tmp_path)
        doc = yaml.safe_load((tmp_path / "semantics.yml").read_text())
        legacy = [
            {"name": "share of loan records where loan contract status is contract finished",
             "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression":
                                          "SUM(CASE WHEN loan.status = 'A' THEN 1 ELSE 0 END)"
                                          " * 100.0 / COUNT(*)"}]}},
            {"name": "share of loan amount where loan contract status is contract finished",
             "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression":
                                          "SUM(CASE WHEN loan.status = 'A'"
                                          " THEN loan.amount ELSE 0 END)"
                                          " * 100.0 / NULLIF(SUM(loan.amount), 0)"}]}},
        ]
        doc["semantic_model"][0]["metrics"] = legacy + doc["semantic_model"][0]["metrics"]
        (tmp_path / "semantics.yml").write_text(
            yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8")

        rebuilt, _, changed = regen_metrics(tmp_path, "en")
        assert changed
        exprs = [m["expression"]["dialects"][0]["expression"]
                 for m in rebuilt["semantic_model"][0]["metrics"]]
        assert not any(e.startswith("SUM(CASE WHEN") for e in exprs)  # 旧形态全被换掉
        assert ("CAST(SUM(CASE WHEN loan.status = 'A' THEN 1 ELSE 0 END) AS DOUBLE)"
                " * 100.0 / COUNT(*)") in exprs
        assert "hand_written" in [m["name"] for m in rebuilt["semantic_model"][0]["metrics"]]


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
        assert "How many district records are there for each Card type; lowercase text values?" in questions
        assert "How many district records are there for each Card type; lowercase text values.?" not in questions
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


class TestWritePending:
    def test_stamps_files_with_meta(self, tmp_path):
        """带 _meta 的文件落盘必须重盖章 —— 否则机器写入被读成"人改过"。

        规则原文见 KbService._write_doc:摘要没跟着正文更新,文件下次读回
        就会被判成"人改过",而"哪些是人的编辑"是三方合并的分界依据。
        """
        _write_examples(tmp_path)
        path = tmp_path / "examples.yml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc["_meta"] = {
            "format": 1,
            "generator": "memory",
            "trove": "0.0.0",
            "digest": "sha256:" + "0" * 64,
            "generated_at": "2026-01-01T00:00:00+00:00",
        }
        path.write_text(
            yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        regen_doc, _, _ = regen_examples(tmp_path, "en")
        assert body_edited(regen_doc) is True  # 写前:正文已变,摘要还是旧的
        _write_pending([(path, regen_doc)])

        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert body_edited(written) is False  # 写后:摘要跟随正文
        assert written["_meta"]["generator"] == "regen_kb_generated"
        assert written["_meta"]["format"] == 1

    def test_no_meta_file_stays_unstamped(self, tmp_path):
        """semantics.yml 不带 _meta,落盘不引入来源块(仓内惯例)。"""
        path = tmp_path / "semantics.yml"
        doc = {"semantic_model": [{"name": "x", "datasets": [], "metrics": []}]}
        _write_pending([(path, doc)])
        written = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "_meta" not in written
