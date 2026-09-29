# Continuous Inventory

A runnable FastAPI and LangGraph service for approval-first inventory operations. SQLite is the only implemented persistence backend; state and domain authority remain replaceable ports and workers do not rely on process memory. External-system behavior is simulated locally.

## Run

Requires Python 3.11 or later. Install the package and test dependencies into a virtual environment; no database server is required.

```sh
cd /Users/naveenacharya/dev/continuous-inventory
python -m pip install -e '.[test]'
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python3 -m continuous_inventory demo --output output/reports/demo.json
PYTHONPATH=src python3 -m continuous_inventory evaluate --cases examples/evaluation_cases.json --output output/reports/evaluation.json
```

The demo exercises both pilot tracks with approval, simulated posting/transfer, lost-acknowledgment recovery, and verification. To retain its state, add `--workspace output/runtime/demo-1` with a new directory. Evaluation results are synthetic contract evidence, not proof of real-agent quality.

Run the fulfillment-routing MCP simulation:

```sh
continuous-inventory seed-routing-demo --workspace output/runtime/routing-mcp
CONTINUOUS_INVENTORY_WORKSPACE=output/runtime/routing-mcp continuous-inventory-mcp
```

For the complete walkthrough and expected results, see [Fulfillment Routing MCP Demo](docs/fulfillment-routing-demo.md).

For the business-level workflow and responsibility boundaries, see [Fulfillment Routing Overview](docs/fulfillment-routing-overview.md).

The MCP server uses stdio and exposes six `inventory.*` tools for capability discovery, order inspection, route preparation, plan retrieval, approval, and execution. The demo order is `SO-1001`. Only `OPEN` orders are eligible, and execution remains local and simulated.

Compare forecasting methods:

```sh
PYTHONPATH=src python3 -m continuous_inventory forecast --history 10,12,11,15,14,18 --compare
PYTHONPATH=src python3 -m continuous_inventory forecast --history 10,20,12,22 --method seasonal_naive --parameters '{"season_length":2}' --horizon 4
```

Preview imported demand:

```sh
PYTHONPATH=src python3 -m continuous_inventory preview-demand examples/sales-projections.csv --item-map '{"SKU-A":"component-A"}' --location-map '{"MAIN":"L1-F"}'
PYTHONPATH=src python3 -m continuous_inventory preview-event 'Our annual sale is coming up'
```

Run the unauthenticated local simulation API with `continuous-inventory serve-demo`. For Podman, use `podman compose up --build`. See [API and adapter contracts](docs/api.md) and [implementation status and limits](docs/implementation-status.md) before connecting real services.

## Implementation layout

- `src/continuous_inventory/`: demand, rules, forecasting, fulfillment routing, MCP and FastAPI transports, plans, LangGraph agent, provider-neutral model adapter, adapters, CLI and evaluation bridge.
- `tests/`: state/HTTP contracts, agent trajectories, local event envelopes, concurrency, recovery, tenant isolation, forecasting and pilot outcome checks.
- `examples/`: CSV input and declarative pilot evaluation cases.
- `output/reports/`: generated demo and evaluation results.
- `output/runtime/`: ignored local SQLite files, only when local persistence is selected.

## Strategy and architecture artifacts

- [Implementation architecture](continuous-inventory-implementation-architecture.md) — proposed AI-native design, contracts, diagrams, and rollout gates.
- [Strategy paper](continuous-inventory-technology-strategy.md) — editable working document.
- [Print-ready PDF](output/pdf/continuous-inventory-technology-strategy.pdf) — print snapshot of the strategy paper. Use the Markdown strategy for the latest content and regenerate before printing an updated edition.
- `scripts/build_print.py` — PDF generator; requires Python with ReportLab and pypdf. Diagram layouts are defined in the script and should be updated when source diagrams change.

Run the generator from any directory with `python3 /Users/naveenacharya/dev/continuous-inventory/scripts/build_print.py`.

The initial files were copied from the ChatGPT project mirror. This folder is the working location for future revisions and artifacts.
