"""``import_official_semantics.py`` 测试:官方文档 → semantics.yml 两键导入。

这个脚本是「官方数据库文档 → 语义模型」的唯一通道,它的正确性由三条
底线组成,每条都各有一组测试:

1. **切得对**:官方 value_description 四形态(stands for / 引号 / 全角
   冒号 / 裸值)切出与 lint 一致的枚举对,叙述行不产 garbage;
2. **改得准**:只碰 description / enum_display 两个键 —— 越界必须被内置
   结构 diff 断言拦下(退出码 1),而不是靠人眼复查;
3. **落得稳**:幂等(第二次零 diff)、往返非 byte-identical 拒写、对不上
   任何 dataset 的 KB 拒写(退出码 2)——「查不成的绿是假绿」。

全部用 tmp 目录里的 mini KB + mini CSV,零网络零 LLM。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest
import yaml

from scripts.import_official_semantics import (
    _allowed_change,
    _row_value_text,
    apply_official,
    changed_paths,
    merge_enum_display,
    official_enum_pairs,
    read_official_docs,
    read_official_rows,
)

_DUMP_KWARGS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)


def _dump(doc) -> str:
    return yaml.safe_dump(doc, **_DUMP_KWARGS)


# ── mini KB / mini 官方文档 ───────────────────────────────


def _kb_doc() -> dict:
    """三个 dataset:sales 无官方文档(必须一字不动)。"""
    return {
        "semantic_model": [{
            "name": "t",
            "datasets": [
                {
                    "name": "loan",
                    "fields": [
                        {"name": "loan_id", "semantic_role": "key", "datatype": "integer",
                         "ai_context": {"synonyms": ["loan number"]},
                         "description": "unique identifier for each loan"},
                        {"name": "status", "semantic_role": "dimension", "datatype": "text",
                         "enum_display": {"A": "active", "B": "closed"},
                         "ai_context": {"synonyms": []},
                         "description": "status of the loan repayment"},
                        {"name": "amount", "semantic_role": "measure", "datatype": "integer",
                         "ai_context": {"synonyms": []},
                         "description": "total amount of the loan"},
                    ],
                },
                {
                    "name": "district",
                    "fields": [
                        {"name": "district_id", "semantic_role": "key",
                         "datatype": "integer", "description": "unique identifier for a district"},
                        {"name": "A9", "semantic_role": "measure", "datatype": "integer",
                         "description": "Number of cities, ranging from 1 to 11."},
                        {"name": "region", "semantic_role": "dimension", "datatype": "text",
                         "ai_context": {"synonyms": []},
                         "description": "region or area of the district"},
                    ],
                },
                {
                    "name": "sales",
                    "fields": [
                        {"name": "amount", "semantic_role": "measure", "datatype": "integer",
                         "description": "sales amount"},
                    ],
                },
            ],
        }],
        "version": "0.2.0.dev0",
    }


def _write_kb(kb: Path) -> Path:
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "semantics.yml").write_text(_dump(_kb_doc()), encoding="utf-8")
    return kb


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


_HEADER = ["original_column_name", "column_name", "column_description",
           "data_format", "value_description"]


def _write_docs(docs: Path) -> Path:
    _write_csv(docs / "loan.csv", _HEADER, [
        ["loan_id", "loan_id", "the id number identifying the loan", "integer", ""],
        ["status", "status", "repayment status", "text",
         "'A' stands for contract finished, no problems;"
         " 'B' stands for loan not paid"],
        # 非 text 列的值文本是单位注记,不产词表(且 column_description 为空 → 不覆盖)
        ["amount", "amount", "", "integer", "unit：US dollar"],
    ])
    _write_csv(docs / "district.csv", _HEADER, [
        ["district_id", "district_id", "location of branch", "integer", ""],
        # column_description 为空 → 不得覆盖现有描述
        ["A9", "A9", "", "integer", "not useful"],
        # 裸值列表(官方没给释义)→ 新 code 仍应追加(值本身是真的)
        ["region", "region", "", "text", "west Bohemia; east Bohemia"],
    ])
    return docs


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    return _write_kb(tmp_path / "financial")


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    return _write_docs(tmp_path / "fin_database_description")


def _run_main(monkeypatch, kb_dir: Path, docs_dir: Path, *flags: str) -> int:
    from scripts import import_official_semantics as mod

    monkeypatch.setattr(
        sys, "argv",
        ["import_official_semantics.py", "--kb-dir", str(kb_dir),
         "--docs", str(docs_dir), *flags],
    )
    return mod.main()


def _field(kb_dir: Path, dataset: str, name: str) -> dict:
    doc = yaml.safe_load((kb_dir / "semantics.yml").read_text(encoding="utf-8"))
    for ds in doc["semantic_model"][0]["datasets"]:
        if ds["name"] == dataset:
            return next(f for f in ds["fields"] if f["name"] == name)
    raise AssertionError(dataset)


# ── 1. 切得对:官方 value_description 的解析 ────────────────


class TestOfficialEnumPairs:
    """解析器**复用**两处既有实现(deterministic_gen._enum_values /
    lint.parse_enum_values),不新写第三份 —— 这里钉的是「四形态都切得出来,
    叙述行一个都不漏」,以及两个通道切分口径一致。"""

    def test_stands_for_form(self):
        assert official_enum_pairs(
            "'A' stands for contract finished, no problems;\n"
            "'B' stands for contract finished, loan not paid;"
        ) == [("A", "contract finished, no problems"),
              ("B", "contract finished, loan not paid")]

    def test_full_width_colon_form(self):
        """``F：female``(全角冒号)—— 与 ``F=female`` 同义,lint 那支也认。"""
        assert official_enum_pairs("F：female \nM：male") == [("F", "female"), ("M", "male")]

    def test_ascii_equals_and_quoted_colon_forms(self):
        assert official_enum_pairs("A=contract finished\nB=contract running") == [
            ("A", "contract finished"), ("B", "contract running")]
        # 官方实形:引号式一行一个 code(实测 card.type / trans.operation)
        assert official_enum_pairs(
            '"junior": junior class of credit card;\n'
            '"gold": high-level credit card'
        ) == [("junior", "junior class of credit card"),
              ("gold", "high-level credit card")]

    def test_semicolon_separated_entries_on_one_line(self):
        """``;`` 分隔的同行条目必须各归各 —— 不拆会把第二条吞进第一条的 label。"""
        assert official_enum_pairs(
            "'A' stands for contract finished, no problems;"
            " 'B' stands for loan not paid") == [
                ("A", "contract finished, no problems"), ("B", "loan not paid")]

    def test_bare_value_list(self):
        """无 ``=`` 无引号的官方行 → 裸值对(按文档顺序,不重排)。"""
        assert official_enum_pairs("west Bohemia; east Bohemia") == [
            ("west Bohemia", "west Bohemia"), ("east Bohemia", "east Bohemia")]

    def test_three_word_bare_value_via_lint_fallback(self):
        """``_enum_values`` 跳过 3 词裸值(叙述守卫),lint 的切分能把它们捞回来。

        两支解析器的分工:前者严(防 garbage),后者宽(取值域不漏);兜底
        只在前者空手时启用,且仍过一遍「像不像存储值」的守卫。
        """
        assert official_enum_pairs("some other place; yet another spot") == [
            ("some other place", "some other place"),
            ("yet another spot", "yet another spot")]

    def test_narrative_lines_yield_nothing(self):
        """叙述行不得被当成值:整句/带冒号的说明一律跳过。"""
        assert official_enum_pairs("each bank has unique two-letter code") == []
        assert official_enum_pairs(
            '"OWNER" : "USER" : "DISPONENT"\n'
            "commonsense evidence:\n"
            "the account can only have the right to issue permanent orders"
        ) == [("OWNER", "OWNER")]  # 只列值不给义:code 保留,label 回退 code
        assert official_enum_pairs("not useful") == [("not useful", "not useful")]

    def test_punctuation_only_label_is_not_a_definition(self):
        """引号分支剥引号后 label 只剩标点的对不得带进 enum_display。"""
        pairs = official_enum_pairs('"ONE" : "TWO"')
        assert all(":" not in label for _, label in pairs)


# ── 2. 改得准:两键白名单与合并语义 ────────────────────────


class TestMergeEnumDisplay:
    def test_existing_order_kept_official_overrides_labels(self):
        merged, changes, skips = merge_enum_display(
            {"A": "active", "B": "closed"},
            [("A", "contract finished, no problems"), ("B", "loan not paid")])
        assert list(merged) == ["A", "B"]  # 保序
        assert merged["A"] == "contract finished, no problems"
        assert changes == ["A: 'active' → 'contract finished, no problems'",
                           "B: 'closed' → 'loan not paid'"]
        assert skips == []

    def test_official_new_codes_appended(self):
        merged, changes, _ = merge_enum_display(
            {"A": "active"}, [("A", "active"), ("C", "running contract")])
        assert list(merged) == ["A", "C"]
        assert changes == ["+ C: 'running contract'"]

    def test_non_informative_official_does_not_overwrite_existing_label(self):
        """官方只列值(``_"OWNER"_ : "USER"`` 解析出 label=code)时保留原 label。

        覆盖会把 ``owner`` 写回 ``OWNER`` —— 名字里写的是「官方优先」,但
        官方这里并没有给释义,优先的是**有信息的那份**。
        """
        merged, changes, skips = merge_enum_display(
            {"OWNER": "owner", "USER": "user"}, [("OWNER", "OWNER")])
        assert merged == {"OWNER": "owner", "USER": "user"}
        assert changes == []
        assert skips == ["OWNER: 官方仅列值,保留原 label 'owner'"]

    def test_new_code_without_gloss_is_still_appended(self):
        merged, changes, _ = merge_enum_display(None, [("X", "X")])
        assert merged == {"X": "X"} and changes == ["+ X: 'X'"]


class TestApplyOfficial:
    def test_two_key_only_and_expected_values(self, kb, docs):
        before = yaml.safe_load((kb / "semantics.yml").read_text(encoding="utf-8"))
        doc = yaml.safe_load((kb / "semantics.yml").read_text(encoding="utf-8"))
        result = apply_official(doc, read_official_docs(docs))

        assert _field_of(doc, "loan", "loan_id")["description"] == (
            "the id number identifying the loan")
        assert _field_of(doc, "loan", "status")["enum_display"] == {
            "A": "contract finished, no problems", "B": "loan not paid"}
        # 空白 column_description 不覆盖现有描述
        assert _field_of(doc, "district", "A9")["description"] == (
            "Number of cities, ranging from 1 to 11.")
        # 无官方文档的 dataset 一字不动
        assert _field_of(doc, "sales", "amount")["description"] == "sales amount"
        # 新 code 追加且 label 回退 code
        assert _field_of(doc, "district", "region")["enum_display"] == {
            "east Bohemia": "east Bohemia", "west Bohemia": "west Bohemia"}
        # 非 text 列的值文本按注记跳过(有记录,不静默)
        assert any("loan.amount" in n and "非 text" in n for n in result["notes"])

        # 结构 diff 只落在两个键上
        paths = changed_paths(before, doc)
        assert paths and all(_allowed_change(p) for p in paths), paths

    def test_enum_display_key_inserted_at_field_convention_position(self, kb, docs):
        """新 enum_display 不能追加到字段末尾 —— 键序要跟盘上既有字段一致。"""
        doc = yaml.safe_load((kb / "semantics.yml").read_text(encoding="utf-8"))
        apply_official(doc, read_official_docs(docs))
        keys = list(_field_of(doc, "district", "region"))
        assert keys.index("enum_display") < keys.index("ai_context")
        assert keys.index("enum_display") < keys.index("description")

    def test_idempotent_on_second_apply(self, kb, docs):
        """同一份文档套两遍 → 第二遍零变更(合并语义必须收敛)。"""
        official = read_official_docs(docs)
        doc = yaml.safe_load((kb / "semantics.yml").read_text(encoding="utf-8"))
        apply_official(doc, official)
        again = yaml.safe_load(_dump(doc))
        result = apply_official(again, official)
        assert result["lines"] == [] and result["changed_fields"] == 0


def _field_of(doc: dict, dataset: str, name: str) -> dict:
    for ds in doc["semantic_model"][0]["datasets"]:
        if ds["name"] == dataset:
            return next(f for f in ds["fields"] if f["name"] == name)
    raise AssertionError(dataset)


# ── 3. 官方 CSV 读取(含 account.csv 的尾列怪癖)─────────────


class TestOfficialCsvReading:
    def test_extra_unnamed_column_carries_value_text(self, tmp_path):
        """account.csv 的 frequency 行:值语义在未命名的第 6 列。

        只读 value_description 会把该行的值词表整条丢掉。
        """
        _write_csv(tmp_path / "account.csv", [*_HEADER, ""], [
            ["account_id", "account_id", "the id of the account", "integer", "", ""],
            ["frequency", "frequency", "frequency of the acount", "text", "",
             '"POPLATEK MESICNE" stands for monthly issuance'],
        ])
        rows = read_official_rows(tmp_path / "account.csv")
        assert rows["frequency"]["values"] == (
            '"POPLATEK MESICNE" stands for monthly issuance')
        assert official_enum_pairs(rows["frequency"]["values"]) == [
            ("POPLATEK MESICNE", "monthly issuance")]

    def test_row_value_text_merges_named_and_extra(self):
        row = {"original_column_name": "x", "value_description": "a=b",
               None: ["c=d"], "": "e=f"}
        assert _row_value_text(row) == "a=b\nc=d\ne=f"

    def test_stem_maps_to_dataset_name(self, docs):
        assert sorted(read_official_docs(docs)) == ["district", "loan"]

    def test_bom_is_stripped(self, tmp_path):
        """官方文件带 BOM(utf-8-sig);不处理的话第一个列名会带 \\ufeff。"""
        _write_csv(tmp_path / "loan.csv", _HEADER, [
            ["loan_id", "loan_id", "id", "integer", ""]])
        rows = read_official_rows(tmp_path / "loan.csv")
        assert list(rows) == ["loan_id"]

    def test_blank_column_name_rows_skipped(self, tmp_path):
        _write_csv(tmp_path / "loan.csv", _HEADER, [
            ["", "", "orphan note", "text", ""],
            ["loan_id", "loan_id", "id", "integer", ""]])
        assert list(read_official_rows(tmp_path / "loan.csv")) == ["loan_id"]


# ── 4. CLI:退出码 / 幂等 / 拒写 ────────────────────────────


class TestCli:
    def test_dry_run_does_not_write(self, kb, docs, monkeypatch, capsys):
        raw = (kb / "semantics.yml").read_text(encoding="utf-8")
        assert _run_main(monkeypatch, kb, docs) == 0
        out = capsys.readouterr().out
        assert "dry-run" in out and "description:" in out
        assert (kb / "semantics.yml").read_text(encoding="utf-8") == raw

    def test_check_exits_1_on_drift(self, kb, docs, monkeypatch):
        assert _run_main(monkeypatch, kb, docs, "--check") == 1
        # --check 只报不写
        assert "unique identifier for each loan" in (kb / "semantics.yml").read_text(
            encoding="utf-8")

    def test_write_then_second_run_is_idempotent(self, kb, docs, monkeypatch, capsys):
        assert _run_main(monkeypatch, kb, docs, "--write") == 0
        capsys.readouterr()
        written = (kb / "semantics.yml").read_text(encoding="utf-8")
        assert _field(kb, "loan", "loan_id")["description"] == (
            "the id number identifying the loan")
        # 第二次:零 diff,且文件逐字节不变
        assert _run_main(monkeypatch, kb, docs) == 0
        assert "已在同步状态" in capsys.readouterr().out
        assert (kb / "semantics.yml").read_text(encoding="utf-8") == written
        # --check 在同步后也应放行
        assert _run_main(monkeypatch, kb, docs, "--check") == 0

    def test_unrelated_kb_refuses_to_write(self, tmp_path, monkeypatch, capsys):
        """docs 的 dataset 与 KB 一个都对不上 → 退出码 2,且文件不动。

        「查不成的绿是假绿」:若这里放行,一个指错 --docs 的调用会以成功
        收场,而语义模型一个字都没被核对过。
        """
        kb = _write_kb(tmp_path / "financial")
        docs = _write_docs(tmp_path / "fin_database_description")
        doc = yaml.safe_load((kb / "semantics.yml").read_text(encoding="utf-8"))
        doc["semantic_model"][0]["datasets"] = [
            {"name": "orders", "fields": [{"name": "order_id", "description": "x"}]}]
        raw = _dump(doc)
        (kb / "semantics.yml").write_text(raw, encoding="utf-8")

        assert _run_main(monkeypatch, kb, docs, "--write") == 2
        assert "对不上" in capsys.readouterr().err
        assert (kb / "semantics.yml").read_text(encoding="utf-8") == raw

    def test_missing_docs_dir_exits_2(self, kb, tmp_path, monkeypatch):
        assert _run_main(monkeypatch, kb, tmp_path / "nowhere") == 2

    def test_docs_dir_without_csv_exits_2(self, kb, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert _run_main(monkeypatch, kb, empty) == 2

    def test_missing_semantics_yml_exits_2(self, tmp_path, docs, monkeypatch):
        kb = tmp_path / "emptykb"
        kb.mkdir()
        assert _run_main(monkeypatch, kb, docs) == 2

    def test_non_byte_identical_roundtrip_refuses(self, kb, docs, monkeypatch, capsys):
        """YAML 往返不保真 → 拒写(重写会把无关格式全部重排成噪声 diff)。"""
        text = (kb / "semantics.yml").read_text(encoding="utf-8")
        (kb / "semantics.yml").write_text(
            text.replace("version: 0.2.0.dev0", 'version: "0.2.0.dev0"'),
            encoding="utf-8")
        assert _run_main(monkeypatch, kb, docs, "--write") == 2
        assert "byte-identical" in capsys.readouterr().err

    def test_out_of_whitelist_change_is_blocked_and_not_written(
        self, kb, docs, monkeypatch, capsys,
    ):
        """内置结构 diff 断言:变更越出两键白名单 → 退出码 1 且不落盘。

        用 monkeypatch 冒充「未来某次改动顺手也碰了 metrics」:这个断言必须
        在**落盘前**把它拦下,而不是等人 review diff。
        """
        from scripts import import_official_semantics as mod

        real = mod.apply_official

        def rogue(doc, official):
            result = real(doc, official)
            doc["semantic_model"][0]["metrics"] = [{"name": "tampered"}]
            return result

        monkeypatch.setattr(mod, "apply_official", rogue)
        raw = (kb / "semantics.yml").read_text(encoding="utf-8")
        assert _run_main(monkeypatch, kb, docs, "--write") == 1
        assert "白名单" in capsys.readouterr().err
        assert (kb / "semantics.yml").read_text(encoding="utf-8") == raw


class TestWhitelist:
    def test_allowed_paths(self):
        assert _allowed_change("semantic_model[0].datasets[1].fields[2].description")
        assert _allowed_change("semantic_model[0].datasets[1].fields[2].enum_display")
        assert _allowed_change("semantic_model[0].datasets[0].fields[0].enum_display.A")
        # 新增键的路径带后缀(新增/删除/长度变化)
        assert _allowed_change("semantic_model[0].datasets[0].fields[3].enum_display (新增)")

    def test_disallowed_paths(self):
        for path in (
            "semantic_model[0].metrics[0].name",
            "semantic_model[0].datasets[0].name",
            "semantic_model[0].datasets[0].fields[0].ai_context",
            "semantic_model[0].datasets (长度 3→2)",
            "version",
        ):
            assert not _allowed_change(path), path

    def test_changed_paths_reports_added_keys(self):
        before = {"semantic_model": [{"datasets": [{"fields": [{"name": "a"}]}]}]}
        after = {"semantic_model": [{"datasets": [{"fields": [
            {"name": "a", "enum_display": {"X": "x"}}]}]}]}
        paths = changed_paths(before, after)
        assert paths == ["semantic_model[0].datasets[0].fields[0].enum_display (新增)"]
        assert all(_allowed_change(p) for p in paths)
