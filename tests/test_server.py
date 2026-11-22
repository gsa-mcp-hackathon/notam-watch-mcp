"""Smoke tests for the NFS MCP server."""

import pytest
from fastmcp import Client, FastMCP

from server.app import mcp
from server.config import settings


def test_server_is_importable() -> None:
    assert mcp is not None


def test_server_has_name() -> None:
    assert mcp.name == "NFS MCP Server"


def test_default_transport_is_stdio() -> None:
    assert settings.mcp_transport == "stdio"


def test_components_register_without_error() -> None:
    from server.prompts import register_prompts
    from server.resources import register_resources
    from server.routes import register_routes
    from server.tools import register_tools

    register_tools(FastMCP("tools-test"))
    register_prompts(FastMCP("prompts-test"))
    register_resources(FastMCP("resources-test"))
    register_routes(FastMCP("routes-test"))


@pytest.mark.asyncio
async def test_tools_are_discoverable() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    for tool in (
        "find_flight_plan",
        "get_route_notams",
        "get_flights_approaching_notams",
        "get_flight_briefing",
        "find_route_deviations_near_notams",
        "search_notams",
    ):
        assert tool in names
    # Removed stubs that returned placeholder data and could mislead agents.
    for stub in (
        "get_live_flights",
        "preview_predictive_analysis",
        "preview_nms_sync",
        "say_hello",
        # Static or credential-less status tools: always "ready" / always failing
        # inside the app. Ingest jobs still use the NMS/SWIM probes directly.
        "get_platform_status",
        "check_nms_connection",
        "check_swim_connection",
        # Translation is done by the agent's model, not a separate tool.
        "translate_notam",
    ):
        assert stub not in names
    assert len(names) == 6


@pytest.mark.asyncio
async def test_prompt_and_resource_are_discoverable() -> None:
    prompts = await mcp.list_prompts()
    resources = await mcp.list_resources()

    prompt_names = {prompt.name for prompt in prompts}
    assert {
        "preflight_notam_briefing",
        "airspace_watch",
        "flight_status",
        "route_conformance",
        "notam_lookup",
    } <= prompt_names
    assert "greet_someone" not in prompt_names
    uris = {str(resource.uri) for resource in resources}
    assert "nfs://info/server" in uris
    assert "nfs://architecture/overview" not in uris


@pytest.mark.asyncio
@pytest.mark.parametrize("altitude", [None, "30000"])
async def test_preflight_prompt_renders_optional_altitude(altitude: str | None) -> None:
    arguments = {"origin": "KJFK", "destination": "KDCA"}
    if altitude is not None:
        arguments["cruise_altitude_ft"] = altitude

    async with Client(mcp) as client:
        result = await client.get_prompt("preflight_notam_briefing", arguments)

    text = result.messages[0].content.text
    assert "KJFK to KDCA" in text
    assert "get_route_notams" in text
    if altitude is None:
        assert "cruise_altitude_ft" not in text
    else:
        assert "cruise_altitude_ft=30000" in text


@pytest.mark.asyncio
async def test_route_conformance_prompt_is_nationwide() -> None:
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
        prompt = next(item for item in prompts if item.name == "route_conformance")
        assert not prompt.arguments
        result = await client.get_prompt("route_conformance")

    text = result.messages[0].content.text
    assert "flights nationwide" in text
    assert "find_route_deviations_near_notams" in text
