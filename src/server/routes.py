"""HTTP-only health and version routes."""

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from server import __version__


def register_routes(mcp: FastMCP) -> None:
    """Register lightweight routes used by hosted deployments."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse({"status": "healthy", "service": "nfs-mcp"})

    @mcp.custom_route("/version", methods=["GET"])
    async def version(request: Request) -> JSONResponse:
        return JSONResponse({"version": __version__})
