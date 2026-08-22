from trove.main import parse_args, serve_parser


def test_repl_default_no_datasource():
    assert parse_args([]).datasource == ""


def test_serve_default_no_datasource():
    assert serve_parser().parse_args([]).datasource == ""
