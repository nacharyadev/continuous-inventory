"""Deterministic fulfillment-routing MVP.

The router consumes only confirmed MVP contracts: OPEN sales orders, location
details including origin address, and item-location QoH/Available quantities.
It does not infer projected supply or decompose Available into hidden claims.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import combinations
from math import comb

from .common import Actor, Conflict, InventoryError, digest, money, now, quantity, uid
from .domain import DomainPort
from .store import StatePort


class FulfillmentRoutingService:
    MAX_LOCATIONS = 1000
    MAX_COMBINATIONS = 5000
    MAX_ALTERNATIVES = 3

    def __init__(self, store: StatePort, domain: DomainPort):
        self.store=store
        self.domain=domain

    def capabilities(self, actor: Actor) -> dict:
        actor.require("routing.read")
        return {
            "version":"mvp-1",
            "eligible_order_states":["OPEN"],
            "quantity_inputs":["QoH","Available"],
            "routable_quantity":"Available",
            "location_inputs":["stable L1 identifier","details","origin address"],
            "objectives":["complete fulfillment","fewest fulfillment locations","lowest configured handling cost"],
            "supports_split_routes":True,
            "supports_projected_or_inbound_supply":False,
            "execution":"approval required; simulated IOM/1TS adapter only",
        }

    def explain_order(self, actor: Actor, order_id: str) -> dict:
        actor.require("routing.read")
        order=self.domain.get_sales_order(actor,order_id)
        locations={location["id"]:location for location in self.domain.list_locations(actor)}
        lines=[]
        for line in order["lines"]:
            nodes=[]
            for balance in self.domain.list_item_location_inventory(actor,line["item"]):
                location=locations.get(balance["location"])
                if location is None or not location["active"] or balance["uom"]!=line["uom"]:
                    continue
                nodes.append({
                    "location_id":location["id"],"location_name":location["name"],
                    "origin_address":location["address"],"qoh":balance["qoh"],
                    "available":balance["available"],"uom":balance["uom"],
                    "inventory_id":balance["id"],"inventory_version":balance["version"],
                })
            lines.append({**line,"required_quantity":line["quantity"],"candidate_nodes":nodes})
        return {
            "order_id":order["id"],"order_version":order["version"],"state":order["state"],
            "routing_eligible":order["state"]=="OPEN",
            "eligibility_reason":"ORDER_OPEN" if order["state"]=="OPEN" else "ORDER_NOT_OPEN",
            "destination":order["destination"],"allow_split":order["allow_split"],
            "max_splits":order.get("max_splits"),"lines":lines,
        }

    def prepare(self, actor: Actor, order_id: str, *, request_id: str | None = None) -> dict:
        actor.require("routing.prepare")
        order=self.domain.get_sales_order(actor,order_id)
        if order["state"]!="OPEN":
            raise Conflict("Only sales orders in OPEN state are eligible for routing")
        request={"kind":"fulfillment_route","order_id":order_id,"order_version":order["version"]}
        fingerprint=digest(request)
        plan_id="route_"+digest({"tenant":actor.tenant,"request_id":request_id}) if request_id else uid("route")
        try:existing=self.store.get(actor.tenant,"route_plan",plan_id)
        except InventoryError:existing=None
        if existing:
            if existing["preparation_hash"]!=fingerprint:
                raise Conflict("Routing request ID reused for a different order version")
            return existing

        locations=[location for location in self.domain.list_locations(actor) if location["active"]]
        if len(locations)>self.MAX_LOCATIONS:
            raise InventoryError("Location query exceeds the bounded routing scope")
        balances=[]
        seen_balance_keys=set()
        for item in sorted({line["item"] for line in order["lines"]}):
            for balance in self.domain.list_item_location_inventory(actor,item):
                key=(balance["item"],balance["location"],balance["uom"])
                if key in seen_balance_keys:
                    raise InventoryError("Multiple quantity records exist for the same item, location and UOM")
                seen_balance_keys.add(key)
                balances.append(balance)

        candidate_plans=self._candidate_plans(order,locations,balances)
        if not candidate_plans:
            body={
                "kind":"fulfillment_route","order_id":order_id,"order_version":order["version"],
                "status":"no_feasible_route","reason_codes":["INSUFFICIENT_AVAILABLE_OR_SPLIT_NOT_ALLOWED"],
                "groups":[],"alternatives":[],"expected":{"order":order["version"],"inventory":{},"locations":{}},
                "preparation_hash":fingerprint,"approval":None,"result":None,"error":None,
                "expires_at":(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(),
            }
            body["plan_hash"]=digest({k:body[k] for k in ("order_id","order_version","status","groups","expected","expires_at")})
            return self._save(actor,plan_id,body,"no_feasible_route")

        selected=candidate_plans[0]
        expected_inventory={allocation["inventory_id"]:allocation["inventory_version"]
                            for group in selected["groups"] for allocation in group["allocations"]}
        expected_locations={group["location_id"]:group["location_version"] for group in selected["groups"]}
        expires=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()
        body={
            "kind":"fulfillment_route","order_id":order_id,"order_version":order["version"],
            "status":"prepared","reason_codes":["ORDER_OPEN","USES_AVAILABLE_NOT_QOH","MINIMUM_LOCATION_COUNT"],
            "groups":selected["groups"],"estimated_cost":selected["estimated_cost"],
            "cost_currency":selected["cost_currency"],"cost_scope":"configured location handling cost only",
            "alternatives":[self._alternative(candidate) for candidate in candidate_plans[1:self.MAX_ALTERNATIVES+1]],
            "expected":{"order":order["version"],"inventory":expected_inventory,"locations":expected_locations},
            "preparation_hash":fingerprint,"approval":None,"result":None,"error":None,"expires_at":expires,
        }
        body["plan_hash"]=digest({k:body[k] for k in ("order_id","order_version","groups","expected","expires_at")})
        return self._save(actor,plan_id,body,"prepared")

    def _save(self, actor: Actor, plan_id: str, body: dict, event_name: str) -> dict:
        with self.store.transaction() as db:
            plan=self.store.put(actor.tenant,"route_plan",plan_id,body,db)
            self.store.event(actor.tenant,plan_id,event_name,{"plan_hash":body["plan_hash"],"order_id":body["order_id"]},db)
            return plan

    def _candidate_plans(self, order: dict, locations: list[dict], balances: list[dict]) -> list[dict]:
        location_by_id={location["id"]:location for location in locations}
        balance_by_key={
            (balance["item"],balance["uom"],balance["location"]):balance
            for balance in balances if balance["location"] in location_by_id and quantity(balance["available"])>0
        }
        candidate_ids=sorted({key[2] for key in balance_by_key})
        if not candidate_ids:return []
        max_splits=1 if not order["allow_split"] else (order.get("max_splits") or len(candidate_ids))
        max_splits=min(max_splits,len(candidate_ids))
        evaluated=0
        for size in range(1,max_splits+1):
            count=comb(len(candidate_ids),size)
            if evaluated+count>self.MAX_COMBINATIONS:
                raise InventoryError("Routing combinations exceed bounded MVP scope; narrow candidate locations")
            feasible=[]
            for subset in combinations(candidate_ids,size):
                evaluated+=1
                candidate=self._build_candidate(order,subset,location_by_id,balance_by_key)
                if candidate:feasible.append(candidate)
            if feasible:
                return sorted(feasible,key=self._rank)
        return []

    def _build_candidate(self, order: dict, subset: tuple[str,...], locations: dict,
                         balances: dict) -> dict | None:
        required={}
        for line in order["lines"]:
            key=(line["item"],line["uom"])
            required[key]=required.get(key,Decimal(0))+quantity(line["quantity"])
        for (item,uom),amount in required.items():
            total=sum((quantity(balances[(item,uom,location)]["available"])
                       for location in subset if (item,uom,location) in balances),Decimal(0))
            if total<amount:return None

        remaining={key:quantity(balance["available"]) for key,balance in balances.items() if key[2] in subset}
        groups={location:[] for location in subset}
        ordered_locations=sorted(subset,key=lambda location:(money(locations[location]["handling_cost"]),
                                                              locations[location]["priority"],location))
        for line in order["lines"]:
            need=quantity(line["quantity"])
            for location in ordered_locations:
                key=(line["item"],line["uom"],location)
                available=remaining.get(key,Decimal(0))
                if available<=0:continue
                allocated=min(need,available)
                balance=balances[key]
                groups[location].append({
                    "line_id":line["id"],"item":line["item"],"quantity":str(allocated),"uom":line["uom"],
                    "location_id":location,"inventory_id":balance["id"],"inventory_version":balance["version"],
                    "qoh_at_plan":balance["qoh"],"available_at_plan":balance["available"],
                })
                remaining[key]-=allocated;need-=allocated
                if need==0:break
            if need!=0:return None

        route_groups=[];currencies=set();total_cost=Decimal(0);priority_total=0
        for location_id in ordered_locations:
            if not groups[location_id]:continue
            location=locations[location_id]
            cost=money(location["handling_cost"]);total_cost+=cost;currencies.add(location["currency"])
            priority_total+=location["priority"]
            route_groups.append({
                "location_id":location_id,"location_version":location["version"],
                "location_name":location["name"],"origin_address":location["address"],
                "allocations":groups[location_id],"estimated_handling_cost":str(cost),
                "currency":location["currency"],
            })
        if len(currencies)!=1:
            raise InventoryError("MVP cannot compare location costs in different currencies")
        return {"groups":route_groups,"estimated_cost":str(total_cost),"cost_currency":next(iter(currencies)),
                "priority_total":priority_total}

    @staticmethod
    def _rank(candidate: dict):
        return (len(candidate["groups"]),money(candidate["estimated_cost"]),candidate["priority_total"],
                tuple(group["location_id"] for group in candidate["groups"]))

    @staticmethod
    def _alternative(candidate: dict):
        return {"location_ids":[group["location_id"] for group in candidate["groups"]],
                "group_count":len(candidate["groups"]),"estimated_cost":candidate["estimated_cost"],
                "cost_currency":candidate["cost_currency"]}

    def get(self, actor: Actor, plan_id: str) -> dict:
        actor.require("routing.read")
        return self.store.get(actor.tenant,"route_plan",plan_id)

    def approve(self, actor: Actor, plan_id: str, expected_version: int) -> dict:
        actor.require("routing.approve")
        with self.store.transaction() as db:
            plan=self.store.get(actor.tenant,"route_plan",plan_id,db)
            if plan["version"]!=expected_version or plan["status"]!="prepared":
                raise Conflict("Only the reviewed current prepared route may be approved")
            self._check_expiry(plan)
            order=self.domain.get_sales_order(actor,plan["order_id"])
            if order["state"]!="OPEN" or order["version"]!=plan["order_version"]:
                raise Conflict("Sales order changed or is no longer OPEN; prepare a new route")
            plan.update(status="approved",approval={"actor":actor.name,"plan_hash":plan["plan_hash"],"at":now()})
            updated=self.store.put(actor.tenant,"route_plan",plan_id,plan,db,plan["version"])
            self.store.event(actor.tenant,plan_id,"approved",plan["approval"],db)
            return updated

    def execute(self, actor: Actor, plan_id: str) -> dict:
        actor.require("routing.execute")
        plan=self.get(actor,plan_id)
        if plan["status"]=="executed":return plan
        if plan["status"]!="approved" or not plan["approval"] or plan["approval"]["plan_hash"]!=plan["plan_hash"]:
            raise Conflict("Route requires current approval before execution")
        self._check_expiry(plan)
        key=plan_id+"/fulfillment"
        try:
            operation=self.domain.operation(actor,key)
            if operation is None:
                operation=self.domain.apply_fulfillment_route(actor,plan_id=plan_id,order_id=plan["order_id"],
                    order_version=plan["order_version"],groups=plan["groups"],expected=plan["expected"],key=key)
            with self.store.transaction() as db:
                current=self.store.get(actor.tenant,"route_plan",plan_id,db)
                if current["status"]=="executed":return current
                current.update(status="executed",result=operation["result"],error=None)
                updated=self.store.put(actor.tenant,"route_plan",plan_id,current,db,current["version"])
                self.store.event(actor.tenant,plan_id,"executed",operation["result"],db)
                return updated
        except InventoryError as exc:
            with self.store.transaction() as db:
                current=self.store.get(actor.tenant,"route_plan",plan_id,db)
                if current["status"]=="approved":
                    current.update(status="stale",error=str(exc))
                    self.store.put(actor.tenant,"route_plan",plan_id,current,db,current["version"])
                    self.store.event(actor.tenant,plan_id,"execution_rejected",{"error":str(exc)},db)
            raise

    @staticmethod
    def _check_expiry(plan: dict):
        if datetime.fromisoformat(plan["expires_at"])<datetime.now(timezone.utc):
            raise Conflict("Route expired; prepare a new route")
