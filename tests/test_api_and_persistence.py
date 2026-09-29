import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from continuous_inventory.api import create_app
from continuous_inventory.agent import FixtureProvider
from continuous_inventory.common import Conflict, InventoryError
from continuous_inventory.evaluation import seed
from continuous_inventory.runtime import Runtime, local_actor
from continuous_inventory.sqlite_state import SQLiteState
from continuous_inventory.store import InMemoryState


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.r = Runtime.simulated()
        self.a = local_actor()
        seed(self.r, self.a)
        self.client = TestClient(create_app(self.r, FixtureProvider(), local_event_api=True))

    def test_landed_cost_approval_execution_api_and_graph_trace(self):
        response = self.client.post("/v1/plans/landed-cost", json={
            "source": "freight-1", "allocations": [{"bill_id": "inventory-1", "amount": "120.00"}]
        })
        self.assertEqual(response.status_code, 201, response.text)
        plan = response.json()
        base = "/v1/plans/" + plan["id"]
        self.assertEqual(self.client.post(base + "/execute").status_code, 409)
        self.assertEqual(self.client.post(base + "/approve", json={"expected_version": plan["version"]}).status_code, 200)
        self.assertEqual(self.client.post(base + "/execute").json()["status"], "verifying")
        self.assertEqual(self.client.post(base + "/verify").json()["status"], "completed")
        trajectory = next(item for item in self.r.store.trace("demo", plan["id"]) if item["name"] == "agent_trajectory")
        self.assertEqual(trajectory["body"]["nodes"], [
            "validate_request", "gather_context", "build_options", "model_select", "validate_selection", "prepare_plan"
        ])

    def test_no_auth_and_development_tenant_context(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/v1/plans").json(), [])
        other = self.client.get("/v1/plans", headers={"X-Tenant-Id": "other"})
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.json(), [])

    def test_invalid_forecast_method_is_client_error(self):
        response = self.client.post("/v1/forecasts/configure", json={"algorithm": "invented"})
        self.assertEqual(response.status_code, 400)

    def test_event_contract_duplicate_gap_and_conflict(self):
        base = {
            "event_id": "evt-1", "event_type": "inventory.quantity.changed", "schema_version": 1,
            "source": "iom", "occurred_at": "2026-09-27T10:00:00Z",
            "aggregate": {"type": "inventory_item_location", "id": "SKU-A@MAIN", "version": 1},
            "payload": {"on_hand": "10"}
        }
        first = self.client.post("/local/v1/events", json=base)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["ordering"], "contiguous")
        self.assertFalse(first.json()["duplicate"])
        duplicate = self.client.post("/local/v1/events", json=base)
        self.assertTrue(duplicate.json()["duplicate"])

        gap = {**base, "event_id": "evt-3", "aggregate": {**base["aggregate"], "version": 3}}
        held = self.client.post("/local/v1/events", json=gap)
        self.assertEqual(held.json()["ordering"], "gap")
        self.assertEqual(held.json()["status"], "held")

        changed = {**base, "payload": {"on_hand": "11"}}
        self.assertEqual(self.client.post("/local/v1/events", json=changed).status_code, 409)

    def test_local_event_api_can_be_disabled(self):
        client = TestClient(create_app(self.r, FixtureProvider()))
        self.assertEqual(client.post("/local/v1/events", json={}).status_code, 404)


class StateContractTests(unittest.TestCase):
    def test_atomic_rollback_and_versions_both_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            for store in [InMemoryState(), SQLiteState(Path(tmp) / "state.sqlite")]:
                with store.transaction() as tx:
                    store.put("t", "k", "id", {"x": 1}, tx)
                with self.assertRaises(Conflict):
                    with store.transaction() as tx:
                        store.put("t", "k", "id", {"x": 2}, tx, expected=99)
                with self.assertRaises(RuntimeError):
                    with store.transaction() as tx:
                        store.put("t", "k", "id", {"x": 2}, tx, expected=1)
                        store.event("t", "run", "changed", {}, tx)
                        raise RuntimeError("abort")
                self.assertEqual(store.get("t", "k", "id")["x"], 1)
                self.assertEqual(store.trace("t", "run"), [])
                with self.assertRaises(InventoryError):
                    store.get("other", "k", "id")

    def test_process_recreation_recovers_lost_post_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Runtime.local(tmp)
            a = local_actor()
            seed(r, a)
            p = r.plans.prepare_landed_cost(a, source="freight-1", allocations=[{"bill_id": "inventory-1", "amount": "120.00"}])
            r.plans.approve(a, p["id"], p["version"])
            self.assertEqual(r.plans.execute(a, p["id"], lose_ack=True)["status"], "unknown")
            del r
            r = Runtime.local(tmp)
            r.plans.execute(a, p["id"])
            self.assertEqual(r.plans.verify(a, p["id"])["status"], "completed")
            self.assertEqual(len(r.domain.store.list(a.tenant, "operation")), 1)

    def test_sqlite_independent_workers_compete_without_double_allocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = Runtime.local(tmp)
            a = local_actor()
            seed(r, a)
            plans = []
            for _ in range(2):
                p = r.plans.prepare_landed_cost(a, source="freight-1", allocations=[{"bill_id": "inventory-1", "amount": "120"}])
                r.plans.approve(a, p["id"], p["version"])
                plans.append(p)

            def run(plan):
                worker = Runtime.local(tmp)
                try:
                    worker.plans.execute(a, plan["id"])
                    return True
                except Conflict:
                    return False

            with ThreadPoolExecutor(2) as pool:
                self.assertEqual(sum(pool.map(run, plans)), 1)

    def test_runtime_has_no_implicit_persistence(self):
        with self.assertRaises(TypeError):
            Runtime()
        with tempfile.TemporaryDirectory() as tmp:
            Runtime.simulated()
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
