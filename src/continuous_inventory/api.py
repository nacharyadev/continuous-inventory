"""FastAPI transport for the approval-first continuous inventory service."""
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .agent import ProposalProvider
from .common import Actor, Conflict, Forbidden, InventoryError
from .events import LocalEventIngress
from .forecast import backtest
from .graph import InventoryAgent
from .runtime import Runtime, local_actor


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRequest(StrictModel):
    id: str
    label: str
    max_age_hours: int = 24


class DemandImportRequest(StrictModel):
    source: str
    content: str
    source_version: str
    kind: Literal["confirmed_order", "forecast", "pipeline"]
    item_map: dict[str, str]
    location_map: dict[str, str]
    mode: Literal["snapshot"] = "snapshot"


class DemandActivateRequest(StrictModel):
    batch_id: str


class OutlookRequest(StrictModel):
    item: str
    location: str
    day: str
    forecast_source: str | None = None


class SalesEventRequest(StrictModel):
    title: str
    starts: str | None = None
    ends: str | None = None
    item: str | None = None
    location: str | None = None
    projected_quantity: str | None = None


class RuleRequest(StrictModel):
    scope: Literal["once", "item", "business"]
    mode: Literal["order_driven", "stock_based"]
    item: str | None = None
    case_id: str | None = None
    starts: str | None = None
    ends: str | None = None
    batch_limit: str | None = None
    reason: str = ""


class ForecastConfigureRequest(StrictModel):
    algorithm: str
    parameters: dict[str, Any] | None = None
    item: str | None = None
    starts: str | None = None
    ends: str | None = None


class ForecastRunRequest(StrictModel):
    item: str
    day: str
    history: list[Any]
    horizon: int


class ForecastCompareRequest(StrictModel):
    history: list[Any]
    candidates: list[dict[str, Any]]
    min_train: int = 3
    horizon: int = 1


class Allocation(StrictModel):
    bill_id: str
    amount: str


class LandedCostRequest(StrictModel):
    source: str
    allocations: list[Allocation]
    request_id: str | None = None


class ManufacturingRequest(StrictModel):
    sources: list[str]
    destination: str
    required: str
    manufacturing_order: str
    day: str
    request_id: str | None = None


class ApprovalRequest(StrictModel):
    expected_version: int = Field(ge=1)


class EventAggregate(StrictModel):
    type: str
    id: str
    version: int = Field(ge=1)


class EventEnvelope(StrictModel):
    event_id: str
    event_type: str
    schema_version: int = Field(ge=1)
    source: str
    occurred_at: str
    aggregate: EventAggregate
    payload: dict[str, Any]
    correlation_id: str | None = None
    causation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def create_app(runtime: Runtime, provider: ProposalProvider, *, local_event_api: bool = False) -> FastAPI:
    """Create the API over supplied state, domain, and model adapters.

    Authentication is intentionally absent. Tenant and actor headers are unverified
    local context and are not a production trust boundary.
    """
    app = FastAPI(title="Continuous Inventory", version="0.2.0")
    agent = InventoryAgent(runtime.plans, provider)
    events = LocalEventIngress(runtime.store)

    def actor(
        tenant: Annotated[str, Header(alias="X-Tenant-Id")] = "demo",
        name: Annotated[str, Header(alias="X-Actor-Id")] = "local-operator",
    ) -> Actor:
        if not tenant.strip() or not name.strip() or len(tenant) > 120 or len(name) > 120:
            raise InventoryError("Development tenant and actor headers must be nonempty and at most 120 characters")
        return local_actor(tenant.strip(), name.strip())

    ActorDep = Annotated[Actor, Depends(actor)]

    @app.exception_handler(Forbidden)
    async def forbidden_handler(_request, exc):
        return JSONResponse(status_code=403, content={"error": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict_handler(_request, exc):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(InventoryError)
    async def inventory_handler(_request, exc):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok", "persistence": "sqlite-or-injected", "domain": "injected"}

    @app.get("/v1/plans")
    def list_plans(current: ActorDep):
        current.require("plans.read")
        return runtime.store.list(current.tenant, "plan", limit=100)

    @app.post("/v1/sources", status_code=201)
    def register_source(request: SourceRequest, current: ActorDep):
        return runtime.demand.register_source(current, **request.model_dump())

    @app.post("/v1/demand/imports", status_code=201)
    def import_demand(request: DemandImportRequest, current: ActorDep):
        return runtime.demand.stage_csv(current, **request.model_dump())

    @app.post("/v1/demand/activate")
    def activate_demand(request: DemandActivateRequest, current: ActorDep):
        return runtime.demand.activate(current, **request.model_dump())

    @app.post("/v1/demand/outlook")
    def demand_outlook(request: OutlookRequest, current: ActorDep):
        return runtime.demand.outlook(current, **request.model_dump())

    @app.post("/v1/sales-events", status_code=201)
    def declare_sales_event(request: SalesEventRequest, current: ActorDep):
        return runtime.demand.declare_event(current, **request.model_dump())

    @app.post("/v1/rules", status_code=201)
    def create_rule(request: RuleRequest, current: ActorDep):
        return runtime.rules.teach(current, **request.model_dump())

    @app.post("/v1/forecasts/configure", status_code=201)
    def configure_forecast(request: ForecastConfigureRequest, current: ActorDep):
        return runtime.forecasts.configure(current, **request.model_dump())

    @app.post("/v1/forecasts/run")
    def run_forecast(request: ForecastRunRequest, current: ActorDep):
        return runtime.forecasts.run(current, **request.model_dump())

    @app.post("/v1/forecasts/compare")
    def compare_forecasts(request: ForecastCompareRequest, current: ActorDep):
        current.require("forecast.read")
        return backtest(**request.model_dump())

    @app.post("/v1/plans/landed-cost", status_code=201)
    def prepare_landed_cost(request: LandedCostRequest, current: ActorDep):
        data = request.model_dump()
        data["allocations"] = [item.model_dump() for item in request.allocations]
        return agent.prepare_landed_cost(current, **data)

    @app.post("/v1/plans/manufacturing", status_code=201)
    def prepare_manufacturing(request: ManufacturingRequest, current: ActorDep):
        return agent.prepare_shortage(current, **request.model_dump())

    @app.get("/v1/plans/{plan_id}")
    def get_plan(plan_id: str, current: ActorDep):
        return runtime.plans.get(current, plan_id)

    @app.post("/v1/plans/{plan_id}/approve")
    def approve_plan(plan_id: str, request: ApprovalRequest, current: ActorDep):
        return runtime.plans.approve(current, plan_id, request.expected_version)

    @app.post("/v1/plans/{plan_id}/execute")
    def execute_plan(plan_id: str, current: ActorDep):
        return runtime.plans.execute(current, plan_id)

    @app.post("/v1/plans/{plan_id}/verify")
    def verify_plan(plan_id: str, current: ActorDep):
        return runtime.plans.verify(current, plan_id)

    @app.post("/v1/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: str, current: ActorDep):
        return runtime.plans.cancel(current, plan_id)

    if local_event_api:
        @app.post("/local/v1/events", status_code=202)
        def ingest_event(envelope: EventEnvelope, current: ActorDep):
            return events.ingest(current, envelope.model_dump())

        @app.get("/local/v1/events/{event_id}")
        def get_event(event_id: str, current: ActorDep):
            return events.get(current, event_id)

    return app
