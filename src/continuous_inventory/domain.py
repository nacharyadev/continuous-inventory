"""Explicitly simulated authoritative adapter; never connects to IOM or 1TS."""
from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from .common import Actor, Conflict, InventoryError, digest, money, quantity, uid
from .store import StatePort, InMemoryState


class DomainPort(Protocol):
    def get(self, actor: Actor, kind: str, id: str) -> dict: ...
    def get_sales_order(self, actor: Actor, id: str) -> dict: ...
    def list_locations(self, actor: Actor) -> list[dict]: ...
    def list_item_location_inventory(self, actor: Actor, item: str) -> list[dict]: ...
    def apply_fulfillment_route(self, actor: Actor, *, plan_id: str, order_id: str,
                                order_version: int, groups: list[dict], expected: dict,
                                key: str) -> dict: ...
    def prepare_allocation(self, actor: Actor, id: str, source: str, allocations: list[dict], currency: str) -> dict: ...
    def post_allocation(self, actor: Actor, draft_id: str, draft_version: int, expected: dict,
                        key: str, *, lose_ack: bool = False) -> dict: ...
    def start_transfer(self, actor: Actor, payload: dict, expected: dict, key: str,
                       *, lose_ack: bool = False) -> dict: ...
    def operation(self, actor: Actor, key: str) -> dict | None: ...


class SimulatedDomain:
    def __init__(self, state: StatePort | None = None):
        self.store=state if state is not None else InMemoryState()

    def seed_bill(self, actor: Actor, id: str, *, kind: str, amount: str, currency="USD"):
        actor.require("domain.seed")
        if kind not in {"freight","inventory"}:raise InventoryError("Unsupported bill kind")
        with self.store.transaction() as db:
            return self.store.put(actor.tenant,"bill",id,{"kind":kind,"amount":str(money(amount)),"currency":currency,
                "allocated":"0","added_cost":"0","period_open":True},db)

    def seed_stock(self, actor: Actor, id: str, *, item: str, location: str, amount: str, protected: str="0"):
        actor.require("domain.seed")
        q=quantity(amount);p=quantity(protected)
        if p>q:raise InventoryError("Protected quantity exceeds stock")
        with self.store.transaction() as db:
            return self.store.put(actor.tenant,"stock",id,{"item":item,"location":location,"quantity":str(q),"protected":str(p),"tracking":"none"},db)

    def seed_sales_order(self, actor: Actor, id: str, *, state: str, lines: list[dict],
                         destination: dict, allow_split: bool = True, max_splits: int | None = None):
        """Seed a simulated 1TS sales order. This is not a production order API."""
        actor.require("domain.seed")
        if not isinstance(state, str) or not state:
            raise InventoryError("Sales order state required")
        if not lines:
            raise InventoryError("Sales order lines required")
        seen=set(); normalized=[]
        for line in lines:
            if set(line)!={"id","item","quantity","uom"} or not line["id"] or not line["item"] or not line["uom"]:
                raise InventoryError("Invalid sales order line")
            if line["id"] in seen:
                raise InventoryError("Duplicate sales order line")
            seen.add(line["id"])
            normalized.append({**line,"quantity":str(quantity(line["quantity"],positive=True))})
        if not isinstance(destination,dict) or not destination.get("country"):
            raise InventoryError("Destination address with country required")
        if max_splits is not None and (not isinstance(max_splits,int) or max_splits<1):
            raise InventoryError("max_splits must be a positive integer")
        body={"state":state,"lines":normalized,"destination":destination,"allow_split":bool(allow_split),
              "max_splits":max_splits,"routing_status":"unrouted"}
        with self.store.transaction() as db:
            return self.store.put(actor.tenant,"sales_order",id,body,db)

    def seed_location(self, actor: Actor, id: str, *, name: str, address: dict,
                      active: bool = True, priority: int = 100,
                      handling_cost: str = "0", currency: str = "USD"):
        """Seed a simulated location query response, including origin address."""
        actor.require("domain.seed")
        if not name or not isinstance(address,dict) or not address.get("country"):
            raise InventoryError("Location name and address with country required")
        if not isinstance(priority,int) or priority<0:
            raise InventoryError("Location priority must be a nonnegative integer")
        body={"name":name,"address":address,"active":bool(active),"priority":priority,
              "handling_cost":str(money(handling_cost)),"currency":currency}
        with self.store.transaction() as db:
            return self.store.put(actor.tenant,"location",id,body,db)

    def seed_item_location_inventory(self, actor: Actor, id: str, *, item: str, location: str,
                                     qoh: str, available: str, uom: str = "each"):
        """Seed the MVP item-location quantity contract: QoH plus Available only."""
        actor.require("domain.seed")
        q=quantity(qoh); a=quantity(available)
        if a>q:
            raise InventoryError("Available cannot exceed QoH in the simulator")
        if not item or not location or not uom:
            raise InventoryError("Item, location and UOM required")
        self.store.get(actor.tenant,"location",location)
        with self.store.transaction() as db:
            return self.store.put(actor.tenant,"item_location_inventory",id,
                                  {"item":item,"location":location,"qoh":str(q),
                                   "available":str(a),"uom":uom},db)

    def get(self, actor: Actor, kind: str, id: str):
        actor.require("domain.read")
        return self.store.get(actor.tenant,kind,id)

    def get_sales_order(self, actor: Actor, id: str):
        return self.get(actor,"sales_order",id)

    def list_locations(self, actor: Actor):
        actor.require("domain.read")
        return self.store.list(actor.tenant,"location",limit=1000)

    def list_item_location_inventory(self, actor: Actor, item: str):
        actor.require("domain.read")
        return self.store.list(actor.tenant,"item_location_inventory",filters={"item":item},limit=1000)

    def operation(self, actor: Actor, key: str):
        actor.require("domain.read")
        try:return self.store.get(actor.tenant,"operation",key)
        except InventoryError:return None

    def prepare_allocation(self, actor: Actor, id: str, source: str, allocations: list[dict], currency: str):
        actor.require("domain.draft")
        with self.store.transaction() as db:
            self._validate_allocations(actor,source,allocations,currency,db)
            body={"source":source,"allocations":allocations,"currency":currency,"status":"draft"}
            try:
                old=self.store.get(actor.tenant,"draft",id,db)
            except InventoryError:old=None
            if old:
                if any(old[k]!=v for k,v in body.items()):raise Conflict("Draft identity reused with different payload")
                return old
            return self.store.put(actor.tenant,"draft",id,body,db)

    def edit_draft(self, actor: Actor, id: str, allocations: list[dict]):
        actor.require("domain.draft")
        with self.store.transaction() as db:
            old=self.store.get(actor.tenant,"draft",id,db)
            if old["status"]!="draft":raise Conflict("Posted transaction cannot be edited as a draft")
            self._validate_allocations(actor,old["source"],allocations,old["currency"],db)
            old["allocations"]=allocations
            return self.store.put(actor.tenant,"draft",id,old,db,old["version"])

    def _validate_allocations(self, actor, source_id, allocations, currency,db):
        source=self.store.get(actor.tenant,"bill",source_id,db)
        if source["kind"]!="freight" or source["currency"]!=currency or not source["period_open"]:
            raise InventoryError("Invalid charge currency/type or closed period")
        if not allocations:raise InventoryError("Allocation targets required")
        seen=set();total=Decimal(0)
        for line in allocations:
            if set(line)!={"bill_id","amount"} or line["bill_id"] in seen:raise InventoryError("Invalid/duplicate allocation target")
            seen.add(line["bill_id"])
            target=self.store.get(actor.tenant,"bill",line["bill_id"],db)
            if target["kind"]!="inventory" or target["currency"]!=currency or not target["period_open"]:
                raise InventoryError("Invalid target currency/type or closed period")
            amount=money(line["amount"])
            if amount<=0:raise InventoryError("Allocation must be positive")
            total+=amount
        if total>money(source["amount"])-money(source["allocated"]):raise Conflict("Source charge already allocated or insufficient")
        return source,total

    def post_allocation(self, actor, draft_id, draft_version, expected, key, *, lose_ack=False):
        actor.require("domain.post")
        request={"kind":"post_allocation","draft":draft_id,"draft_version":draft_version,"expected":expected}
        with self.store.transaction() as db:
            prior=self._prior(actor,key,request,db)
            if prior:return prior
            draft=self.store.get(actor.tenant,"draft",draft_id,db)
            if draft["version"]!=draft_version or draft["status"]!="draft":raise Conflict("Draft changed after approval")
            if set(expected)!={draft["source"], *(line["bill_id"] for line in draft["allocations"])}:
                raise InventoryError("Expected versions must cover exactly the source and target bills")
            for id,version in expected.items():
                if self.store.get(actor.tenant,"bill",id,db)["version"]!=version:raise Conflict("Bill changed after preparation")
            source,total=self._validate_allocations(actor,draft["source"],draft["allocations"],draft["currency"],db)
            source["allocated"]=str(money(source["allocated"])+total)
            self.store.put(actor.tenant,"bill",source["id"],source,db,source["version"])
            for line in draft["allocations"]:
                target=self.store.get(actor.tenant,"bill",line["bill_id"],db)
                target["added_cost"]=str(money(target["added_cost"])+money(line["amount"]))
                self.store.put(actor.tenant,"bill",target["id"],target,db,target["version"])
            draft.update(status="posted",costing_complete=True)
            posted=self.store.put(actor.tenant,"draft",draft_id,draft,db,draft["version"])
            result=self._save_operation(actor,key,request,{"transaction_id":draft_id,"type":"inventory_cost_allocation","status":"posted","costing_complete":True},db)
        if lose_ack:raise TimeoutError("Simulated acknowledgment lost after posting")
        return result

    def _prior(self,actor,key,request,db):
        try:prior=self.store.get(actor.tenant,"operation",key,db)
        except InventoryError:return None
        if prior["request_hash"]!=digest(request):raise Conflict("Idempotency key reused with a different request")
        return prior

    def _save_operation(self,actor,key,request,result,db):
        return self.store.put(actor.tenant,"operation",key,{"request_hash":digest(request),"result":result},db)

    def apply_fulfillment_route(self, actor: Actor, *, plan_id: str, order_id: str,
                                order_version: int, groups: list[dict], expected: dict,
                                key: str):
        """Atomically simulate assignment and fulfillment-order creation.

        Production implementations must delegate these effects to authoritative IOM/1TS
        commands and preserve the same idempotency and concurrency contract.
        """
        actor.require("domain.fulfillment")
        request={"kind":"fulfillment_route","plan_id":plan_id,"order_id":order_id,
                 "order_version":order_version,"groups":groups,"expected":expected}
        with self.store.transaction() as db:
            prior=self._prior(actor,key,request,db)
            if prior:return prior
            order=self.store.get(actor.tenant,"sales_order",order_id,db)
            if order["version"]!=order_version or order["state"]!="OPEN":
                raise Conflict("Sales order changed or is no longer OPEN; prepare a new route")
            if expected.get("order")!=order_version:
                raise InventoryError("Expected order version does not match route")

            order_lines={line["id"]:line for line in order["lines"]}
            allocated_by_line={line_id:Decimal(0) for line_id in order_lines}
            allocated_by_balance={}
            fulfillment_ids=[]
            assignment_ids=[]
            for group in groups:
                location=self.store.get(actor.tenant,"location",group["location_id"],db)
                if not location["active"]:
                    raise Conflict("Fulfillment location is no longer active")
                if expected.get("locations",{}).get(location["id"])!=location["version"]:
                    raise Conflict("Fulfillment location changed; prepare a new route")
                for allocation in group["allocations"]:
                    line=order_lines.get(allocation["line_id"])
                    if line is None or line["item"]!=allocation["item"] or line["uom"]!=allocation["uom"]:
                        raise InventoryError("Route allocation does not match the sales order")
                    amount=quantity(allocation["quantity"],positive=True)
                    allocated_by_line[line["id"]]+=amount
                    balance_id=allocation["inventory_id"]
                    current=allocated_by_balance.setdefault(balance_id,{"quantity":Decimal(0),"allocation":allocation})
                    current["quantity"]+=amount

            for line_id,line in order_lines.items():
                if allocated_by_line[line_id]!=quantity(line["quantity"]):
                    raise InventoryError("Executed route must cover every sales order line exactly")

            for balance_id,claim in allocated_by_balance.items():
                balance=self.store.get(actor.tenant,"item_location_inventory",balance_id,db)
                allocation=claim["allocation"]
                if expected.get("inventory",{}).get(balance_id)!=balance["version"]:
                    raise Conflict("Available quantity changed; prepare a new route")
                if balance["item"]!=allocation["item"] or balance["location"]!=allocation["location_id"] or balance["uom"]!=allocation["uom"]:
                    raise InventoryError("Route inventory reference is inconsistent")
                if claim["quantity"]>quantity(balance["available"]):
                    raise Conflict("Available quantity is no longer sufficient; prepare a new route")
                balance["available"]=str(quantity(balance["available"])-claim["quantity"])
                self.store.put(actor.tenant,"item_location_inventory",balance_id,balance,db,balance["version"])

            for index,group in enumerate(groups,1):
                suffix=digest({"plan":plan_id,"group":index,"location":group["location_id"]})[:24]
                assignment_id=f"assignment_{suffix}"
                fulfillment_id=f"fulfillment_{suffix}"
                assignment=self.store.put(actor.tenant,"assignment",assignment_id,
                    {"plan_id":plan_id,"order_id":order_id,"location":group["location_id"],
                     "allocations":group["allocations"],"status":"assigned"},db)
                fulfillment=self.store.put(actor.tenant,"fulfillment_order",fulfillment_id,
                    {"plan_id":plan_id,"order_id":order_id,"location":group["location_id"],
                     "origin_address":group["origin_address"],"allocations":group["allocations"],
                     "assignment_id":assignment_id,"status":"created"},db)
                assignment_ids.append(assignment["id"]);fulfillment_ids.append(fulfillment["id"])

            order.update(routing_status="routed",route_plan_id=plan_id,
                         assignment_ids=assignment_ids,fulfillment_order_ids=fulfillment_ids)
            self.store.put(actor.tenant,"sales_order",order_id,order,db,order["version"])
            result={"plan_id":plan_id,"order_id":order_id,"status":"executed",
                    "assignment_ids":assignment_ids,"fulfillment_order_ids":fulfillment_ids}
            return self._save_operation(actor,key,request,result,db)

    def start_transfer(self,actor,payload,expected,key,*,lose_ack=False):
        actor.require("domain.transfer")
        request={"kind":"transfer","payload":payload,"expected":expected}
        with self.store.transaction() as db:
            prior=self._prior(actor,key,request,db)
            if prior:return prior
            source=self.store.get(actor.tenant,"stock",payload["source"],db)
            dest=self.store.get(actor.tenant,"stock",payload["destination"],db)
            if source["id"]==dest["id"] or source["item"]!=dest["item"] or source["item"]!=payload["item"]:
                raise InventoryError("Invalid transfer identities")
            if source["tracking"]!="none" or dest["tracking"]!="none":raise InventoryError("Tracked items require a real tracking adapter")
            for stock in (source,dest):
                if stock["version"]!=expected[stock["id"]]:raise Conflict("Stock changed; replan")
            amount=quantity(payload["quantity"],positive=True)
            if amount>quantity(source["quantity"])-quantity(source["protected"]):raise Conflict("Protected stock would be consumed")
            source["quantity"]=str(quantity(source["quantity"])-amount)
            self.store.put(actor.tenant,"stock",source["id"],source,db,source["version"])
            transfer=self.store.put(actor.tenant,"transfer",uid("transfer"),{**payload,"status":"in_transit","received":"0"},db)
            result=self._save_operation(actor,key,request,{"transaction_id":transfer["id"],"type":"transfer","status":"in_transit"},db)
        if lose_ack:raise TimeoutError("Simulated acknowledgment lost after transfer dispatch")
        return result

    def confirm_receipt(self,actor,transfer_id,amount,receipt_key):
        actor.require("domain.receive")
        request={"transfer":transfer_id,"amount":str(quantity(amount,positive=True))}
        with self.store.transaction() as db:
            prior=self._prior(actor,"receipt/"+receipt_key,request,db)
            if prior:return prior
            transfer=self.store.get(actor.tenant,"transfer",transfer_id,db)
            received=quantity(transfer["received"])+quantity(amount,positive=True)
            if received>quantity(transfer["quantity"]):raise Conflict("Receipt exceeds transfer quantity")
            dest=self.store.get(actor.tenant,"stock",transfer["destination"],db)
            dest["quantity"]=str(quantity(dest["quantity"])+quantity(amount))
            self.store.put(actor.tenant,"stock",dest["id"],dest,db,dest["version"])
            transfer.update(received=str(received),status="received" if received==quantity(transfer["quantity"]) else "partially_received")
            self.store.put(actor.tenant,"transfer",transfer_id,transfer,db,transfer["version"])
            return self._save_operation(actor,"receipt/"+receipt_key,request,{"status":transfer["status"]},db)
