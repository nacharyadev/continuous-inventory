from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import BoundedAgent,FixtureProvider,JsonGatewayProvider
from .common import InventoryError
from .evaluation import run_suite,run_trial,seed,verify_trial
from .forecast import ALGORITHMS,backtest,predict
from .runtime import Runtime,local_actor


def emit(value,output=None):
    text=json.dumps(value,indent=2)
    if output:
        path=Path(output);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text+"\n")
    print(text)


def main(argv=None):
    parser=argparse.ArgumentParser(description="Continuous inventory simulation, local persistence, and evaluation foundation")
    sub=parser.add_subparsers(dest="command",required=True)
    demo=sub.add_parser("demo");demo.add_argument("--output");demo.add_argument("--workspace")
    ev=sub.add_parser("evaluate");ev.add_argument("--cases",default="examples/evaluation_cases.json");ev.add_argument("--output")
    f=sub.add_parser("forecast");f.add_argument("--history",required=True,help="Comma-separated equally spaced quantities")
    f.add_argument("--method",choices=ALGORITHMS,default="moving_average");f.add_argument("--horizon",type=int,default=3)
    f.add_argument("--parameters",default="{}");f.add_argument("--compare",action="store_true")
    imp=sub.add_parser("preview-demand");imp.add_argument("file");imp.add_argument("--kind",choices=["forecast","confirmed_order","pipeline"],default="forecast")
    imp.add_argument("--item-map",required=True);imp.add_argument("--location-map",required=True)
    event=sub.add_parser("preview-event");event.add_argument("title")
    server=sub.add_parser("serve-demo");server.add_argument("--host",default="127.0.0.1");server.add_argument("--port",type=int,default=8765);server.add_argument("--workspace",default="output/runtime/local")
    routing=sub.add_parser("seed-routing-demo");routing.add_argument("--workspace",default="output/runtime/routing-mcp")
    sub.add_parser("rrsi-run");sub.add_parser("rrsi-verify")
    args=parser.parse_args(argv)
    try:
        if args.command=="evaluate":
            report=run_suite(args.cases);emit(report,args.output);return 0 if report["passed"]==report["total"] else 1
        if args.command=="rrsi-run":
            request=json.load(sys.stdin)
            task={k:v for k,v in request["task"].items() if k in {"id","capability","input"}}
            emit(run_trial(task,request["workspace"]));return 0
        if args.command=="rrsi-verify":
            request=json.load(sys.stdin);emit(verify_trial(request["task"],request["agent_output"]));return 0
        if args.command=="forecast":
            history=args.history.split(",")
            if args.compare:
                candidates=[{"algorithm":"naive"},{"algorithm":"moving_average","parameters":{"window":3}},{"algorithm":"exponential_smoothing","parameters":{"alpha":"0.3"}}]
                emit(backtest(history,candidates,min_train=3))
            else:emit(predict(history,args.horizon,args.method,json.loads(args.parameters)))
            return 0
        r=Runtime.local(args.workspace) if getattr(args,"workspace",None) else Runtime.simulated();a=local_actor()
        if args.command=="demo":
            if r.domain.store.list(a.tenant,"bill"):
                raise InventoryError("Use a new demo workspace; existing data is preserved")
            seed(r,a);agent=BoundedAgent(r.plans,FixtureProvider())
            lc=agent.prepare_landed_cost(a,"freight-1",[{"bill_id":"inventory-1","amount":"120.00"}])
            mo=agent.prepare_shortage(a,sources=["warehouse"],destination="factory",required="25",manufacturing_order="MO-1",day="2026-09-27")
            outcomes=[]
            for plan in (lc,mo):
                r.plans.approve(a,plan["id"],plan["version"])
                r.plans.execute(a,plan["id"],lose_ack=True)
                worker=Runtime(control_state=r.store,domain=r.domain)
                result=worker.plans.execute(a,plan["id"])
                before=r.plans.verify(a,plan["id"])
                if plan["kind"]=="manufacturing":
                    r.domain.confirm_receipt(a,result["result"]["transaction_id"],"20","demo-receipt")
                outcomes.append({"plan":r.plans.verify(a,plan["id"]),"before_physical_receipt":before["status"],"trace":r.store.trace(a.tenant,plan["id"])})
            emit({"mode":"SIMULATION ONLY — local persistence when --workspace is supplied","provider":agent.provider.name,"outcomes":outcomes},args.output)
        elif args.command=="seed-routing-demo":
            if r.domain.store.list(a.tenant,"sales_order") or r.domain.store.list(a.tenant,"location"):
                raise InventoryError("Use a new routing demo workspace; existing data is preserved")
            r.domain.seed_location(a,"EAST",name="East DC",address={"line1":"1 East St","city":"Boston","region":"MA","postal_code":"02108","country":"US"},priority=20,handling_cost="4.00")
            r.domain.seed_location(a,"WEST",name="West DC",address={"line1":"1 West St","city":"Reno","region":"NV","postal_code":"89501","country":"US"},priority=10,handling_cost="2.00")
            r.domain.seed_item_location_inventory(a,"SKU-1@EAST",item="SKU-1",location="EAST",qoh="12",available="5")
            r.domain.seed_item_location_inventory(a,"SKU-1@WEST",item="SKU-1",location="WEST",qoh="10",available="4")
            order=r.domain.seed_sales_order(a,"SO-1001",state="OPEN",lines=[{"id":"line-1","item":"SKU-1","quantity":"8","uom":"each"}],destination={"line1":"99 Buyer Ave","city":"Austin","region":"TX","postal_code":"78701","country":"US"},allow_split=True,max_splits=2)
            emit({"mode":"SIMULATION ONLY","workspace":str(args.workspace),"order_id":order["id"],"state":order["state"],"mcp_command":"CONTINUOUS_INVENTORY_WORKSPACE="+str(args.workspace)+" continuous-inventory-mcp"})
        elif args.command=="preview-demand":
            r.demand.register_source(a,"upload","Preview upload")
            emit(r.demand.stage_csv(a,"upload",Path(args.file).read_text(),source_version="preview",kind=args.kind,
                item_map=json.loads(args.item_map),location_map=json.loads(args.location_map)))
        elif args.command=="preview-event":emit(r.demand.declare_event(a,title=args.title))
        elif args.command=="serve-demo":
            import uvicorn
            from .api import create_app
            if not r.domain.store.list(a.tenant,"bill"):seed(r,a)
            gateway_url=os.environ.get("CI_MODEL_GATEWAY_URL")
            gateway_token=os.environ.get("CI_MODEL_GATEWAY_TOKEN")
            if bool(gateway_url) != bool(gateway_token):
                raise InventoryError("Set both CI_MODEL_GATEWAY_URL and CI_MODEL_GATEWAY_TOKEN, or neither")
            provider=JsonGatewayProvider(gateway_url,gateway_token) if gateway_url else FixtureProvider()
            app=create_app(r,provider,local_event_api=True)
            print(f"Local simulation API at http://{args.host}:{args.port}; no auth; SQLite and simulated domain only; provider={provider.name}. Stop with Ctrl-C.",flush=True)
            uvicorn.run(app,host=args.host,port=args.port)
        return 0
    except (InventoryError,ValueError,OSError) as exc:
        print(json.dumps({"error":str(exc)}),file=sys.stderr);return 2

if __name__=="__main__":raise SystemExit(main())
