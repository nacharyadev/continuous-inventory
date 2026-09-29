from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .common import Actor, Conflict, InventoryError, digest, now, quantity, uid
from .store import StatePort


class DemandService:
    """CSV demand ingestion with explicit source snapshots and identity reconciliation."""

    def __init__(self, store: StatePort):
        self.store = store

    def register_source(self, actor: Actor, id: str, label: str, *, max_age_hours: int = 24):
        actor.require("demand.write")
        if not id or max_age_hours <= 0:
            raise InventoryError("Source ID and positive freshness window are required")
        with self.store.transaction() as db:
            return self.store.put(actor.tenant, "source", id, {"label": label, "max_age_hours": max_age_hours, "active_batch": None, "last_import": None}, db)

    def stage_csv(self, actor: Actor, source: str, content: str, *, source_version: str,
                  kind: str, item_map: dict[str, str], location_map: dict[str, str],
                  mode: str = "snapshot") -> dict:
        actor.require("demand.write")
        self.store.get(actor.tenant, "source", source)
        if kind not in {"confirmed_order", "forecast", "pipeline"} or mode != "snapshot":
            raise InventoryError("Use a typed demand source and explicit complete snapshot; deltas are not supported yet")
        if not source_version or len(content.encode()) > 2_000_000:
            raise InventoryError("Source version required; file limit is 2 MB")
        fingerprint = digest({"source": source, "source_version": source_version, "content": content,
                              "kind": kind, "item_map": item_map, "location_map": location_map, "mode": mode})
        rows, issues, seen = [], [], set()
        reader = csv.DictReader(io.StringIO(content))
        required = {"external_id", "item", "location", "quantity", "uom", "date"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise InventoryError("CSV headers required: " + ", ".join(sorted(required)))
        for number, raw in enumerate(reader, 2):
            try:
                if None in raw or any(v is None for v in raw.values()):
                    raise InventoryError("Malformed CSV row")
                if any(str(v).lstrip().startswith(("=", "+", "@")) for v in raw.values()):
                    raise InventoryError("Formula-like cells are not accepted")
                external = raw["external_id"].strip()
                if not external or external in seen:
                    raise InventoryError("Missing or duplicate external line ID")
                seen.add(external)
                if raw["item"] not in item_map or raw["location"] not in location_map:
                    raise InventoryError("Unmapped item or location")
                if raw["uom"] != "each":
                    raise InventoryError("Prototype supports explicit 'each' UOM only")
                qty = quantity(raw["quantity"])
                day = date.fromisoformat(raw["date"]).isoformat()
                rows.append({"external_id": external, "item": item_map[raw["item"]], "location": location_map[raw["location"]],
                             "quantity": str(qty), "uom": "each", "date": day})
            except (InventoryError, ValueError) as exc:
                issues.append({"row": number, "reason": str(exc)})
        with self.store.transaction() as db:
            existing = [b for b in self.store.list(actor.tenant, "batch", db, filters={"fingerprint": fingerprint}) if b["fingerprint"] == fingerprint]
            if existing:
                return existing[0]
            for batch in self.store.list(actor.tenant, "batch", db, filters={"source": source, "source_version": source_version}):
                if batch["source"] == source and batch["source_version"] == source_version and batch["status"] == "active":
                    raise Conflict("Source version already active with different contents")
            return self.store.put(actor.tenant, "batch", uid("batch"), {"source": source, "source_version": source_version,
                "kind": kind, "mode": mode, "fingerprint": fingerprint, "rows": rows, "issues": issues, "status": "staged"}, db)

    def activate(self, actor: Actor, batch_id: str) -> dict:
        actor.require("demand.activate")
        with self.store.transaction() as db:
            batch = self.store.get(actor.tenant, "batch", batch_id, db)
            if batch["issues"]:
                raise InventoryError("Resolve all import issues before activation")
            if batch["status"] == "active":
                return batch
            if batch["status"] != "staged":
                raise Conflict("Superseded snapshots cannot be silently reactivated")
            for other in self.store.list(actor.tenant, "batch", db, filters={"source": batch["source"], "source_version": batch["source_version"]}):
                if other["id"] != batch_id and other["source"] == batch["source"] and other["source_version"] == batch["source_version"] and other["status"] in {"active", "superseded"}:
                    raise Conflict("A different import already used this source version")
            source = self.store.get(actor.tenant, "source", batch["source"], db)
            if source["active_batch"]:
                old = self.store.get(actor.tenant, "batch", source["active_batch"], db)
                old["status"] = "superseded"
                self.store.put(actor.tenant, "batch", old["id"], old, db, old["version"])
            source.update(active_batch=batch_id, last_import=now())
            self.store.put(actor.tenant, "source", source["id"], source, db, source["version"])
            batch["status"] = "active"
            result = self.store.put(actor.tenant, "batch", batch_id, batch, db, batch["version"])
            self.store.event(actor.tenant, batch_id, "demand.activated", {"source": source["id"], "fingerprint": batch["fingerprint"]}, db)
            return result

    def record_internal_order(self, actor: Actor, line_id: str, *, item: str, location: str, day: str,
                              amount: str, external_source: str | None = None, external_id: str | None = None):
        actor.require("demand.write")
        if bool(external_source) != bool(external_id):
            raise InventoryError("External source and line ID must be supplied together")
        with self.store.transaction() as db:
            if external_source:
                self.store.get(actor.tenant, "source", external_source, db)
            return self.store.put(actor.tenant, "internal_order", line_id, {"item": item, "location": location,
                "date": date.fromisoformat(day).isoformat(), "quantity": str(quantity(amount)),
                "external_source": external_source, "external_id": external_id}, db)

    def outlook(self, actor: Actor, item: str, location: str, day: str, *, forecast_source: str | None = None) -> dict:
        actor.require("demand.read")
        date.fromisoformat(day)
        with self.store.connect() as db:
            all_internal = self.store.list(actor.tenant, "internal_order", db, filters={"item": item})
            internal = [r for r in all_internal if (r["item"], r["location"], r["date"]) == (item, location, day)]
            confirmed = sum((quantity(r["quantity"]) for r in internal), Decimal(0))
            external_remaining = Decimal(0); forecast = Decimal(0); pipeline = Decimal(0); issues = []; sources = []
            for source in self.store.list(actor.tenant, "source", db):
                if not source["active_batch"]:
                    issues.append({"source": source["id"], "reason": "missing"}); continue
                if datetime.now(timezone.utc) - datetime.fromisoformat(source["last_import"]) > timedelta(hours=source["max_age_hours"]):
                    issues.append({"source": source["id"], "reason": "stale"})
                batch = self.store.get(actor.tenant, "batch", source["active_batch"], db)
                sources.append({"source": source["id"], "batch": batch["id"], "version": batch["source_version"]})
                for row in batch["rows"]:
                    if (row["item"], row["location"], row["date"]) != (item, location, day): continue
                    qty = quantity(row["quantity"])
                    if batch["kind"] == "confirmed_order":
                        linked = [r for r in all_internal if r["external_source"] == source["id"] and r["external_id"] == row["external_id"]]
                        if any((r["item"], r["location"], r["date"]) != (item,location,day) for r in linked):
                            issues.append({"source": source["id"], "reason": "linked_order_scope_conflict"})
                            continue
                        consumed = sum((quantity(r["quantity"]) for r in linked), Decimal(0))
                        if consumed > qty:
                            issues.append({"source": source["id"], "reason": "internal_exceeds_external"})
                        external_remaining += max(Decimal(0), qty-consumed)
                    elif batch["kind"] == "forecast" and source["id"] == forecast_source:
                        forecast += qty
                    elif batch["kind"] == "pipeline":
                        pipeline += qty
            if forecast_source:
                selected_source = self.store.get(actor.tenant, "source", forecast_source, db)
                if selected_source["active_batch"] and self.store.get(actor.tenant, "batch", selected_source["active_batch"], db)["kind"] != "forecast":
                    raise InventoryError("Selected source is not a forecast")
            if forecast_source and not any(s["source"] == forecast_source for s in sources):
                raise InventoryError("Selected forecast source has no active snapshot")
            confirmed += external_remaining
            return {"confirmed": str(confirmed), "external_remaining": str(external_remaining), "forecast_total": str(forecast),
                    "unconsumed_forecast": str(max(Decimal(0),forecast-confirmed)), "planning_demand": str(max(confirmed,forecast)),
                    "tentative_pipeline": str(pipeline), "issues": issues, "source_versions": sources,
                    "semantics": "selected total-day forecast consumed by confirmed demand; pipeline excluded"}

    def declare_event(self, actor: Actor, *, title: str, starts: str | None = None, ends: str | None = None,
                      item: str | None = None, location: str | None = None, projected_quantity: str | None = None):
        """Retain intent with material gaps; never invent an uplift or auto-activate demand."""
        actor.require("demand.write")
        if not title.strip():raise InventoryError("Event title required")
        if starts:date.fromisoformat(starts)
        if ends:date.fromisoformat(ends)
        if starts and ends and starts>ends:raise InventoryError("Event end precedes start")
        if projected_quantity is not None:quantity(projected_quantity)
        fields={"starts":starts,"ends":ends,"item":item,"location":location,"projected_quantity":projected_quantity}
        missing=[k for k,v in fields.items() if v is None]
        with self.store.transaction() as db:
            return self.store.put(actor.tenant,"sales_event",uid("event"),{"title":title,**fields,"missing":missing,
                "status":"needs_clarification" if missing else "ready_for_scenario_review","activated":False},db)
