from .common import Actor
from .store import StatePort, InMemoryState
from .domain import DomainPort, SimulatedDomain
from .plans import PlanService
from .demand import DemandService
from .forecast import ForecastService
from .rules import RuleService
from .routing import FulfillmentRoutingService

PERMISSIONS=frozenset({"demand.write","demand.activate","demand.read","rules.write","rules.read","rules.business",
 "forecast.configure","forecast.business","forecast.read","domain.seed","domain.read","domain.draft","domain.post",
 "domain.transfer","domain.receive","plans.prepare","plans.read","plans.approve","plans.execute","plans.verify","agent.prepare",
 "domain.fulfillment","routing.read","routing.prepare","routing.approve","routing.execute",
 "events.ingest","events.read"})


def local_actor(tenant="demo",name="local-operator"):
    """Trusted local demo identity only; an API host must provide authenticated identity."""
    return Actor(tenant,name,PERMISSIONS)


class Runtime:
    """Stateless composition root: state and domain authorities are supplied by the host."""
    def __init__(self, *, control_state: StatePort, domain: DomainPort):
        self.store=control_state
        self.domain=domain
        self.plans=PlanService(control_state,domain)
        self.demand=DemandService(control_state)
        self.rules=RuleService(control_state)
        self.forecasts=ForecastService(control_state)
        self.routing=FulfillmentRoutingService(control_state,domain)

    @classmethod
    def simulated(cls):
        """Fresh ephemeral simulation. Nothing is persisted by this service."""
        return cls(control_state=InMemoryState(),domain=SimulatedDomain())

    @classmethod
    def local(cls, directory):
        """Persistent local simulation, with separate SQLite control and domain databases."""
        from pathlib import Path
        from .sqlite_state import SQLiteState
        root = Path(directory)
        return cls(control_state=SQLiteState(root / "control.sqlite"),
                   domain=SimulatedDomain(SQLiteState(root / "simulated-domain.sqlite")))
