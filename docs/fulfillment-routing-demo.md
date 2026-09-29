# Fulfillment Routing MCP Demo

This runbook demonstrates the fulfillment-routing MVP in the Inventory Agent MCP. It uses local SQLite state and a simulated IOM/1TS adapter; it does not read or change production data.

## What the demo proves

- Only a sales order in `OPEN` state is eligible for routing.
- `Available` is the only quantity used to make routing decisions; `QoH` is informational.
- Location details include the origin address used in the proposed route.
- One order can be split across locations when a single location cannot fulfill it.
- Preparing a route does not change inventory.
- Execution requires explicit approval of the current plan version.
- Execution is idempotent: retrying it does not create duplicate assignments or fulfillment orders.

## 1. Install the project

Python 3.11 or later is required.

```sh
cd /Users/naveenacharya/dev/continuous-inventory
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

If the virtual environment already exists, only activate it and install again when dependencies have changed.

## 2. Run the automated checks

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

The expected result is that all tests pass. These tests validate the local implementation and simulated adapter; they are not evidence of production IOM/1TS connectivity.

## 3. Seed a fresh demo workspace

Use a new workspace name for each clean run:

```sh
continuous-inventory seed-routing-demo \
  --workspace output/runtime/routing-demo-1
```

The command creates this scenario:

| Record | Demo value |
|---|---|
| Sales order | `SO-1001`, state `OPEN` |
| Demand | 8 each of `SKU-1` |
| Split policy | Allowed, maximum 2 locations |
| East DC | `QoH` 12, `Available` 5, handling cost USD 4.00 |
| West DC | `QoH` 10, `Available` 4, handling cost USD 2.00 |
| Ship-to address | Austin, Texas |

Both distribution centers have origin addresses in the seeded location details. Neither location has 8 available units, so a valid route must split the order.

The seed command intentionally refuses to overwrite an existing populated workspace. Choose another workspace suffix, such as `routing-demo-2`, to restart from clean state.

## 4. Start the MCP server

In the MCP client configuration, use this command and environment variable:

```text
command: /Users/naveenacharya/dev/continuous-inventory/.venv/bin/continuous-inventory-mcp
CONTINUOUS_INVENTORY_WORKSPACE: /Users/naveenacharya/dev/continuous-inventory/output/runtime/routing-demo-1
```

For a direct terminal launch, run:

```sh
CONTINUOUS_INVENTORY_WORKSPACE=output/runtime/routing-demo-1 \
  continuous-inventory-mcp
```

The server communicates over standard input/output, so a direct terminal launch waits silently for an MCP client. Stop it with `Ctrl-C`. When an MCP client launches the command itself, do not start a second copy manually.

## 5. Run the demo sequence

Use the MCP client to call the tools in this order.

### A. Check the capability boundary

Call:

```text
inventory.get_routing_capabilities
{}
```

Confirm that:

- `eligible_order_states` contains only `OPEN`.
- `routable_quantity` is `Available`.
- `supports_split_routes` is `true`.
- `supports_projected_or_inbound_supply` is `false`.
- execution requires approval and uses the simulated adapter.

### B. Inspect the order before routing

Call:

```text
inventory.explain_order_fulfillment_state
{"order_id":"SO-1001"}
```

Confirm that:

- `routing_eligible` is `true` and `eligibility_reason` is `ORDER_OPEN`.
- the requested quantity is 8 each of `SKU-1`.
- East DC reports `QoH` 12 and `Available` 5.
- West DC reports `QoH` 10 and `Available` 4.
- both candidate locations include their origin addresses.

### C. Prepare a route

Call:

```text
inventory.prepare_fulfillment_route
{"order_id":"SO-1001","request_id":"demo-route-1"}
```

Save the returned `id` and `version`; they are needed for approval. Confirm that:

- `status` is `prepared`.
- the plan contains two fulfillment groups.
- West DC supplies 4 units and East DC supplies 4 units.
- `estimated_cost` is `6.00` USD.
- `cost_scope` says this is configured location handling cost only.
- the evidence records both `qoh_at_plan` and `available_at_plan`.

Call the same tool again with the same `request_id`. It should return the same plan instead of creating another one.

### D. Prove that preparation is non-mutating

Call the inspection tool again:

```text
inventory.explain_order_fulfillment_state
{"order_id":"SO-1001"}
```

East and West quantities should be unchanged. A prepared plan is a proposal, not a reservation or execution.

### E. Verify the approval boundary

Before approving, try:

```text
inventory.execute_route_plan
{"plan_id":"<returned-plan-id>"}
```

The call must fail because the route has not been approved.

Now approve the exact reviewed version:

```text
inventory.approve_route_plan
{"plan_id":"<returned-plan-id>","expected_version":<returned-version>}
```

Confirm that the status changes to `approved`. Approval alone must not change inventory.

### F. Execute the approved route

Call:

```text
inventory.execute_route_plan
{"plan_id":"<returned-plan-id>"}
```

Confirm that:

- `status` is `executed`.
- the result contains two assignment IDs.
- the result contains two fulfillment-order IDs.

Call the execution tool a second time with the same plan ID. It should return the same result and must not create duplicates.

### G. Inspect the final quantities

Call:

```text
inventory.explain_order_fulfillment_state
{"order_id":"SO-1001"}
```

The expected quantities are:

| Location | Before | After execution |
|---|---:|---:|
| East DC `QoH` | 12 | 12 |
| East DC `Available` | 5 | 1 |
| West DC `QoH` | 10 | 10 |
| West DC `Available` | 4 | 0 |

This is the key MVP behavior: routing and execution consume `Available`; they do not reduce `QoH` in the simulator.

## Suggested natural-language demo script

If the MCP client lets an assistant select tools, these prompts exercise the same flow:

1. `Show the fulfillment-routing capabilities.`
2. `Explain whether sales order SO-1001 can be routed and show inventory by location.`
3. `Prepare a fulfillment route for SO-1001 using request ID demo-route-1. Do not execute it.`
4. `Show me the proposed allocations, origin addresses, quantities, and estimated handling cost.`
5. `Approve the current version of that route.`
6. `Execute the approved route.`
7. `Show the order's inventory by location again and explain what changed.`

The assistant must pause for explicit approval before step 5. If it skips review or executes during preparation, the demo should be considered a failure even if the route itself is valid.

## Resetting the demo

Runtime state is kept under `output/runtime/` and is intentionally ignored by Git. The safest reset is to seed a new workspace name and update `CONTINUOUS_INVENTORY_WORKSPACE` to match it.

## Current MVP limits

- The domain adapter is a local simulator, not production IOM/1TS.
- Identity values are trusted local development context; there is no production authentication.
- Cost ranking uses configured location handling cost only.
- There is no carrier rate, transit-time, capacity, geocoding, or projected/inbound supply integration.
- Prepared plans expire after 30 minutes and become invalid if the order, selected location, or inventory version changes.

