"""存量兼容:已有资产零迁移加载,治理字段按 I1 推断(设计稿 A1)。

跑的是**仓库里真实的三份 examples.yml**(git 跟踪的存量数据),不是构造的
样本 —— I1 说的「存量零迁移」只有拿真数据才验得出来:构造的样本一定符合
写样本时脑子里的形状,存量才会暴露「字段比我预期的少」这类问题。

刻意**不冻结条数**。存量条数是会变的(加一条示例、改一条问题都会动它),
硬编码一个数字会让这条测试在完全正当的改动下变红,而它的职责是「无损」。
所以断言写成关系:loader 加载到的 == 文件里非 pending 的(独立用 yaml 数
出来的)。**2026-09-29 实测**:demo 211/206、financial 201/198、
mysql_fin 242/218(全量/非 pending),合计 654 → 622。

注意 206/198/218 与设计稿 §2.1 的数字一致 —— 它是 **loader 视角**(pending
已被跳过),不是文件里的原始条数。两个数字都对,量的是两件事。
"""

import shutil
from pathlib import Path

import pytest
import yaml

from trove.services.kb.governance import DRAFT, SOURCE_KB_INIT
from trove.services.kb.service import KbService, _parse_file

REPO_KB = Path(__file__).resolve().parents[3] / ".trove" / "kb"
DATASOURCES = ("demo", "financial", "mysql_fin")
GOVERNANCE_KEYS = {"status", "owner", "approved_by", "approved_at", "source"}


def _raw_examples(ds: str) -> list[dict]:
    """直接读盘 —— 与 loader 无关的独立口径(否则「无损」是自证的)。"""
    path = REPO_KB / ds / "examples.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["examples"]


def _loaded_examples(ds: str) -> list[dict]:
    return [
        payload for kind, _key, payload in _parse_file(REPO_KB / ds / "examples.yml")
        if kind in ("example", "template")
    ]


class TestStockAssetsLoadUnchanged:
    """A1 / I1:存量加载一条不少,治理字段全部推断为 draft。"""

    @pytest.mark.parametrize("ds", DATASOURCES)
    def test_loader_keeps_every_non_pending_example(self, ds):
        """无损 = 文件里非 pending 的每一条都被加载(pending 不加载是 I4)。"""
        raw = _raw_examples(ds)
        expected = [ex for ex in raw if not ex.get("pending")]

        loaded = _loaded_examples(ds)

        assert len(loaded) == len(expected)
        assert [ex["question"] for ex in loaded] == [
            str(ex.get("question", "")) for ex in expected]

    @pytest.mark.parametrize("ds", DATASOURCES)
    def test_stock_examples_infer_draft_from_kb_init(self, ds):
        """存量一律 draft + kb_init:治理字段不要求任何人改文件。

        这一条同时是 I1 的**方向**断言:推断必须偏低 —— 存量资产没有任何
        人工背书的证据,把它们猜成 certified 等于凭空给人背书。
        """
        for payload in _loaded_examples(ds):
            assert set(payload) >= GOVERNANCE_KEYS
            assert payload["status"] == DRAFT
            assert payload["source"] == SOURCE_KB_INIT
            assert payload["approved_by"] == ""
            assert payload["approved_at"] == ""

    @pytest.mark.parametrize("ds", DATASOURCES)
    def test_stock_files_carry_no_governance_block(self, ds):
        """前提探针:存量文件里**真的**没有 governance 块。

        上一条断言之所以有意义,全靠存量确实没写过治理块。哪天有人手写块进
        真实 KB(或跑了一次迁移),这里先红 —— 它会提醒改测试的人:推断规则
        的覆盖面变了,而不只是数字变了。
        """
        for example in _raw_examples(ds):
            assert "governance" not in example


class TestStockAssetsThroughService:
    """同一条不变量,走完整服务链路(YAML → mirror → ExampleHit)。"""

    @pytest.fixture
    def kb(self, tmp_path):
        """真实 KB 的副本(只拷 YAML)。

        不直接指向仓库的 KB:``ensure_synced`` 会写 kb.sqlite,测试绝不能动
        工作区里的镜像;也不拷 kb.sqlite —— 一份旧版本的镜像会触发镜像版本
        门(拒绝或重建),那是另一条路径的测试,不该混进来。
        """
        kb_dir = tmp_path / "kb"
        for ds in DATASOURCES:
            (kb_dir / ds).mkdir(parents=True)
            for yml in (REPO_KB / ds).glob("*.yml"):
                shutil.copy(yml, kb_dir / ds / yml.name)
        return KbService(tmp_path, kb_dir=kb_dir, git_kb=False)

    @pytest.mark.parametrize("ds", DATASOURCES)
    async def test_mirror_rows_carry_inferred_governance(self, kb, ds):
        await kb.ensure_synced(ds)

        rows = await kb._rows(
            "SELECT payload FROM kb_items "
            "WHERE kind IN ('example', 'template') AND datasource = ?", (ds,))

        import json
        payloads = [json.loads(r["payload"]) for r in rows]
        assert len(payloads) == len(_loaded_examples(ds))
        for payload in payloads:
            assert payload["status"] == DRAFT
            assert payload["source"] == SOURCE_KB_INIT

    async def test_retrieved_hits_expose_governance_fields(self, kb):
        """治理字段必须一路到 ExampleHit —— 中间任何一段丢键,检索侧就瞎了。

        用文件里第一条模板的**原文**提问:自匹配必然命中,因此这里红的只可能
        是治理字段没穿过 mirror,而不是「没检索到」。
        """
        await kb.ensure_synced("demo")
        question = str(_raw_examples("demo")[0]["question"])

        hits = await kb.search_examples(question, "demo", limit=5)

        assert hits, "自匹配未命中,先怀疑检索而不是治理字段"
        for hit in hits:
            assert hit.status == DRAFT
            assert hit.source == SOURCE_KB_INIT
