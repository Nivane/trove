"""Checkpointer factory and graph-state persistence tests."""

from trove.storage.checkpoint_store import build_checkpointer
from trove.workflow.graphs import GraphServices, build_graphs
from trove.workflow.state import WorkflowState


def make_graphs(checkpointer):
    return build_graphs(
        GraphServices(llm=None, catalog=None, connectors=None, config=None),
        checkpointer=checkpointer,
    )["empty"]


class TestBuildCheckpointer:
    async def test_creates_saver_and_db_file(self, tmp_home):
        async with build_checkpointer(str(tmp_home)) as saver:
            assert saver is not None
            db_path = tmp_home / "checkpoints.db"
            assert db_path.exists()

    async def test_reopening_existing_db_works(self, tmp_home):
        """The checkpointer DB can be reopened after a previous session."""
        async with build_checkpointer(str(tmp_home)):
            pass
        async with build_checkpointer(str(tmp_home)) as saver:
            assert saver is not None


class TestGraphStatePersistence:
    async def test_state_persisted_per_thread(self, tmp_home):
        """After a run, the final graph state is retrievable via thread_id."""
        async with build_checkpointer(str(tmp_home)) as saver:
            graph = make_graphs(saver)
            state = WorkflowState(session_id="s1", question="q")
            await graph.ainvoke(state, {"configurable": {"thread_id": "s1"}})

            snapshot = await graph.aget_state({"configurable": {"thread_id": "s1"}})
            assert snapshot.values["final_response"]
            assert "(未执行任何查询)" in snapshot.values["final_response"]

    async def test_threads_are_isolated(self, tmp_home):
        """Different session thread_ids keep independent checkpoints."""
        async with build_checkpointer(str(tmp_home)) as saver:
            graph = make_graphs(saver)
            await graph.ainvoke(
                WorkflowState(session_id="s1", question="q1"),
                {"configurable": {"thread_id": "s1"}},
            )
            await graph.ainvoke(
                WorkflowState(session_id="s2", question="q2"),
                {"configurable": {"thread_id": "s2"}},
            )

            s1 = await graph.aget_state({"configurable": {"thread_id": "s1"}})
            s2 = await graph.aget_state({"configurable": {"thread_id": "s2"}})
            assert s1.values["question"] == "q1"
            assert s2.values["question"] == "q2"

    async def test_resume_updates_existing_thread(self, tmp_home):
        """Invoking again with the same thread_id resumes the existing thread."""
        async with build_checkpointer(str(tmp_home)) as saver:
            graph = make_graphs(saver)
            await graph.ainvoke(
                WorkflowState(session_id="s1", question="q1"),
                {"configurable": {"thread_id": "s1"}},
            )
            await graph.ainvoke(
                WorkflowState(session_id="s1", question="q2"),
                {"configurable": {"thread_id": "s1"}},
            )
            snapshot = await graph.aget_state({"configurable": {"thread_id": "s1"}})
            assert snapshot.values["question"] == "q2"
