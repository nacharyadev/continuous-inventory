"""SQLite relational adapter for the current implementation.

SQLite's BEGIN IMMEDIATE serializes writers within one local file. That is useful
for development and single-replica deployments, not a claim of distributed database semantics.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .common import Conflict, InventoryError, encode, now


class SQLiteState:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS records (
                    tenant TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    version INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY (tenant, kind, id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL,
                    run_id TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(tenant,run_id,seq);
                CREATE INDEX IF NOT EXISTS records_item ON records(tenant,kind,json_extract(body,'$.item'));
                CREATE INDEX IF NOT EXISTS records_source ON records(tenant,kind,json_extract(body,'$.source'),json_extract(body,'$.source_version'));
                CREATE INDEX IF NOT EXISTS records_fingerprint ON records(tenant,kind,json_extract(body,'$.fingerprint'));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except sqlite3.OperationalError as exc:
                db.rollback()
                if "locked" in str(exc).lower():
                    raise Conflict("State is busy; retry the same idempotent operation") from exc
                raise
            except BaseException:
                db.rollback()
                raise

    def get(self, tenant, kind, id, db=None):
        if db is None:
            with self.connect() as read:
                return self.get(tenant,kind,id,read)
        row = db.execute("SELECT version,body FROM records WHERE tenant=? AND kind=? AND id=?",(tenant,kind,id)).fetchone()
        if row is None:
            raise InventoryError(f"{kind} not found in this tenant")
        return {"id":id,"version":row["version"],**json.loads(row["body"])}

    def list(self, tenant, kind, db=None, *, filters=None, limit=1000):
        if not isinstance(limit,int) or not 1<=limit<=1000:
            raise InventoryError("Query limit must be in [1, 1000]")
        if db is None:
            with self.connect() as read:
                return self.list(tenant,kind,read,filters=filters,limit=limit)
        clauses=["tenant=?","kind=?"];params=[tenant,kind]
        for key,value in (filters or {}).items():
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*",key):
                raise InventoryError("Invalid filter field")
            if isinstance(value,(dict,list)):
                raise InventoryError("Query filters must be scalar")
            if value is None:
                clauses.append(f"json_extract(body, '$.{key}') IS NULL")
            else:
                clauses.append(f"json_extract(body, '$.{key}')=?");params.append(value)
        # Field names are validated identifiers; all user values remain bound parameters.
        query="SELECT id,version,body FROM records WHERE "+" AND ".join(clauses)+" ORDER BY id LIMIT ?"
        rows=db.execute(query,[*params,limit+1]).fetchall()
        if len(rows)>limit:
            raise InventoryError("Query exceeds bounded planning scope; partition the request")
        return [{"id":r["id"],"version":r["version"],**json.loads(r["body"])} for r in rows]

    def put(self,tenant,kind,id,body,db,expected=None):
        if db is None or not db.in_transaction:
            raise InventoryError("Writes require an active unit of work")
        if not tenant or not kind or not id:
            raise InventoryError("Tenant, kind and record identity required")
        clean={k:v for k,v in body.items() if k not in {"id","version"}}
        if expected is None:
            version=1
            try:
                db.execute("INSERT INTO records VALUES(?,?,?,?,?)",(tenant,kind,id,version,encode(clean)))
            except sqlite3.IntegrityError as exc:
                raise Conflict("Record already exists; use its current version") from exc
        else:
            version=expected+1
            result=db.execute("UPDATE records SET version=?,body=? WHERE tenant=? AND kind=? AND id=? AND version=?",(version,encode(clean),tenant,kind,id,expected))
            if result.rowcount!=1:
                raise Conflict("Record changed; reload and retry")
        return {"id":id,"version":version,**clean}

    def event(self,tenant,run,name,body,db):
        if db is None or not db.in_transaction:
            raise InventoryError("Events require an active unit of work")
        db.execute("INSERT INTO events(tenant,run_id,name,body,recorded_at) VALUES(?,?,?,?,?)",(tenant,run,name,encode(body),now()))

    def trace(self,tenant,run):
        with self.connect() as db:
            return [{"seq":r["seq"],"name":r["name"],"body":json.loads(r["body"]),"at":r["recorded_at"]}
                    for r in db.execute("SELECT * FROM events WHERE tenant=? AND run_id=? ORDER BY seq",(tenant,run))]
