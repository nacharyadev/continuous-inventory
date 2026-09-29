import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal

from continuous_inventory.common import Conflict,InventoryError
from continuous_inventory.evaluation import verify_trial,trajectory
from continuous_inventory.runtime import Runtime,local_actor


class BoundaryTests(unittest.TestCase):
    def test_seasonal_forecast_override_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Runtime.simulated();a=local_actor()
            base=r.forecasts.configure(a,algorithm="naive",item="A")
            r.forecasts.configure(a,algorithm="moving_average",parameters={"window":2},item="A",starts="2026-09-01",ends="2026-11-30")
            self.assertEqual(r.forecasts.run(a,item="A",day="2026-10-01",history=[2,4],horizon=1)["point_forecast"],["3"])
            self.assertEqual(r.forecasts.run(a,item="A",day="2026-12-01",history=[2,4],horizon=1)["point_forecast"],["4"])
            r.forecasts.replace_selection(a,base["id"],base["version"],algorithm="exponential_smoothing",parameters={"alpha":"0.5"})
            with self.assertRaises(Conflict):r.forecasts.replace_selection(a,base["id"],base["version"],algorithm="naive")

    def test_wrong_forecast_source_kind_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Runtime.simulated();a=local_actor();r.demand.register_source(a,"crm","CRM")
            b=r.demand.stage_csv(a,"crm","external_id,item,location,quantity,uom,date\n1,A,L,10,each,2026-10-01",source_version="1",kind="pipeline",item_map={"A":"A"},location_map={"L":"L"})
            r.demand.activate(a,b["id"])
            with self.assertRaises(InventoryError):r.demand.outlook(a,"A","L","2026-10-01",forecast_source="crm")

    def test_conflicting_staged_same_source_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Runtime.simulated();a=local_actor();r.demand.register_source(a,"ext","external")
            def stage(q):return r.demand.stage_csv(a,"ext",f"external_id,item,location,quantity,uom,date\n1,A,L,{q},each,2026-10-01",source_version="1",kind="forecast",item_map={"A":"A"},location_map={"L":"L"})
            first=stage(10);second=stage(20);r.demand.activate(a,first["id"])
            with self.assertRaises(Conflict):r.demand.activate(a,second["id"])

    def test_missing_execution_trace_fails_without_crashing(self):
        trace=[{"name":n,"body":{"plan_hash":"x"}} for n in ["prepared","approved","command_confirmed","verified"]]
        self.assertFalse(trajectory(trace,"landed_cost")["execution_started"])

    def test_mutated_state_fails_outcome_verifier(self):
        task={"expected":{"status":"verifying","observed":{"destination_quantity":"25"}}}
        output={"answer":{"status":"verifying","observed":{"destination_quantity":"15"}},"trace":[],"capability":"manufacturing"}
        self.assertEqual(verify_trial(task,output)["reward"],0)

    def test_rrsi_json_protocol_matches_oncall(self):
        root=Path(__file__).resolve().parents[1]
        task=json.loads((root/"examples/evaluation_cases.json").read_text())[0]
        env={**os.environ,"PYTHONPATH":str(root/"src")}
        with tempfile.TemporaryDirectory() as tmp:
            req={"task":{k:v for k,v in task.items() if k!="expected"},"workspace":tmp,"seed":7,"harness_dir":str(root/"examples")}
            run=subprocess.run([sys.executable,"-m","continuous_inventory","rrsi-run"],input=json.dumps(req),text=True,capture_output=True,env=env,check=True)
            output=json.loads(run.stdout)
            request={"task":task,"agent_output":output,"workspace":tmp}
            verify=subprocess.run([sys.executable,"-m","continuous_inventory","rrsi-verify"],input=json.dumps(request),text=True,capture_output=True,env=env,check=True)
            self.assertEqual(json.loads(verify.stdout)["reward"],1)

if __name__=="__main__":unittest.main()

class AdditionalSafetyTests(unittest.TestCase):
    def setUp(self):
        from continuous_inventory.evaluation import seed
        self.r=Runtime.simulated();self.a=local_actor();seed(self.r,self.a)

    def test_duplicate_prepare_has_one_native_draft(self):
        args=dict(source="freight-1",allocations=[{"bill_id":"inventory-1","amount":"120"}],request_id="retry-me")
        first=self.r.plans.prepare_landed_cost(self.a,**args)
        second=self.r.plans.prepare_landed_cost(self.a,**args)
        self.assertEqual(first["id"],second["id"])
        self.assertEqual(len(self.r.domain.store.list(self.a.tenant,"draft")),1)
        args["allocations"]=[{"bill_id":"inventory-1","amount":"100"}]
        with self.assertRaises(Conflict):self.r.plans.prepare_landed_cost(self.a,**args)

    def test_closed_period_prevents_posting(self):
        p=self.r.plans.prepare_landed_cost(self.a,source="freight-1",allocations=[{"bill_id":"inventory-1","amount":"120"}])
        self.r.plans.approve(self.a,p["id"],p["version"])
        with self.r.domain.store.transaction() as tx:
            b=self.r.domain.store.get(self.a.tenant,"bill","freight-1",tx)
            b["period_open"]=False
            self.r.domain.store.put(self.a.tenant,"bill",b["id"],b,tx,b["version"])
        with self.assertRaises(InventoryError):self.r.plans.execute(self.a,p["id"])
        self.assertEqual(self.r.domain.get(self.a,"bill","freight-1")["allocated"],"0")

    def test_stock_race_rejects_second_plan(self):
        args=dict(source="warehouse",destination="factory",required="65",manufacturing_order="MO-1",day="2026-09-27")
        p1=self.r.plans.prepare_shortage(self.a,**args);p2=self.r.plans.prepare_shortage(self.a,**args)
        for p in [p1,p2]:self.r.plans.approve(self.a,p["id"],p["version"])
        self.r.plans.execute(self.a,p1["id"])
        with self.assertRaises(Conflict):self.r.plans.execute(self.a,p2["id"])
        self.assertEqual(self.r.domain.get(self.a,"stock","warehouse")["quantity"],"40")
