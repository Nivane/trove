"""确认即认证:治理块落盘 + ``Approved-by`` trailer + 认证门(§7.2 / G4 / G5)。

三条被钉住的不变量,都不是「实现细节」而是设计取向:

- **认证是人的显式动作,必须过门**(失败模式表:closed)—— 不过门就整批拒绝,
  不做部分确认。理由:确认是「这条可以被信任并直接执行」的背书,部分确认会
  让 admin 以为整个批次都进了库。
- **不知道批准人时不写认证记录**(I5 的推论)—— 宁可不认,不可假认。
- **确认写下的东西,加载器必须读回同一个状态** —— 写端与读端的治理语义若
  漂移,盘上会留下一批「写时 certified、读时 draft」的资产,而且没有任何
  报错。这是本文件里最重要的一条往返断言。
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from trove.services.kb.governance import (
    CERTIFIED,
    ExampleCertificationError,
    certification_issues,
)
from trove.services.kb.service import KbService


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture
def git_kb(tmp_path):
    """一个带 git 身份的数据源(零网络)—— 审计链断言要真的读 git log。

    本文件不复用 test_git_versioning 的 fixture:那两个 fixture 服务的是
    「git 机制本身」,这里的断言对象是**治理语义**;共用会让任一方的改动
    同时动到另一方的红。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@local")
    kb = KbService(repo, git_kb=True)
    (kb.kb_dir / "demo").mkdir(parents=True)
    return kb, repo


def _examples(kb: KbService, ds: str = "demo") -> list[dict]:
    path = kb.kb_dir / ds / "examples.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["examples"]


def _trailers(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%(trailers)").stdout


class TestConfirmWritesGovernance:
    """确认 → 治理块补齐(§6.1「下次该资产被确认时自然补齐」)。"""

    async def test_confirm_writes_certified_block_with_approver(self, git_kb):
        kb, _ = git_kb
        await kb.draft_example("东区贷款总额", "SELECT SUM(amount) FROM loan", "demo")

        assert await kb.confirm_pending_examples("demo", actor="admin") == 1

        example = _examples(kb)[0]
        assert "pending" not in example
        gov = example["governance"]
        assert gov["status"] == CERTIFIED
        assert gov["approved_by"] == "admin"
        assert gov["owner"] == "admin"  # §6.1:owner = 首次确认者
        assert gov["approved_at"], "I5 要求 certified 带日期"

    async def test_confirm_without_actor_does_not_claim_certified(self, git_kb):
        """拿不到批准人(API 层未传主体)→ 不写认证记录,只清 pending。

        I5 说 certified 必须有人;那么「不知道是谁」时唯一的诚实姿态是**不
        认**。写一个 approved_at 却没有 approved_by 的半条记录,只会让台账
        上出现一批看不出问题的假认证。
        """
        kb, _ = git_kb
        await kb.draft_example("东区贷款总额", "SELECT SUM(amount) FROM loan", "demo")

        assert await kb.confirm_pending_examples("demo") == 1

        example = _examples(kb)[0]
        assert "pending" not in example  # 确认本身仍然生效:进检索
        assert (example.get("governance") or {}).get("status") != CERTIFIED


class TestAuditTrail:
    """G4:examples 路径的提交必须能追到批准人(§2.3 的语义/示例不对称)。"""

    async def test_confirm_commit_carries_approved_by_and_generator(self, git_kb):
        kb, repo = git_kb
        await kb.draft_example("东区贷款总额", "SELECT SUM(amount) FROM loan", "demo")

        await kb.confirm_pending_examples("demo", actor="admin")

        trailers = _trailers(repo)
        assert "Approved-by: admin" in trailers
        assert "Generator: kb" in trailers

    async def test_confirm_without_actor_writes_no_approved_by_trailer(self, git_kb):
        """没有批准人时**不写** trailer —— 不能拿 git 的提交者冒充批准人。

        ``GitKb._with_trailers`` 会丢掉空值,所以这条靠的是「传空而非编造」,
        而不是靠它过滤:传 ``actor`` 本身为空串,谁来过滤都不会变成一个名字。
        """
        kb, repo = git_kb
        await kb.draft_example("东区贷款总额", "SELECT SUM(amount) FROM loan", "demo")

        await kb.confirm_pending_examples("demo")

        trailers = _trailers(repo)
        assert "Approved-by" not in trailers
        assert "Generator: kb" in trailers


class TestCertificationGate:
    """G5:认证门不过 → 整批拒绝(closed),文件与盘上状态一个字节都不动。"""

    @pytest.mark.parametrize("sql", [
        "DELETE FROM loan",
        "SELEC * FORM loan",
        "SELECT 1; DROP TABLE loan",
    ])
    async def test_confirm_refuses_bad_sql_and_leaves_file_untouched(self, git_kb, sql):
        kb, repo = git_kb
        await kb.draft_example("坏示例", sql, "demo")
        before = (kb.kb_dir / "demo" / "examples.yml").read_text(encoding="utf-8")
        head_before = _git(repo, "rev-parse", "HEAD").stdout

        with pytest.raises(ExampleCertificationError):
            await kb.confirm_pending_examples("demo", actor="admin")

        assert (kb.kb_dir / "demo" / "examples.yml").read_text(encoding="utf-8") == before
        # 被拒的资产保持 pending:既不进检索,也没被悄悄丢掉
        assert len(await kb.list_pending_examples("demo")) == 1
        hits = await kb.search_examples("坏示例", "demo", limit=5)
        assert not any(h.question == "坏示例" for h in hits)
        assert _git(repo, "rev-parse", "HEAD").stdout == head_before

    async def test_refusal_names_the_offending_asset(self, git_kb):
        """报错要点名是哪一条、错在哪 —— 否则 admin 面对一批 pending 无从下手。

        原因文案**原样来自认证门**而不是在这里自造一句:查日志的人搜得到同一个
        字符串,门的措辞改了这里也跟着改(断言的是复用关系,不是某句文案)。
        """
        kb, _ = git_kb
        sql = "DELETE FROM loan"
        await kb.draft_example("东区贷款总额", sql, "demo")

        with pytest.raises(ExampleCertificationError) as excinfo:
            await kb.confirm_pending_examples("demo", actor="admin")

        message = str(excinfo.value)
        assert "东区贷款总额" in message
        assert certification_issues(sql)[0] in message

    async def test_one_bad_asset_refuses_the_whole_batch(self, git_kb):
        """整批拒绝,不做部分确认:好资产也留在 pending,等坏资产修好。

        部分确认看起来更「友好」,但它让「confirmed」这个状态失去含义 ——
        admin 点了确认,却有一半没进库,而返回的计数说不清哪些进了。
        """
        kb, _ = git_kb
        await kb.draft_example("好示例", "SELECT SUM(amount) FROM loan", "demo")
        await kb.draft_example("坏示例", "DELETE FROM loan", "demo")

        with pytest.raises(ExampleCertificationError):
            await kb.confirm_pending_examples("demo", actor="admin")

        pending = {ex["question"] for ex in await kb.list_pending_examples("demo")}
        assert pending == {"好示例", "坏示例"}


class TestWriteReadRoundTrip:
    """写端(confirm)与读端(loader)的治理语义必须一致。"""

    async def test_confirmed_asset_loads_back_as_certified(self, git_kb):
        """确认写下的 certified,重新加载后必须仍是 certified。

        这条同时钉住两件事:(1) confirm 写的块自己过得了 I5(不是「写了但
        读回被降级」);(2) 治理字段能穿过 mirror 一路到 ExampleHit —— 中间
        任何一段把 payload 的键丢了,这里就红。
        """
        kb, _ = git_kb
        await kb.draft_example(
            "东区贷款总额", "SELECT SUM(amount) FROM loan", "demo",
        )

        await kb.confirm_pending_examples("demo", actor="admin")

        hits = await kb.search_examples("东区贷款总额", "demo", limit=5)
        hit = next(h for h in hits if h.question == "东区贷款总额")
        assert hit.status == CERTIFIED
        assert hit.approved_by == "admin"
        assert hit.approved_at

    async def test_pending_asset_stays_out_of_retrieval(self, git_kb):
        """I4 回归:确认前不进检索 —— 治理字段的出现不得改变这条不变量。

        正面钉住(而不是断言「没有例外」):草稿在盘上确实存在、确实被 loader
        看见过(list_pending 读的是同一个文件),只是不参与检索。这样即便将来
        有人把 pending 过滤挪到 governance 解析之后,这条也会红。
        """
        kb, _ = git_kb
        await kb.draft_example("东区贷款总额", "SELECT SUM(amount) FROM loan", "demo")

        assert [ex["question"] for ex in await kb.list_pending_examples("demo")] == [
            "东区贷款总额"]
        hits = await kb.search_examples("东区贷款总额", "demo", limit=5)
        assert not any(h.question == "东区贷款总额" for h in hits)


class TestDraftKeepsGovernanceOutOfTheWay:
    """草稿阶段是 open 的:坏 SQL 照样能存成草稿(只有认证才 closed)。"""

    async def test_draft_accepts_sql_that_certification_will_later_refuse(self, git_kb):
        """草稿不当门 —— 门在确认那一刻。

        草稿是**待审**状态,本身就是给人看的;提前拒绝只会让「用户反馈了一
        条错 SQL」这件事消失在日志里,而它恰恰是改对它的线索。
        """
        kb, _ = git_kb
        res = await kb.draft_example("坏示例", "DELETE FROM loan", "demo")
        assert res["status"] == "drafted"
        assert len(await kb.list_pending_examples("demo")) == 1
