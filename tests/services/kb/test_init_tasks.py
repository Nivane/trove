"""异步 KB init 任务注册表 + init_kb 进度回调测试。"""

from __future__ import annotations

from trove.services.kb.init_tasks import InitTaskStore


class TestInitTaskStore:
    def setup_method(self):
        self.store = InitTaskStore(ttl_s=3600, max_entries=5)
        self.store.reset()

    def test_create_running_and_mutex(self):
        t1 = self.store.create("demo", "ds-1")
        assert t1["status"] == "running"
        assert t1["progress"] == 0
        # 同源 running → 拒绝
        assert self.store.create("demo", "ds-1") is None
        # 其他源可并行
        t2 = self.store.create("other", "ds-2")
        assert t2 is not None and t2["status"] == "running"

    def test_update_progress_and_done(self):
        task = self.store.create("demo", "ds-1")
        self.store.update(task["id"], stage="notes", progress=40, detail="1/3")
        got = self.store.by_datasource("demo")
        assert got["stage"] == "notes"
        assert got["progress"] == 40
        assert got["detail"] == "1/3"
        self.store.done(task["id"], "Initialized ...")
        got = self.store.by_datasource("demo")
        assert got["status"] == "done"
        assert got["progress"] == 100
        assert "Initialized" in got["summary"]
        # done 后可重新 init(互斥解除)
        assert self.store.create("demo", "ds-1") is not None

    def test_fail_records_error(self):
        task = self.store.create("demo", "ds-1")
        self.store.fail(task["id"], "LLM draft parse failed")
        got = self.store.by_datasource("demo")
        assert got["status"] == "error"
        assert "parse failed" in got["error"]

    def test_update_unknown_task_noop(self):
        self.store.update("nope", progress=50)  # 不抛

    def test_ttl_prunes_finished(self):
        store = InitTaskStore(ttl_s=-1, max_entries=10)  # 立即过期
        store.reset()
        task = store.create("demo", "ds-1")
        store.done(task["id"], "ok")
        store._prune_locked()
        assert store.by_datasource("demo") is None

    def test_capacity_keeps_running(self):
        store = InitTaskStore(max_entries=2)
        store.reset()
        t1 = store.create("a", "")
        store.done(t1["id"], "ok")
        t2 = store.create("b", "")
        store.done(t2["id"], "ok")
        t3 = store.create("c", "")  # running,不应被容量清理
        store._prune_locked()
        assert store.by_datasource("c") is not None
        assert store.by_datasource("c")["status"] == "running"


class TestInitProgressCallback:
    async def test_init_kb_reports_progress(self, tmp_path, sqlite_registry, monkeypatch):
        """init_kb 的 progress 回调在各阶段上报 stage/progress。"""
        import yaml

        from trove.core.config import AgentConfig
        from trove.llm.gateway import LLMGateway
        from trove.services.kb.init_pipeline import init_kb
        from trove.services.kb.service import KbService

        # 单表 + 即时 mock → init 很快,但每个 stage 都会上报
        monkeypatch.setattr("trove.services.kb.init_pipeline.INIT_CHUNK_TABLES", 8)
        kb = KbService(tmp_path / "proj")
        events: list[dict] = []

        await init_kb(
            kb, sqlite_registry,
            llm=LLMGateway(mock_response="tables:\n  - name: students\n"
                                          "    description: student records\n"
                                          "    columns:\n"
                                          "      - name: grade\n"
                                          "        type: int\n"
                                          "        description: test score\n"
                                          "        enums: []\n"
                                          "    metrics: []\n"),
            config=AgentConfig(target="mock/model"),
            datasource="test_db",
            progress=lambda u: events.append(u),
        )

        stages = [e["stage"] for e in events]
        assert "schema" in stages
        assert "notes" in stages
        assert "semantic" in stages
        assert "write" in stages
        assert "done" in stages
        # 进度单调不降,终态 100
        progresses = [e["progress"] for e in events]
        assert progresses == sorted(progresses)
        assert events[-1]["progress"] == 100
        assert (kb.kb_dir / "test_db" / "semantics.yml").exists()

    async def test_progress_callback_exception_is_swallowed(self, tmp_path, sqlite_registry, monkeypatch):
        """progress 回调抛异常不阻断 init。"""
        from trove.core.config import AgentConfig
        from trove.llm.gateway import LLMGateway
        from trove.services.kb.init_pipeline import init_kb
        from trove.services.kb.service import KbService

        monkeypatch.setattr("trove.services.kb.init_pipeline.INIT_CHUNK_TABLES", 8)
        kb = KbService(tmp_path / "proj")

        def boom(u):
            raise RuntimeError("progress boom")

        summary = await init_kb(
            kb, sqlite_registry,
            llm=LLMGateway(mock_response="tables:\n  - name: students\n"
                                          "    description: student records\n"
                                          "    columns: []\n    metrics: []\n"),
            config=AgentConfig(target="mock/model"),
            datasource="test_db",
            progress=boom,
        )
        assert "Initialized" in summary
