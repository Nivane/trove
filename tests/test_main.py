import pytest

from trove.main import mcp_parser, parse_args, serve_parser


def test_repl_default_no_datasource():
    assert parse_args([]).datasource == ""


def test_serve_default_no_datasource():
    assert serve_parser().parse_args([]).datasource == ""


def test_repl_still_takes_a_workflow():
    """REPL 是唯一真读它的地方(main.py 的 workflow_name=args.workflow)。"""
    assert parse_args(["-w", "fixed"]).workflow == "fixed"


@pytest.mark.parametrize("parser", [serve_parser, mcp_parser])
def test_serve_and_mcp_refuse_a_workflow_flag(parser):
    """serve / mcp 不该收下自己从不读的参数。

    这两个入口曾经都声明了 ``--workflow`` 却没有任何读取点:``trove serve
    -w fixed`` 不报错、也不生效,启动照旧跑 reflection —— 一个不会失败的错误
    答案。单轮工作流由 API 请求体逐条指定(/v1/chat 的 ``workflow`` 字段),
    启动参数选不了。这条用例钉住「宁可报错,不要静默忽略」。
    """
    assert not hasattr(parser().parse_args([]), "workflow")
    with pytest.raises(SystemExit):
        parser().parse_args(["-w", "fixed"])
