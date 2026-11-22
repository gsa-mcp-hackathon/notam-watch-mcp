"""Register all prompts exposed by the server."""

from server.prompts import briefing


def register_prompts(mcp) -> None:
    """Register every prompt module."""
    briefing.register(mcp)


__all__ = ["register_prompts"]
