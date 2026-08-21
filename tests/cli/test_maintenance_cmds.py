"""trove maintenance CLI 冒烟测试(真实 tmp home + tmp cwd)。

CLI 通过 _load_config 解析 home(CONFIG_SEARCH_PATHS 首位是 ./conf/agent.yml),
因此用 tmp cwd 下的 conf/agent.yml 把 home 指向 tmp,避免 CLI 扫到真实
~/.trove(代码库不识别 TROVE_HOME 环境变量;brief 的 setenv 构造路径以
实际实现为准调整,断言语义不变)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trove.cli.maintenance_cmds import main_maintenance


def _point_home(monkeypatch, tmp_path: Path, quota: int = 100, grace_min: int = 10) -> Path:
    """Redirect the CLI's config.home to tmp via conf/agent.yml in cwd."""
    home = tmp_path / ".trove"
    conf = tmp_path / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "agent.yml").write_text(
        f"agent:\n"
        f"  home: {home}\n"
        f"  retention:\n"
        f"    max_sessions_per_user: {quota}\n"
        f"    active_grace_min: {grace_min}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return home


@pytest.mark.asyncio
async def test_maintenance_status_empty(tmp_path, monkeypatch, capsys):
    """空 home:status 输出具体统计(会话数、磁盘、配额),不报错。"""
    _point_home(monkeypatch, tmp_path)
    await main_maintenance(["status"])
    out = capsys.readouterr().out
    assert "sessions=0" in out
    assert "disk_mb=0.0" in out
    assert "quota_per_user=100" in out


@pytest.mark.asyncio
async def test_maintenance_run_dry_run(tmp_path, monkeypatch, capsys):
    """dry-run 报告候选(配额 2、3 会话 → 1 候选)但不删除任何文件。"""
    from trove.storage.session_store import SessionStore

    home = _point_home(monkeypatch, tmp_path, quota=2)
    store = SessionStore(home_dir=str(home))
    for _ in range(3):
        s = await store.create_session(".", user_id="alice")
        await store.save_session(s)

    await main_maintenance(["run", "--dry-run"])
    out = capsys.readouterr().out
    assert '"sessions": 3' in out
    assert '"candidates": 1' in out
    # 文件都在
    remaining = await store.list_all()
    assert len(remaining) == 3


@pytest.mark.asyncio
async def test_maintenance_run_default_no_orphans(tmp_path, monkeypatch, capsys):
    """默认 run = 配额 sweep + 深度修剪,不含孤儿清理(orphans 键恒在、值为 0)。"""
    from trove.storage.session_store import SessionStore

    home = _point_home(monkeypatch, tmp_path, quota=2, grace_min=0)
    store = SessionStore(home_dir=str(home))
    for _ in range(3):
        s = await store.create_session(".", user_id="alice")
        await store.save_session(s)

    await main_maintenance(["run"])
    out = capsys.readouterr().out
    assert '"orphans": 0' in out
    assert "removed=1" in out  # SweepStats.__str__ 用 removed=N 表示 removed_sessions
    assert len(await store.list_all()) == 2
