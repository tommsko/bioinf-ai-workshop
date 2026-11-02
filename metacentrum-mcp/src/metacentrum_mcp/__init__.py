"""MCP gateway from agent harnesses to the MetaCentrum grid (PBS)."""

__version__ = "0.2.0"


def main() -> None:
    from .cli import main as cli_main

    cli_main()
