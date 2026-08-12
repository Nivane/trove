"""TUI rendering using Rich.

Provides formatted output for:
  - Streamed responses (thought, sql, result, done)
  - Markdown rendering
  - Token usage display
  - Error formatting
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box


class TUI:
    """Terminal UI renderer for the Trove REPL."""

    def __init__(self):
        self._console = Console()

    def print_welcome(self, version: str = "0.1.0") -> None:
        """Print the welcome banner."""
        self._console.print(
            Panel.fit(
                f"[bold cyan]Trove[/bold cyan] v{version} — Intelligent Data Agent\n"
                "Natural language → SQL → Answers\n\n"
                "Type [bold]/help[/bold] for commands, or just ask a question.\n"
                "Type [bold]/exit[/bold] to quit.",
                title="Welcome",
                border_style="cyan",
            )
        )

    def print_prompt(self) -> None:
        """Print the input prompt."""
        self._console.print("", end="")

    def print_thought(self, content: str) -> None:
        """Print a streaming thought/processing indicator."""
        self._console.print(f"  [dim]🤔 {content}[/dim]")

    def print_sql(self, sql: str) -> None:
        """Print generated SQL in a styled panel."""
        self._console.print(
            Panel(sql, title="Generated SQL", border_style="blue", title_align="left")
        )

    def print_result_table(self, columns: list[str], rows: list[list], max_rows: int = 20) -> None:
        """Print query results as a formatted table."""
        table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")

        for col in columns:
            table.add_column(col)

        for row in rows[:max_rows]:
            table.add_row(*[str(cell) for cell in row])

        self._console.print(table)

        if len(rows) > max_rows:
            self._console.print(f"  [dim]... and {len(rows) - max_rows} more rows[/dim]")

    def print_result_empty(self) -> None:
        """Print empty result indicator."""
        self._console.print("  [yellow]Query returned zero rows.[/yellow]")

    def print_markdown(self, content: str) -> None:
        """Render Markdown content."""
        md = Markdown(content)
        self._console.print(md)

    def print_error(self, message: str) -> None:
        """Print an error message."""
        self._console.print(f"  [red]✗ {message}[/red]")

    def print_success(self, message: str) -> None:
        """Print a success message."""
        self._console.print(f"  [green]✓ {message}[/green]")

    def print_info(self, message: str) -> None:
        """Print an informational message."""
        self._console.print(f"  [blue]ℹ {message}[/blue]")

    def print_token_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Print token usage summary."""
        total = prompt_tokens + completion_tokens
        self._console.print(
            f"  [dim]Tokens: {prompt_tokens} prompt + {completion_tokens} completion "
            f"= {total} total[/dim]"
        )

    def print_exec_time(self, ms: float) -> None:
        """Print execution time."""
        self._console.print(f"  [dim]Execution time: {ms:.0f}ms[/dim]")

    def print_separator(self) -> None:
        """Print a visual separator."""
        self._console.print("─" * 60)

    def print_help_text(self, text: str) -> None:
        """Print command help text."""
        self._console.print(text)

    @property
    def console(self) -> Console:
        return self._console
