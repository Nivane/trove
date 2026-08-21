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


def _point_home(monkeypatch, tmp_path: Path) -> Path:
    """Redirect the CLI's config.home to tmp via conf/agent.yml in cwd."""
    home = tmp_path / ".trove"
    conf = tmp_path / "conf"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "agent.yml").write_text(f"agent:\n  home: {home}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return home


@pytest.mark.asyncio
async def test_maintenance_status_empty(tmp_path, monkeypatch, capsys):
    """空 home:status 输出包含 0 个会话,不报错。"""
    _point_home(monkeypatch, tmp_path)
    await main_maintenance(["status"])
    out = capsys.readouterr().out
    assert "sessions" in out


@pytest.mark.asyncio
async def test_maintenance_run_dry_run(tmp_path, monkeypatch, capsys):
    """dry-run 报告候选但不删除任何文件。"""
    from trove.storage.session_store import SessionStore

    home = _point_home(monkeypatch, tmp_path)
    store = SessionStore(home_dir=str(home))
    for _ in range(3):
        s = await store.create_session(".", user_id="alice")
        await store.save_session(s)

    await main_maintenance(["run", "--dry-run"])
    out = capsys.readouterr().out
    assert "dry" in out.lower()
    # 文件都在
    remaining = await store.list_all()
    assert len(remaining) == 3
