from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .common import Actor, Conflict, InventoryError, digest, money, now, quantity, uid
from .domain import DomainPort
from .rules import RuleService
from .store import StatePort


class PlanService:
    """Single-action pilot workflows backed by caller-owned state. No automatic delegation or live adapters yet."""

    def __init__(self, store: StatePort, domain: DomainPort):
        self.store=store; self.domain=domain; self.rules=RuleService(store)

    def _save(self, actor, id, kind, payload, expected, objective, *, rule_context=None, evidence=None, preparation_hash=None):
        body={"kind":kind,"payload":payload,"expected":expected,"objective":objective,
              "status":"prepared","preparation_hash":preparation_hash,"rule_context":rule_context,"evidence":evidence or [],"approval":None,
              "expires_at":(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat(),"result":None,"error":None}
        body["plan_hash"]=digest({k:body[k] for k in ("kind","payload","expected","objective","rule_context","expires_at")})
        with self.store.transaction() as db:
            try:
                plan=self.store.put(actor.tenant,"plan",id,body,db)
            except Conflict:
                existing=self.store.get(actor.tenant,"plan",id,db)
                if preparation_hash is None or existing.get("preparation_hash")!=preparation_hash:
                    raise
                return existing
            self.store.event(actor.tenant,id,"prepared",{"plan_hash":body["plan_hash"],"kind":kind},db)
            return plan

    def _preparation(self, actor, request_id, request):
        fingerprint=digest(request)
        id="plan_"+digest({"tenant":actor.tenant,"request_id":request_id}) if request_id else uid("plan")
        try:old=self.store.get(actor.tenant,"plan",id)
        except InventoryError:old=None
        if old and old.get("preparation_hash")!=fingerprint:
            raise Conflict("Preparation request ID reused with different intent")
        return id,fingerprint,old

    def prepare_landed_cost(self, actor: Actor, *, source: str, allocations: list[dict], evidence=None, request_id=None):
        actor.require("plans.prepare")
        id,preparation_hash,existing=self._preparation(actor,request_id,{"kind":"landed_cost","source":source,"allocations":allocations})
        if existing:return existing
        source_bill=self.domain.get(actor,"bill",source)
        expected={source:source_bill["version"]}
        for line in allocations:
            bill=self.domain.get(actor,"bill",line["bill_id"])
            expected[bill["id"]]=bill["version"]
        draft=self.domain.prepare_allocation(actor,"ica_"+id,source,allocations,source_bill["currency"])
        return self._save(actor,id,"landed_cost",{"draft_id":draft["id"],"draft_version":draft["version"],
            "source":source,"allocations":allocations,"currency":source_bill["currency"]},expected,
            "Post reviewed Inventory Cost Allocation and verify cost completion",evidence=evidence,preparation_hash=preparation_hash)

    def prepare_shortage(self, actor: Actor, *, source: str, destination: str, required: str,
                         manufacturing_order: str, day: str, evidence=None, request_id=None):
        actor.require("plans.prepare")
        if not manufacturing_order:raise InventoryError("Manufacturing order reference required")
        id,preparation_hash,existing=self._preparation(actor,request_id,{"kind":"manufacturing","source":source,"destination":destination,"required":required,"manufacturing_order":manufacturing_order,"day":day})
        if existing:return existing
        src=self.domain.get(actor,"stock",source);dst=self.domain.get(actor,"stock",destination)
        if src["item"]!=dst["item"] or source==destination:raise InventoryError("Invalid source/destination")
        policy=self.rules.resolve(actor,dst["item"],day,manufacturing_order)
        if policy["needs_teaching"]:raise InventoryError("Teach this item's process before preparing a recovery")
        available=quantity(dst["quantity"])-quantity(dst["protected"])
        shortage=max(Decimal(0),quantity(required,positive=True)-available)
        if shortage==0:raise InventoryError("Manufacturing requirement is already covered")
        if shortage>quantity(src["quantity"])-quantity(src["protected"]):raise Conflict("No feasible transfer without consuming protected stock")
        payload={"source":source,"destination":destination,"item":dst["item"],"quantity":str(shortage),
                 "required":str(quantity(required,positive=True)),"manufacturing_order":manufacturing_order}
        context={"item":dst["item"],"day":day,"case_id":manufacturing_order,"resolved":policy}
        return self._save(actor,id,"manufacturing",payload,{source:src["version"],destination:dst["version"]},
            "Confirm uncommitted destination material covers the linked manufacturing requirement",rule_context=context,evidence=evidence,preparation_hash=preparation_hash)

    def get(self, actor, id):
        actor.require("plans.read")
        return self.store.get(actor.tenant,"plan",id)

    def approve(self, actor: Actor, id: str, expected_version: int):
        actor.require("plans.approve")
        with self.store.transaction() as db:
            plan=self.store.get(actor.tenant,"plan",id,db)
            if plan["version"]!=expected_version or plan["status"]!="prepared":raise Conflict("Only the reviewed current prepared version may be approved")
            self._check_expiry(plan)
            plan.update(status="approved",approval={"actor":actor.name,"plan_hash":plan["plan_hash"],"at":now()})
            result=self.store.put(actor.tenant,"plan",id,plan,db,plan["version"])
            self.store.event(actor.tenant,id,"approved",plan["approval"],db)
            return result

    def cancel(self,actor,id):
        actor.require("plans.approve")
        with self.store.transaction() as db:
            plan=self.store.get(actor.tenant,"plan",id,db)
            if plan["status"] not in {"prepared","approved"}:raise Conflict("Execution has begun; compensation requires explicit recovery")
            plan["status"]="cancelled"
            return self.store.put(actor.tenant,"plan",id,plan,db,plan["version"])

    def _check_expiry(self,plan):
        if datetime.fromisoformat(plan["expires_at"])<datetime.now(timezone.utc):raise Conflict("Plan expired; replan")

    def execute(self,actor: Actor,id: str,*,lose_ack=False):
        actor.require("plans.execute")
        with self.store.transaction() as db:
            plan=self.store.get(actor.tenant,"plan",id,db)
            if plan["status"]=="completed":return plan
            if plan["status"] not in {"approved","executing","verifying","unknown"}:raise Conflict("Plan requires current approval before execution")
            if plan["status"]=="verifying":return plan
            if not plan["approval"] or plan["approval"]["plan_hash"]!=plan["plan_hash"]:raise Conflict("Approval does not cover this plan")
            first=plan["status"]=="approved"
            if first:
                self._check_expiry(plan)
                context=plan["rule_context"]
                if context and self.rules.resolve(actor,context["item"],context["day"],context["case_id"])!=context["resolved"]:
                    raise Conflict("Customer process changed; prepare a new plan")
            plan["status"]="executing"
            plan=self.store.put(actor.tenant,"plan",id,plan,db,plan["version"])
            self.store.event(actor.tenant,id,"execution_started",{"plan_hash":plan["plan_hash"]},db)
        key=id+"/action-1"
        try:
            # Query authoritative status first on recovery. Operation IDs live in the domain authority, not this worker.
            result=self.domain.operation(actor,key)
            if result is None:
                # Recovered, still-uncommitted work must satisfy current expiry/rules too.
                self._check_expiry(plan)
                context=plan["rule_context"]
                if context and self.rules.resolve(actor,context["item"],context["day"],context["case_id"])!=context["resolved"]:
                    raise Conflict("Customer process changed before mutation")
                p=plan["payload"]
                if plan["kind"]=="landed_cost":
                    result=self.domain.post_allocation(actor,p["draft_id"],p["draft_version"],plan["expected"],key,lose_ack=lose_ack)
                else: result=self.domain.start_transfer(actor,p,plan["expected"],key,lose_ack=lose_ack)
            return self._record_result(actor,id,"verifying",result["result"],None,"command_confirmed")
        except TimeoutError as exc:
            return self._record_result(actor,id,"unknown",None,str(exc),"command_unknown")
        except InventoryError as exc:
            self._record_result(actor,id,"exception",None,str(exc),"command_rejected")
            raise

    def _record_result(self,actor,id,status,result,error,event):
        with self.store.transaction() as db:
            plan=self.store.get(actor.tenant,"plan",id,db)
            # A concurrent recovering worker may already have confirmed the result.
            if plan["status"]=="completed" or (status=="unknown" and plan["status"]=="verifying"):return plan
            plan.update(status=status,result=result,error=error)
            updated=self.store.put(actor.tenant,"plan",id,plan,db,plan["version"])
            self.store.event(actor.tenant,id,event,{"result":result,"error":error},db)
            return updated

    def verify(self,actor: Actor,id: str):
        actor.require("plans.verify")
        plan=self.get(actor,id)
        if plan["status"]=="completed":return plan
        if plan["status"]!="verifying":raise Conflict("Cannot verify before authoritative command confirmation")
        p=plan["payload"];r=plan["result"]
        if plan["kind"]=="landed_cost":
            tx=self.domain.get(actor,"draft",r["transaction_id"])
            checks={"transaction_posted":tx["status"]=="posted","costing_complete":tx.get("costing_complete") is True,
                    "approved_bill_links":tx["source"]==p["source"] and tx["allocations"]==p["allocations"]}
        else:
            tx=self.domain.get(actor,"transfer",r["transaction_id"])
            dest=self.domain.get(actor,"stock",p["destination"])
            checks={"physical_receipt":tx["status"]=="received", "material_ready":quantity(dest["quantity"])-quantity(dest["protected"])>=quantity(p["required"])}
        with self.store.transaction() as db:
            current=self.store.get(actor.tenant,"plan",id,db)
            current["verification"]={"checks":checks,"at":now(),"scope":"simulated single-component material readiness; no production release" if plan["kind"]=="manufacturing" else "simulated posting and costing"}
            if all(checks.values()):current["status"]="completed"
            current=self.store.put(actor.tenant,"plan",id,current,db,current["version"])
            self.store.event(actor.tenant,id,"verified" if all(checks.values()) else "verification_pending",{"checks":checks},db)
            return current
