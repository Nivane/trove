"""Metadata commands: /tables, /schemas, /table_schema."""

from trove.cli.slash_registry import SlashRegistry, SlashCommand


def register_metadata_commands(registry: SlashRegistry, context: dict) -> None:
    """Register metadata browsing commands."""

    async def cmd_tables(args: str) -> str:
        """List all tables in the current datasource."""
        catalog = context.get("catalog_service")
        if not catalog:
            return "No datasource connected. Use /datasource to connect."

        try:
            tables = await catalog.list_tables()
            if not tables:
                return "No tables found in the current datasource."

            lines = ["Tables:"]
            for t in tables:
                lines.append(
                    f"  {t['name']:<30} "
                    f"({t['columns']} columns, ~{t.get('row_count', '?')} rows)"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing tables: {e}"

    async def cmd_table_schema(args: str) -> str:
        """Show schema for a specific table. Usage: /table_schema <table_name>"""
        if not args.strip():
            return "Usage: /table_schema <table_name>"

        catalog = context.get("catalog_service")
        if not catalog:
            return "No datasource connected."

        try:
            detail = await catalog.table_detail(args.strip())
            if not detail:
                return f"Table '{args.strip()}' not found."

            lines = [
                f"Table: {detail['name']}",
                f"Approx rows: {detail.get('row_count', 'unknown')}",
                "",
                "Columns:",
            ]
            for col in detail["columns"]:
                pk = " [PK]" if col["primary_key"] else ""
                nullable = "" if col["nullable"] else " NOT NULL"
                lines.append(f"  {col['name']:<25} {col['type']}{nullable}{pk}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"

    async def cmd_schemas(args: str) -> str:
        """List available schemas."""
        catalog = context.get("catalog_service")
        if not catalog:
            return "No datasource connected."
        tables = await catalog.list_tables()
        schemas = sorted(set(t.get("schema", "main") for t in tables))
        return "Schemas:\n" + "\n".join(f"  {s}" for s in schemas)

    async def cmd_databases(args: str) -> str:
        """Show current datasource info."""
        registry = context.get("connector_registry")
        if not registry:
            return "No datasource registry."

        names = registry.list_names()
        default = registry.default_name
        lines = ["Datasources:"]
        for name in names:
            marker = " (default)" if name == default else ""
            lines.append(f"  {name}{marker}")
        return "\n".join(lines) if names else "No datasources registered."

    registry.register(SlashCommand(
        name="tables",
        description="List all tables",
        group="metadata",
        handler=cmd_tables,
    ))
    registry.register(SlashCommand(
        name="table_schema",
        description="Show columns of a table. Usage: /table_schema <name>",
        group="metadata",
        handler=cmd_table_schema,
        aliases=["schema"],
    ))
    registry.register(SlashCommand(
        name="schemas",
        description="List available schemas",
        group="metadata",
        handler=cmd_schemas,
    ))
    registry.register(SlashCommand(
        name="databases",
        description="List registered datasources",
        group="metadata",
        handler=cmd_databases,
        aliases=["dbs"],
    ))
