"""`trove-cli --print` 一次性进程的收尾:打印完必须真的退出。

aiosqlite 的 worker 线程是非 daemon 的 —— 任何 store 不释放连接,进程就会
在 asyncio.run 返回后一直挂着。交互入口靠显式 exit 遮住了这个问题,
--print 路径没有:输出齐全后进程不退出(2026-10 实测)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from trove.main import _dispose_components


def _hook(calls: list[str], name: str, *, fail: bool = False, method: str = "dispose"):
    async def fn() -> None:
        calls.append(name)
        if fail:
            raise RuntimeError(f"{name} boom")

    obj = SimpleNamespace()
    setattr(obj, method, fn)
    return obj


async def test_dispose_components_calls_every_hook_and_isolates_failures():
    """每个持有连接的组件都要收;一个失败不能把其余组件留在原地。"""
    calls: list[str] = []
    components = {
        "question_coverage": _hook(calls, "coverage"),
        "connector_registry": _hook(calls, "connectors", method="close_all"),
        "jobs": SimpleNamespace(store=_hook(calls, "jobs.store")),
        "actions": SimpleNamespace(store=_hook(calls, "actions.store")),
        "oidc": _hook(calls, "oidc"),
        "verdicts": _hook(calls, "verdicts"),
        "memory": _hook(calls, "memory", fail=True),
        "user_facts": _hook(calls, "user_facts"),
        "session_store": _hook(calls, "session_store"),
        "settings": _hook(calls, "settings"),
        "auth": _hook(calls, "auth"),
    }

    await _dispose_components(components)

    # 顺序是承重的:coverage 的后台任务先收,再放连接。
    assert calls == [
        "coverage", "connectors", "jobs.store", "actions.store",
        "oidc", "verdicts", "memory", "user_facts",
        "session_store", "settings", "auth",
    ]


async def test_dispose_components_tolerates_missing_and_partial_entries():
    """测试/库外调用方给的是部分 components —— 缺席键一律跳过。"""
    await _dispose_components({})
    await _dispose_components({
        "memory": None,
        "jobs": SimpleNamespace(),          # 没有 .store
        "connector_registry": SimpleNamespace(),  # 没有 close_all
    })


@pytest.mark.parametrize("mode", ["print", "repl"])
def test_one_shot_process_exits(tmp_path, mode):
    """端到端:启动组件后进程必须自行退出(空 stdin 不跑查询)。

    --print 与 REPL(EOF 即退出)共用同一份清理清单;挂了就是这里超时 ——
    子进程被 kill,测试失败(回归门)。
    """
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("TROVE_STORAGE_URL", None)
    env.pop("TROVE_ADMIN_PASSWORD", None)
    args = ["--datasource", "demo"] + (["--print"] if mode == "print" else [])

    try:
        result = subprocess.run(
            [
                sys.executable, "-c",
                "from trove.main import main_cli; main_cli()",
                *args,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{mode} 进程在收尾后没有退出(aiosqlite worker 线程被挂住?)")

    assert result.returncode == 0, result.stderr[-2000:]
