"""_flatten_details: CLI 把 output 节点发来的 <details> 折叠段展平为普通 markdown。"""

from trove.cli.app import _flatten_details


class TestFlattenDetails:
    def test_strips_wrapper_lines_keeps_inner(self):
        src = (
            "## Answer\n\n"
            "### Conclusion\n"
            "ok\n\n"
            "<details>\n"
            "<summary>View SQL & details</summary>\n"
            "\n"
            "### Generated SQL\n"
            "\n"
            "```sql\n"
            "SELECT 1\n"
            "```\n"
            "</details>\n"
        )
        flat = _flatten_details(src)
        assert "<details>" not in flat
        assert "<summary>" not in flat
        assert "</details>" not in flat
        assert "### Generated SQL" in flat
        assert "SELECT 1" in flat
        assert "## Answer" in flat

    def test_passthrough_without_wrapper(self):
        src = "## Answer\n\n**Question**: q\n"
        assert _flatten_details(src).rstrip("\n") == src.rstrip("\n")
