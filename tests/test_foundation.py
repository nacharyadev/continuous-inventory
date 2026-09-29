import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from continuous_inventory.agent import BoundedAgent,FixtureProvider
from continuous_inventory.common import Actor,Conflict,Forbidden,InventoryError,quantity
from continuous_inventory.evaluation import run_suite,seed,trajectory
from continuous_inventory.forecast import backtest,predict
from continuous_inventory.runtime import Runtime,local_actor


class RuntimeBase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.r=Runtime.simulated();self.a=local_actor();seed(self.r,self.a)

    def lc(self):return self.r.plans.prepare_landed_cost(self.a,source="freight-1",allocations=[{"bill_id":"inventory-1","amount":"120.00"}])
    def mo(self):return self.r.plans.prepare_shortage(self.a,source="warehouse",destination="factory",required="25",manufacturing_order="MO-1",day="2026-09-27")
    def approve(self,p):return self.r.plans.approve(self.a,p["id"],p["version"])


class RuntimeCase(RuntimeBase):
    def test_draft_does_not_post(self):
        p=self.lc();self.assertEqual(self.r.domain.get(self.a,"bill","freight-1")["allocated"],"0")
        self.assertEqual(self.r.domain.get(self.a,"draft",p["payload"]["draft_id"])["status"],"draft")

    def test_unapproved_plan_cannot_execute(self):
        p=self.lc()
        with self.assertRaises(Conflict):self.r.plans.execute(self.a,p["id"])
        self.assertEqual(self.r.domain.get(self.a,"bill","freight-1")["allocated"],"0")

    def test_stale_plan_version_cannot_approve(self):
        p=self.lc()
        with self.assertRaises(Conflict):self.r.plans.approve(self.a,p["id"],99)

    def test_draft_edit_invalidates_approval(self):
        p=self.lc();self.approve(p)
        self.r.domain.edit_draft(self.a,p["payload"]["draft_id"],[{"bill_id":"inventory-1","amount":"100"}])
        with self.assertRaises(Conflict):self.r.plans.execute(self.a,p["id"])
        self.assertEqual(self.r.domain.get(self.a,"bill","freight-1")["allocated"],"0")

    def test_new_worker_recovers_post_without_duplicate(self):
        p=self.lc();self.approve(p)
        self.assertEqual(self.r.plans.execute(self.a,p["id"],lose_ack=True)["status"],"unknown")
        restarted=Runtime(control_state=self.r.store,domain=self.r.domain);restarted.plans.execute(self.a,p["id"])
        self.assertEqual(restarted.plans.verify(self.a,p["id"])["status"],"completed")
        self.assertEqual(Decimal(restarted.domain.get(self.a,"bill","inventory-1")["added_cost"]),Decimal(120))

    def test_concurrent_charge_allocations_only_one_posts(self):
        p1=self.lc();p2=self.lc();self.approve(p1);self.approve(p2)
        def execute(p):
            try:self.r.plans.execute(self.a,p["id"]);return True
            except Conflict:return False
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(execute,[p1,p2]))
        self.assertEqual(sum(results),1)
        self.assertEqual(Decimal(self.r.domain.get(self.a,"bill","freight-1")["allocated"]),Decimal(120))

    def test_duplicate_workers_share_operation(self):
        p=self.lc();self.approve(p)
        with ThreadPoolExecutor(2) as pool:list(pool.map(lambda _:self.r.plans.execute(self.a,p["id"]),range(2)))
        self.assertEqual(len(self.r.domain.store.list(self.a.tenant,"operation")),1)

    def test_partial_receipt_not_completed(self):
        p=self.mo();self.approve(p);result=self.r.plans.execute(self.a,p["id"])
        self.assertEqual(self.r.plans.verify(self.a,p["id"])["status"],"verifying")
        self.r.domain.confirm_receipt(self.a,result["result"]["transaction_id"],"10","r1")
        self.assertEqual(self.r.plans.verify(self.a,p["id"])["status"],"verifying")
        self.r.domain.confirm_receipt(self.a,result["result"]["transaction_id"],"10","r2")
        self.assertEqual(self.r.plans.verify(self.a,p["id"])["status"],"completed")

    def test_duplicate_receipt_is_idempotent(self):
        p=self.mo();self.approve(p);result=self.r.plans.execute(self.a,p["id"])
        for _ in range(2):self.r.domain.confirm_receipt(self.a,result["result"]["transaction_id"],"20","same")
        self.assertEqual(Decimal(self.r.domain.get(self.a,"stock","factory")["quantity"]),25)

    def test_protected_stock_prevents_recovery(self):
        with self.assertRaises(Conflict):self.r.plans.prepare_shortage(self.a,source="warehouse",destination="factory",required="90",manufacturing_order="MO-2",day="2026-09-27")

    def test_rule_revocation_invalidates_prepared_plan(self):
        p=self.mo();self.approve(p)
        rule=self.r.store.list(self.a.tenant,"rule")[0];self.r.rules.revoke(self.a,rule["id"])
        with self.assertRaises(Conflict):self.r.plans.execute(self.a,p["id"])
        self.assertEqual(self.r.domain.get(self.a,"stock","warehouse")["quantity"],"100")

    def test_tenant_isolation(self):
        p=self.lc();other=local_actor("other")
        with self.assertRaises(InventoryError):self.r.plans.get(other,p["id"])
        with self.assertRaises(InventoryError):self.r.domain.get(other,"bill","freight-1")

    def test_permission_required(self):
        p=self.lc();reader=Actor(self.a.tenant,"reader",frozenset({"plans.read"}))
        with self.assertRaises(Forbidden):self.r.plans.approve(reader,p["id"],p["version"])

    def test_cancelled_plan_cannot_execute(self):
        p=self.lc();self.approve(p);self.r.plans.cancel(self.a,p["id"])
        with self.assertRaises(Conflict):self.r.plans.execute(self.a,p["id"])

    def test_one_time_rule_does_not_leak(self):
        self.r.rules.teach(self.a,scope="once",case_id="special",mode="stock_based")
        self.assertTrue(self.r.rules.resolve(self.a,"new","2026-09-27","unrelated")["needs_teaching"])
        self.assertEqual(self.r.rules.resolve(self.a,"new","2026-09-27","special")["mode"],"stock_based")

    def test_season_scope(self):
        self.r.rules.teach(self.a,scope="item",item="seasonal",mode="stock_based",starts="2026-09-01",ends="2026-11-30")
        self.assertFalse(self.r.rules.resolve(self.a,"seasonal","2026-10-01")["needs_teaching"])
        self.assertTrue(self.r.rules.resolve(self.a,"seasonal","2026-12-01")["needs_teaching"])

    def test_conflicting_rules_escalate(self):
        self.r.rules.teach(self.a,scope="business",mode="stock_based")
        with self.assertRaises(Conflict):self.r.rules.resolve(self.a,"component-A","2026-09-27")

    def test_agent_rejects_fabricated_evidence(self):
        class Bad:
            name="bad"
            def choose(self,ctx):return {"option_id":"reviewed_targets","reason":"x","evidence_ids":["invented"]}
        agent=BoundedAgent(self.r.plans,Bad())
        with self.assertRaises(InventoryError):agent.prepare_landed_cost(self.a,"freight-1",[{"bill_id":"inventory-1","amount":"120"}])
        self.assertEqual(self.r.domain.store.list(self.a.tenant,"draft"),[])

    def test_agent_cannot_choose_unknown_action(self):
        class Bad:
            name="bad"
            def choose(self,ctx):return {"option_id":"close_period","reason":"x","evidence_ids":["freight-1","inventory-1"]}
        with self.assertRaises(InventoryError):BoundedAgent(self.r.plans,Bad()).prepare_landed_cost(self.a,"freight-1",[{"bill_id":"inventory-1","amount":"120"}])


class DemandCase(RuntimeBase):
    def stage(self,source="ext",kind="confirmed_order",amount="10",version="1",external="order-1"):
        if not any(s["id"]==source for s in self.r.store.list(self.a.tenant,"source")):self.r.demand.register_source(self.a,source,source)
        csv=f"external_id,item,location,quantity,uom,date\n{external},SKU-A,MAIN,{amount},each,2026-10-01\n"
        return self.r.demand.stage_csv(self.a,source,csv,source_version=version,kind=kind,item_map={"SKU-A":"A"},location_map={"MAIN":"L1"})

    def test_external_internal_demand_not_double_counted(self):
        b=self.stage();self.r.demand.activate(self.a,b["id"])
        self.r.demand.record_internal_order(self.a,"SO-1",item="A",location="L1",day="2026-10-01",amount="4",external_source="ext",external_id="order-1")
        result=self.r.demand.outlook(self.a,"A","L1","2026-10-01")
        self.assertEqual(Decimal(result["confirmed"]),10);self.assertEqual(Decimal(result["external_remaining"]),6)

    def test_forecast_consumption_and_pipeline(self):
        for src,kind,amt in [("ext","confirmed_order","10"),("forecast","forecast","20"),("crm","pipeline","100")]:
            b=self.stage(src,kind,amt);self.r.demand.activate(self.a,b["id"])
        result=self.r.demand.outlook(self.a,"A","L1","2026-10-01",forecast_source="forecast")
        self.assertEqual(Decimal(result["planning_demand"]),20)
        self.assertEqual(Decimal(result["unconsumed_forecast"]),10)
        self.assertEqual(Decimal(result["tentative_pipeline"]),100)

    def test_reupload_deduplicated_and_snapshot_replaces(self):
        a=self.stage();b=self.stage();self.assertEqual(a["id"],b["id"])
        self.r.demand.activate(self.a,a["id"])
        b=self.stage(amount="12",version="2");self.r.demand.activate(self.a,b["id"])
        self.assertEqual(Decimal(self.r.demand.outlook(self.a,"A","L1","2026-10-01")["confirmed"]),12)

    def test_invalid_rows_block_activation(self):
        b=self.stage(amount="=20+10")
        self.assertEqual(b["issues"][0]["row"],2)
        with self.assertRaises(InventoryError):self.r.demand.activate(self.a,b["id"])

    def test_missing_source_visible(self):
        self.r.demand.register_source(self.a,"crm","CRM")
        self.assertEqual(self.r.demand.outlook(self.a,"A","L1","2026-10-01")["issues"][0]["reason"],"missing")

    def test_annual_sale_does_not_invent_numbers(self):
        event=self.r.demand.declare_event(self.a,title="Annual sale")
        self.assertIn("projected_quantity",event["missing"]);self.assertFalse(event["activated"])


class ForecastCase(unittest.TestCase):
    def test_baselines(self):
        self.assertEqual(predict([2,4,6],2,"moving_average",{"window":3})["point_forecast"],["4","4"])
        self.assertEqual(predict([2,4,6,8],3,"seasonal_naive",{"season_length":2})["point_forecast"],["6","8","6"])
        self.assertEqual(predict([10,20],1,"exponential_smoothing",{"alpha":"0.5"})["point_forecast"],["15.0"])

    def test_zero_demand_wape_is_undefined(self):
        result=backtest([0,0,0,0,0],[{"algorithm":"naive"}],min_train=3)
        self.assertIsNone(result["candidates"][0]["wape"])

    def test_no_future_leakage(self):
        result=backtest([1,2,3,100],[{"algorithm":"naive"}],min_train=3)
        self.assertEqual(result["candidates"][0]["folds"][0]["forecast"],["3"])
        self.assertFalse(result["activated"])

    def test_insufficient_history_not_silently_fallback(self):
        with self.assertRaises(InventoryError):predict([1],2,"seasonal_naive",{"season_length":12})

    def test_non_finite_quantity_rejected(self):
        for value in ["NaN","Infinity","-1"]:
            with self.assertRaises(InventoryError):quantity(value)

    def test_forecast_configuration_is_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Runtime.simulated();a=local_actor()
            r.forecasts.configure(a,algorithm="naive")
            r.forecasts.configure(a,algorithm="moving_average",item="A",parameters={"window":2})
            self.assertEqual(r.forecasts.run(a,item="A",day="2026-09-27",history=[1,3],horizon=1)["point_forecast"],["2"])
            self.assertEqual(r.forecasts.run(a,item="B",day="2026-09-27",history=[1,3],horizon=1)["point_forecast"],["3"])


class EvaluationCase(unittest.TestCase):
    def test_both_pilot_suites(self):
        path=Path(__file__).resolve().parents[1]/"examples/evaluation_cases.json"
        report=run_suite(path)
        self.assertEqual(report["passed"],6)

    def test_verifier_detects_missing_approval(self):
        trace=[{"name":"prepared","body":{}},{"name":"command_confirmed","body":{}},{"name":"verified","body":{}}]
        self.assertFalse(trajectory(trace,"landed_cost")["approved"])

if __name__=="__main__":unittest.main()
