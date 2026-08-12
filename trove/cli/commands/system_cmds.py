"""System commands: /model, /datasource, /init."""

from trove.cli.slash_registry import SlashRegistry, SlashCommand


def register_system_commands(registry: SlashRegistry, context: dict) -> None:
    """Register system management commands."""

    async def cmd_model(args: str) -> str:
        """Show or set the current LLM model. Usage: /model [model_name]"""
        config = context.get("config")
        if not config:
            return "No configuration available."

        if args.strip():
            config.target = args.strip()
            return f"Model set to: {args.strip()}"
        else:
            return f"Current model: {config.target or 'not set'}"

    async def cmd_datasource(args: str) -> str:
        """Show or switch datasource. Usage: /datasource [name]"""
        registry_obj = context.get("connector_registry")
        if not registry_obj:
            return "No datasource registry."

        if args.strip():
            # Switch
            name = args.strip()
            if registry_obj.is_registered(name):
                # Update default
                registry_obj._default_name = name
                return f"Switched to datasource: {name}"
            return f"Datasource '{name}' not found. Available: {registry_obj.list_names()}"

        # Show current
        names = registry_obj.list_names()
        default = registry_obj.default_name
        lines = ["Registered datasources:"]
        for name in names:
            mark = " ← current" if name == default else ""
            lines.append(f"  {name}{mark}")
        return "\n".join(lines) if names else "No datasources registered."

    async def cmd_init(args: str) -> str:
        """Initialize .trove project config in current directory."""
        from trove.storage.config_store import ConfigStore
        store = ConfigStore()
        if store.exists():
            return ".trove/config.yml already exists."
        store.save(store.load())  # Create with defaults
        return "Created .trove/config.yml"

    registry.register(SlashCommand(
        name="model",
        description="Show/set LLM model. Usage: /model [name]",
        group="system",
        handler=cmd_model,
    ))
    registry.register(SlashCommand(
        name="datasource",
        description="Show/switch datasource. Usage: /datasource [name]",
        group="system",
        handler=cmd_datasource,
        aliases=["ds"],
    ))
    registry.register(SlashCommand(
        name="init",
        description="Initialize .trove/config.yml in current directory",
        group="system",
        handler=cmd_init,
    ))
