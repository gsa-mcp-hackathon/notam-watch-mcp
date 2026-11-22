"""Register all resources exposed by the server."""

from server.resources import info


def register_resources(mcp) -> None:
    """Register every resource module."""
    info.register(mcp)


__all__ = ["register_resources"]
