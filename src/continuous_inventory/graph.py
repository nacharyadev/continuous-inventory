"""LangGraph plan-preparation agent with deterministic control boundaries."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .agent import ProposalProvider
from .common import Actor, InventoryError, digest, quantity
from .plans import PlanService


class AgentState(TypedDict, total=False):
    actor: Actor
    request: dict[str, Any]
    objective: str
    evidence: list[dict]
    options: list[dict]
    destination: dict
    sources: list[dict]
    need: Any
    selection: dict
    decision: dict
    plan: dict
    trace: Annotated[list[dict], operator.add]


class InventoryAgent:
    """Builds reviewable plans. Plan approval and execution stay outside the graph."""
    def __init__(self, plans: PlanService, provider: ProposalProvider):
        self.plans = plans
        self.provider = provider
        self.graph = self._build().compile()

    def _build(self):
        graph = StateGraph(AgentState)
        graph.add_node("validate_request", self._validate_request)
        graph.add_node("gather_context", self._gather_context)
        graph.add_node("build_options", self._build_options)
        graph.add_node("model_select", self._model_select)
        graph.add_node("validate_selection", self._validate_selection)
        graph.add_node("prepare_plan", self._prepare_plan)
        graph.add_edge(START, "validate_request")
        graph.add_edge("validate_request", "gather_context")
        graph.add_edge("gather_context", "build_options")
        graph.add_edge("build_options", "model_select")
        graph.add_edge("model_select", "validate_selection")
        graph.add_edge("validate_selection", "prepare_plan")
        graph.add_edge("prepare_plan", END)
        return graph

    @staticmethod
    def _step(name: str, **details):
        return {"trace": [{"node": name, **details}]}

    def _validate_request(self, state: AgentState):
        actor = state["actor"]
        actor.require("agent.prepare")
        request = state["request"]
        kind = request.get("kind")
        if kind not in {"landed_cost", "manufacturing"}:
            raise InventoryError("Unsupported agent capability")
        return self._step("validate_request", capability=kind)

    def _gather_context(self, state: AgentState):
        actor, request = state["actor"], state["request"]
        if request["kind"] == "landed_cost":
            source = self.plans.domain.get(actor, "bill", request["source"])
            evidence = [{"id": source["id"], "kind": "source_bill", "version": source["version"]}]
            for line in request["allocations"]:
                bill = self.plans.domain.get(actor, "bill", line["bill_id"])
                evidence.append({"id": bill["id"], "kind": "inventory_bill", "version": bill["version"]})
            objective = "Prepare a native Inventory Cost Allocation draft; do not post"
            context = {"evidence": evidence, "objective": objective}
        else:
            destination = self.plans.domain.get(actor, "stock", request["destination"])
            policy = self.plans.rules.resolve(actor, destination["item"], request["day"], request["manufacturing_order"])
            if policy["needs_teaching"]:
                raise InventoryError("Teach the item's process first")
            need = quantity(request["required"]) - (quantity(destination["quantity"]) - quantity(destination["protected"]))
            if need <= 0:
                raise InventoryError("No shortage")
            evidence = [
                {"id": destination["id"], "kind": "stock", "version": destination["version"]},
                {"id": request["manufacturing_order"], "kind": "customer_supplied_requirement", "required": request["required"]},
                {"id": "process", "kind": "declared_rules", "resolved": policy},
            ]
            sources = []
            for source_id in request["sources"]:
                source = self.plans.domain.get(actor, "stock", source_id)
                sources.append(source)
                evidence.append({"id": source_id, "kind": "stock", "version": source["version"],
                                 "available": str(quantity(source["quantity"]) - quantity(source["protected"]))})
            context = {"destination": destination, "need": need, "sources": sources, "evidence": evidence,
                       "objective": "Resolve a single-component material shortage without consuming protected stock"}
        return {**context, "trace": [{"node": "gather_context", "evidence_count": len(context["evidence"])}]}

    def _build_options(self, state: AgentState):
        request = state["request"]
        if request["kind"] == "landed_cost":
            options = [{"id": "reviewed_targets", "source": request["source"], "allocations": request["allocations"],
                        "evidence_ids": [item["id"] for item in state["evidence"]]}]
        else:
            destination = state["destination"]
            options = []
            for source in state["sources"]:
                available = quantity(source["quantity"]) - quantity(source["protected"])
                if source["id"] != destination["id"] and source["item"] == destination["item"] and available >= state["need"]:
                    options.append({"id": "transfer/" + source["id"], "source": source["id"], "quantity": str(state["need"]),
                                    "evidence_ids": [source["id"], destination["id"], request["manufacturing_order"], "process"]})
        if not options:
            raise InventoryError("No feasible options; ask for evidence or escalate")
        return {"options": options, "trace": [{"node": "build_options", "option_count": len(options)}]}

    def _model_select(self, state: AgentState):
        context = {"objective": state["objective"], "options": state["options"], "evidence": state["evidence"]}
        selection = self.provider.choose(context)
        return {"selection": selection, "decision": {"provider": self.provider.name, "context_hash": digest(context)},
                "trace": [{"node": "model_select", "provider": self.provider.name}]}

    def _validate_selection(self, state: AgentState):
        result = state["selection"]
        if not isinstance(result, dict) or set(result) != {"option_id", "reason", "evidence_ids"}:
            raise InventoryError("Model returned unsupported output schema")
        if result["option_id"] is None:
            raise InventoryError("Agent abstained; customer review required")
        if not isinstance(result["reason"], str) or not isinstance(result["evidence_ids"], list):
            raise InventoryError("Invalid model explanation/evidence")
        allowed = {item["id"] for item in state["evidence"]}
        if any(not isinstance(item, str) or item not in allowed for item in result["evidence_ids"]):
            raise InventoryError("Model cited unknown evidence")
        selected = next((item for item in state["options"] if item["id"] == result["option_id"]), None)
        if selected is None or not set(selected["evidence_ids"]).issubset(result["evidence_ids"]):
            raise InventoryError("Unsupported option or missing required evidence")
        decision = {**state["decision"], "reason": result["reason"], "evidence_ids": result["evidence_ids"]}
        return {"selection": selected, "decision": decision,
                "trace": [{"node": "validate_selection", "option_id": selected["id"]}]}

    def _prepare_plan(self, state: AgentState):
        actor, request, selected = state["actor"], state["request"], state["selection"]
        if request["kind"] == "landed_cost":
            plan = self.plans.prepare_landed_cost(actor, source=selected["source"], allocations=selected["allocations"],
                                                  evidence=state["evidence"], request_id=request.get("request_id"))
        else:
            plan = self.plans.prepare_shortage(actor, source=selected["source"], destination=request["destination"],
                                               required=request["required"], manufacturing_order=request["manufacturing_order"],
                                               day=request["day"], evidence=state["evidence"], request_id=request.get("request_id"))
        with self.plans.store.transaction() as db:
            self.plans.store.event(actor.tenant, plan["id"], "agent_decision", state["decision"], db)
            self.plans.store.event(actor.tenant, plan["id"], "agent_trajectory", {"nodes": [item["node"] for item in state["trace"]] + ["prepare_plan"]}, db)
        return {"plan": plan, "trace": [{"node": "prepare_plan", "plan_id": plan["id"]}]}

    def prepare_landed_cost(self, actor: Actor, source: str, allocations: list[dict], request_id=None):
        result = self.graph.invoke({"actor": actor, "request": {"kind": "landed_cost", "source": source,
                                    "allocations": allocations, "request_id": request_id}, "trace": []})
        return result["plan"]

    def prepare_shortage(self, actor: Actor, *, sources: list[str], destination: str, required: str,
                         manufacturing_order: str, day: str, request_id=None):
        result = self.graph.invoke({"actor": actor, "request": {"kind": "manufacturing", "sources": sources,
                                    "destination": destination, "required": required, "manufacturing_order": manufacturing_order,
                                    "day": day, "request_id": request_id}, "trace": []})
        return result["plan"]
