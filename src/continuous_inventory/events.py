"""Local-only Kafka contract simulator.

This module persists the envelope a Kafka consumer would receive. It deliberately
does not implement a broker, polling loop, or authoritative domain mutation.
"""
from __future__ import annotations

from datetime import datetime

from .common import Actor, Conflict, InventoryError, digest, now
from .store import StatePort


class LocalEventIngress:
    def __init__(self, store: StatePort):
        self.store = store

    def ingest(self, actor: Actor, envelope: dict) -> dict:
        actor.require("events.ingest")
        required = {"event_id", "event_type", "schema_version", "source", "occurred_at", "aggregate", "payload"}
        if set(envelope) - (required | {"correlation_id", "causation_id", "metadata"}):
            raise InventoryError("Event envelope contains unsupported fields")
        missing = sorted(required - set(envelope))
        if missing:
            raise InventoryError("Missing event fields: " + ", ".join(missing))
        event_id = envelope["event_id"]
        event_type = envelope["event_type"]
        aggregate = envelope["aggregate"]
        if not isinstance(event_id, str) or not event_id or len(event_id) > 160:
            raise InventoryError("event_id must be a nonempty string up to 160 characters")
        if not isinstance(event_type, str) or "." not in event_type or len(event_type) > 160:
            raise InventoryError("event_type must be a versionable dotted name")
        if not isinstance(envelope["schema_version"], int) or envelope["schema_version"] < 1:
            raise InventoryError("schema_version must be a positive integer")
        if not isinstance(envelope["source"], str) or not envelope["source"]:
            raise InventoryError("source is required")
        try:
            datetime.fromisoformat(str(envelope["occurred_at"]).replace("Z", "+00:00"))
        except ValueError:
            raise InventoryError("occurred_at must be ISO-8601") from None
        if not isinstance(aggregate, dict) or set(aggregate) != {"type", "id", "version"}:
            raise InventoryError("aggregate requires exactly type, id, and version")
        if not all(isinstance(aggregate[k], str) and aggregate[k] for k in ("type", "id")):
            raise InventoryError("aggregate type and id are required")
        if not isinstance(aggregate["version"], int) or aggregate["version"] < 1:
            raise InventoryError("aggregate version must be a positive integer")
        if not isinstance(envelope["payload"], dict):
            raise InventoryError("payload must be an object")

        fingerprint = digest(envelope)
        stream_id = aggregate["type"] + "/" + aggregate["id"]
        with self.store.transaction() as db:
            try:
                prior = self.store.get(actor.tenant, "inbox_event", event_id, db)
            except InventoryError:
                prior = None
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise Conflict("event_id was reused with different content")
                return {**prior, "duplicate": True}

            versions = self.store.list(actor.tenant, "event_stream", db, filters={"stream_id": stream_id}, limit=1)
            last_version = versions[0]["last_version"] if versions else 0
            incoming = aggregate["version"]
            if incoming <= last_version:
                ordering = "stale"
            elif incoming == last_version + 1:
                ordering = "contiguous"
            else:
                ordering = "gap"
            body = {
                "fingerprint": fingerprint,
                "event_type": event_type,
                "schema_version": envelope["schema_version"],
                "source": envelope["source"],
                "occurred_at": envelope["occurred_at"],
                "recorded_at": now(),
                "aggregate": aggregate,
                "payload": envelope["payload"],
                "correlation_id": envelope.get("correlation_id"),
                "causation_id": envelope.get("causation_id"),
                "metadata": envelope.get("metadata", {}),
                "ordering": ordering,
                "status": "accepted" if ordering == "contiguous" else "held",
            }
            event = self.store.put(actor.tenant, "inbox_event", event_id, body, db)
            if ordering == "contiguous":
                stream_body = {"stream_id": stream_id, "last_version": incoming, "last_event_id": event_id}
                if versions:
                    self.store.put(actor.tenant, "event_stream", stream_id, stream_body, db, versions[0]["version"])
                else:
                    self.store.put(actor.tenant, "event_stream", stream_id, stream_body, db)
            self.store.event(actor.tenant, event_id, "event.ingested", {"ordering": ordering, "status": body["status"]}, db)
            return {**event, "duplicate": False}

    def get(self, actor: Actor, event_id: str) -> dict:
        actor.require("events.read")
        return self.store.get(actor.tenant, "inbox_event", event_id)
