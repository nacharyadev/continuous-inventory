"""Independent observable-state checks and an RRSI-compatible JSON bridge."""
from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

from .agent import BoundedAgent,FixtureProvider
from .common import Conflict,InventoryError
from .runtime import Runtime,local_actor


def trajectory(trace,kind):
    names=[e["name"] for e in trace]
    checks={"prepared": "prepared" in names,"approved":"approved" in names,"authoritative_confirmation":"command_confirmed" in names,
            "outcome_verified":"verified" in names}
    checks["execution_started"]="execution_started" in names
    if all(checks.values()):
        checks["causal_authorization"]=names.index("prepared")<names.index("approved")<names.index("execution_started")<names.index("command_confirmed")<names.index("verified")
        approval=next(e["body"]["plan_hash"] for e in trace if e["name"]=="approved")
        checks["version_bound_approval"]=all(e["body"]["plan_hash"]==approval for e in trace if e["name"]=="execution_started")
    return checks


def seed(runtime,actor):
    runtime.domain.seed_bill(actor,"freight-1",kind="freight",amount="120.00")
    runtime.domain.seed_bill(actor,"inventory-1",kind="inventory",amount="1000.00")
    runtime.domain.seed_stock(actor,"warehouse",item="component-A",location="L1-W",amount="100",protected="30")
    runtime.domain.seed_stock(actor,"factory",item="component-A",location="L1-F",amount="5")
    runtime.rules.teach(actor,scope="item",item="component-A",mode="order_driven",reason="Build to confirmed orders")


def run_trial(task:dict,workspace:str) -> dict:
    r=Runtime.simulated();a=local_actor();seed(r,a);agent=BoundedAgent(r.plans,FixtureProvider())
    capability=task.get("capability","landed_cost")
    fault=task.get("input",{}).get("fault")
    if capability=="landed_cost":
        plan=agent.prepare_landed_cost(a,"freight-1",[{"bill_id":"inventory-1","amount":"120.00"}])
    elif capability=="manufacturing":
        plan=agent.prepare_shortage(a,sources=["warehouse"],destination="factory",required="25",manufacturing_order="MO-1",day="2026-09-27")
    else:raise InventoryError("Unknown evaluation capability")
    r.plans.approve(a,plan["id"],plan["version"])
    if fault=="stale_draft":r.domain.edit_draft(a,plan["payload"]["draft_id"],[{"bill_id":"inventory-1","amount":"100.00"}])
    rejected=False
    try:
        result=r.plans.execute(a,plan["id"],lose_ack=fault=="lost_ack")
        if result["status"]=="unknown":
            r=Runtime(control_state=r.store, domain=r.domain)  # New worker, same external state authorities.
            result=r.plans.execute(a,plan["id"])
        if capability=="manufacturing":
            r.domain.confirm_receipt(a,result["result"]["transaction_id"],"10" if fault=="partial_receipt" else "20","receipt-1")
        result=r.plans.verify(a,plan["id"])
    except Conflict:
        rejected=True;result=r.plans.get(a,plan["id"])
    if capability=="landed_cost":
        observed={"allocated":r.domain.get(a,"bill","freight-1")["allocated"],"target_added_cost":r.domain.get(a,"bill","inventory-1")["added_cost"],
                  "draft_status":r.domain.get(a,"draft",plan["payload"]["draft_id"])["status"]}
    else:
        observed={"source_quantity":r.domain.get(a,"stock","warehouse")["quantity"],"source_protected":r.domain.get(a,"stock","warehouse")["protected"],
                  "destination_quantity":r.domain.get(a,"stock","factory")["quantity"]}
    return {"capability":capability,"answer":{"status":result["status"],"rejected":rejected,"observed":observed},
            "trace":r.store.trace(a.tenant,plan["id"]),"policy_tokens":0,"provider":"deterministic-fixture-not-an-llm"}


def verify_trial(task,output):
    # Expected values are held by the verifier, not computed by the domain under test.
    expected=task["expected"]
    actual=output["answer"];checks={"status":actual["status"]==expected["status"]}
    for key,value in expected.get("observed",{}).items():
        found=actual["observed"].get(key)
        try:checks["state:"+key]=Decimal(str(found))==Decimal(str(value))
        except Exception:checks["state:"+key]=found==value
    if expected["status"]=="completed":checks.update(trajectory(output["trace"],output["capability"]))
    if expected["status"]=="exception":
        checks["no_post_confirmation"]=not any(e["name"]=="command_confirmed" for e in output["trace"])
    failures=[k for k,v in checks.items() if not v]
    return {"reward":float(not failures),"checks":checks,"failure_tags":failures}


def run_suite(path):
    tasks=json.loads(Path(path).read_text());reports=[]
    for task in tasks:
        with tempfile.TemporaryDirectory(prefix="inventory-eval-") as workspace:
            output=run_trial({k:v for k,v in task.items() if k!="expected"},workspace)
            reports.append({"id":task["id"],"capability":task["capability"],"verdict":verify_trial(task,output),"output":output})
    return {"type":"isolated_simulation_not_production_evidence","provider":"deterministic-fixture-not-an-llm",
            "passed":sum(r["verdict"]["reward"]==1 for r in reports),"total":len(reports),"reports":reports}
