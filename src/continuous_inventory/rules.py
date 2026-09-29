from __future__ import annotations

from datetime import date
from .common import Actor, Conflict, InventoryError, now, uid
from .store import StatePort


class RuleService:
    def __init__(self, store: StatePort):
        self.store = store

    def teach(self, actor: Actor, *, scope: str, mode: str, item: str | None = None,
              case_id: str | None = None, starts: str | None = None, ends: str | None = None,
              batch_limit: str | None = None, reason: str = ""):
        actor.require("rules.write")
        if scope not in {"once", "item", "business"} or mode not in {"order_driven", "stock_based"}:
            raise InventoryError("Choose correction scope and an explicit supported demand trigger")
        if (scope == "item" and not item) or (scope == "once" and not case_id):
            raise InventoryError("Item or case scope requires its identity")
        if scope == "business": actor.require("rules.business")
        if (starts is None) != (ends is None) or (starts and date.fromisoformat(starts)>date.fromisoformat(ends)):
            raise InventoryError("Provide an ordered start/end pair")
        if starts: date.fromisoformat(ends)
        if batch_limit is not None:
            from .common import quantity
            quantity(batch_limit, positive=True)
        body = {"scope": scope, "mode": mode, "item": item if scope=="item" else None,
                "case_id": case_id if scope=="once" else None, "starts": starts, "ends": ends,
                "batch_limit": batch_limit, "reason": reason, "author": actor.name, "active": True}
        with self.store.transaction() as db:
            for rule in self.store.list(actor.tenant, "rule", db):
                same_scope = (rule["scope"],rule["item"],rule["case_id"]) == (body["scope"],body["item"],body["case_id"])
                overlap = max(rule["starts"] or "0001-01-01",starts or "0001-01-01") <= min(rule["ends"] or "9999-12-31",ends or "9999-12-31")
                if rule["active"] and same_scope and overlap:
                    raise Conflict("Overlapping rule: explicitly revoke or replace the old instruction")
            rule=self.store.put(actor.tenant,"rule",uid("rule"),body,db)
            self.store.event(actor.tenant,rule["id"],"rule.taught",{"scope":scope,"author":actor.name},db)
            return rule

    def revoke(self, actor: Actor, id: str):
        actor.require("rules.write")
        with self.store.transaction() as db:
            rule=self.store.get(actor.tenant,"rule",id,db)
            if rule["scope"]=="business": actor.require("rules.business")
            rule["active"]=False
            return self.store.put(actor.tenant,"rule",id,rule,db,rule["version"])

    def resolve(self, actor: Actor, item: str, day: str, case_id: str | None = None) -> dict:
        actor.require("rules.read"); date.fromisoformat(day)
        rules=[]
        for r in self.store.list(actor.tenant,"rule"):
            if not r["active"] or not (r["starts"] or "0001-01-01") <= day <= (r["ends"] or "9999-12-31"): continue
            if r["scope"]=="item" and r["item"]!=item: continue
            if r["scope"]=="once" and r["case_id"]!=case_id: continue
            rules.append(r)
        # Mandatory enterprise constraints are not supported; conflicting instructions require review.
        if len({r["mode"] for r in rules})>1:
            raise Conflict("Applicable rules disagree; an authorized customer must resolve precedence")
        ranks={"business":0,"item":1,"once":2}
        rules.sort(key=lambda r:ranks[r["scope"]])
        return {"mode": rules[-1]["mode"] if rules else None, "batch_limit":rules[-1]["batch_limit"] if rules else None,
                "rule_versions":{r["id"]:r["version"] for r in rules}, "needs_teaching":not bool(rules)}
