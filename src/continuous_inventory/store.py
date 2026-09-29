"""Caller-owned state port plus an in-memory simulation implementation.

Workers do not own authoritative in-process state. SQLite is the only implemented
persistence adapter. A future multi-host adapter must preserve this port's atomic
versions and transaction semantics. Existing domain services remain authoritative.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from typing import Any, ContextManager, Protocol

from .common import Conflict, InventoryError, now


class StatePort(Protocol):
    """Storage-neutral, tenant-scoped unit-of-work contract.

    Remote implementations must provide atomic conditional writes and event append,
    with tenant isolation and bounded indexed queries. 'db' is an opaque caller-owned
    unit-of-work handle, not a database connection supplied by this application.
    """
    def transaction(self) -> ContextManager[Any]: ...
    def connect(self) -> ContextManager[Any]: ...
    def get(self, tenant: str, kind: str, id: str, db=None) -> dict: ...
    def list(self, tenant: str, kind: str, db=None, *, filters: dict | None = None, limit: int = 1000) -> list[dict]: ...
    def put(self, tenant: str, kind: str, id: str, body: dict, db, expected: int | None = None) -> dict: ...
    def event(self, tenant: str, run: str, name: str, body: dict, db) -> None: ...
    def trace(self, tenant: str, run: str) -> list[dict]: ...


class InMemoryState:
    """Simulation only. Share this object across worker instances to test contracts.

    No disk persistence; no database; not a production distributed-state substitute.
    """
    def __init__(self):
        self._records = {}
        self._events = []
        self._lock = RLock()
        self._token = None

    @contextmanager
    def transaction(self):
        with self._lock:
            if self._token is not None:
                raise InventoryError("Nested state mutations are not supported")
            records, events = deepcopy(self._records), deepcopy(self._events)
            token = object()
            self._token = token
            try:
                yield token
            except BaseException:
                self._records, self._events = records, events
                raise
            finally:
                self._token = None

    @contextmanager
    def connect(self):
        with self._lock:
            yield None

    def get(self, tenant, kind, id, db=None):
        with self._lock:
            record = self._records.get((tenant, kind, id))
            if record is None:
                raise InventoryError(f"{kind} not found in this tenant")
            return deepcopy(record)

    def list(self, tenant, kind, db=None, *, filters=None, limit=1000):
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise InventoryError("Query limit must be in [1, 1000]")
        with self._lock:
            result = [deepcopy(value) for (t, k, _), value in self._records.items()
                      if t == tenant and k == kind and all(value.get(key) == val for key, val in (filters or {}).items())]
            if len(result) > limit:
                raise InventoryError("Query exceeds bounded planning scope; partition the request")
            return sorted(result, key=lambda r: r["id"])

    def put(self, tenant, kind, id, body, db, expected=None):
        with self._lock:
            if db is None or db is not self._token:
                raise InventoryError("Writes require an active unit of work")
            if not tenant or not kind or not id:
                raise InventoryError("Tenant, kind and record identity required")
            prior = self._records.get((tenant, kind, id))
            if prior is None:
                if expected is not None:
                    raise Conflict("Record no longer exists")
                version = 1
            else:
                if expected is None or prior["version"] != expected:
                    raise Conflict("Record changed; reload and retry")
                version = expected + 1
            clean = {k: deepcopy(v) for k, v in body.items() if k not in {"id", "version"}}
            result = {"id": id, "version": version, **clean}
            self._records[(tenant, kind, id)] = result
            return deepcopy(result)

    def event(self, tenant, run, name, body, db):
        with self._lock:
            if db is None or db is not self._token:
                raise InventoryError("Trace writes require an active unit of work")
            self._events.append({"seq": len(self._events)+1, "tenant": tenant, "run": run,
                                 "name": name, "body": deepcopy(body), "at": now()})

    def trace(self, tenant, run):
        with self._lock:
            return [{k: deepcopy(v) for k,v in e.items() if k not in {"tenant", "run"}}
                    for e in self._events if e["tenant"] == tenant and e["run"] == run]
