"""MCP transport for the Inventory Agent fulfillment-routing MVP."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .runtime import Runtime, local_actor


def create_mcp_server(runtime: Runtime, actor=None) -> MCPServer:
    """Build an MCP server over injected application and domain ports.

    The supplied actor is trusted development context. Production hosts must replace
    it with authenticated, per-request platform identity and authorization.
    """
    current=actor or local_actor()
    mcp=MCPServer(
        "inventory-agent",
        title="Inventory Agent",
        description="Approval-first inventory operations and fulfillment routing",
        instructions=(
            "For fulfillment routing, first inspect the order or prepare a route. "
            "Only OPEN sales orders are eligible. Available is the sole routable quantity; "
            "QoH is informational. Never execute a route before the user has reviewed and "
            "explicitly approved the current plan version."
        ),
        version="0.3.0",
    )

    @mcp.tool(name="inventory.get_routing_capabilities")
    def get_routing_capabilities() -> dict[str,Any]:
        """Get the exact fulfillment-routing behavior supported by this server."""
        return runtime.routing.capabilities(current)

    @mcp.tool(name="inventory.explain_order_fulfillment_state")
    def explain_order_fulfillment_state(order_id: str) -> dict[str,Any]:
        """Inspect order eligibility and current QoH/Available at active L1 locations."""
        return runtime.routing.explain_order(current,order_id)

    @mcp.tool(name="inventory.prepare_fulfillment_route")
    def prepare_fulfillment_route(order_id: str, request_id: str | None = None) -> dict[str,Any]:
        """Prepare an immutable route proposal for an OPEN sales order; makes no inventory changes."""
        return runtime.routing.prepare(current,order_id,request_id=request_id)

    @mcp.tool(name="inventory.get_route_plan")
    def get_route_plan(plan_id: str) -> dict[str,Any]:
        """Retrieve a fulfillment route, alternatives, evidence, approval and execution status."""
        return runtime.routing.get(current,plan_id)

    @mcp.tool(name="inventory.approve_route_plan")
    def approve_route_plan(plan_id: str, expected_version: int) -> dict[str,Any]:
        """Approve the reviewed current route version. This does not execute the route."""
        return runtime.routing.approve(current,plan_id,expected_version)

    @mcp.tool(name="inventory.execute_route_plan")
    def execute_route_plan(plan_id: str) -> dict[str,Any]:
        """Execute an approved route idempotently through the injected IOM/1TS domain adapter."""
        return runtime.routing.execute(current,plan_id)

    return mcp


def main() -> None:
    workspace=Path(os.environ.get("CONTINUOUS_INVENTORY_WORKSPACE","output/runtime/mcp"))
    tenant=os.environ.get("CONTINUOUS_INVENTORY_TENANT","demo").strip()
    actor_name=os.environ.get("CONTINUOUS_INVENTORY_ACTOR","local-mcp-operator").strip()
    if not tenant or not actor_name:
        raise ValueError("Development tenant and actor must be nonempty")
    server=create_mcp_server(Runtime.local(workspace),local_actor(tenant,actor_name))
    server.run()


if __name__=="__main__":
    main()
