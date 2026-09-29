import unittest

from continuous_inventory.common import Conflict
from continuous_inventory.mcp_server import create_mcp_server
from continuous_inventory.runtime import Runtime, local_actor


class FulfillmentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.runtime=Runtime.simulated()
        self.actor=local_actor()
        self.runtime.domain.seed_location(self.actor,"EAST",name="East DC",
            address={"line1":"1 East St","city":"Boston","region":"MA","postal_code":"02108","country":"US"},
            priority=20,handling_cost="4.00")
        self.runtime.domain.seed_location(self.actor,"WEST",name="West DC",
            address={"line1":"1 West St","city":"Reno","region":"NV","postal_code":"89501","country":"US"},
            priority=10,handling_cost="2.00")

    def seed_balance(self, id, item, location, qoh, available):
        return self.runtime.domain.seed_item_location_inventory(self.actor,id,item=item,location=location,
            qoh=qoh,available=available,uom="each")

    def seed_order(self, id="SO-1", *, state="OPEN", quantity="6", allow_split=True, max_splits=None):
        return self.runtime.domain.seed_sales_order(self.actor,id,state=state,
            lines=[{"id":"line-1","item":"SKU-1","quantity":quantity,"uom":"each"}],
            destination={"line1":"99 Buyer Ave","city":"Austin","region":"TX","postal_code":"78701","country":"US"},
            allow_split=allow_split,max_splits=max_splits)

    def test_open_is_the_only_mvp_eligible_state(self):
        self.seed_order(state="DRAFT")
        explained=self.runtime.routing.explain_order(self.actor,"SO-1")
        self.assertFalse(explained["routing_eligible"])
        self.assertEqual(explained["eligibility_reason"],"ORDER_NOT_OPEN")
        with self.assertRaises(Conflict):
            self.runtime.routing.prepare(self.actor,"SO-1")

    def test_available_not_qoh_drives_route_and_location_address_is_captured(self):
        self.seed_order()
        self.seed_balance("east-stock","SKU-1","EAST","100","2")
        self.seed_balance("west-stock","SKU-1","WEST","6","6")
        plan=self.runtime.routing.prepare(self.actor,"SO-1")
        self.assertEqual(plan["status"],"prepared")
        self.assertEqual([group["location_id"] for group in plan["groups"]],["WEST"])
        self.assertEqual(plan["groups"][0]["origin_address"]["city"],"Reno")
        allocation=plan["groups"][0]["allocations"][0]
        self.assertEqual(allocation["available_at_plan"],"6")
        self.assertEqual(allocation["qoh_at_plan"],"6")

    def test_prefers_lower_handling_cost_when_both_single_node_routes_are_feasible(self):
        self.seed_order()
        self.seed_balance("east-stock","SKU-1","EAST","10","10")
        self.seed_balance("west-stock","SKU-1","WEST","10","10")
        plan=self.runtime.routing.prepare(self.actor,"SO-1")
        self.assertEqual(plan["groups"][0]["location_id"],"WEST")
        self.assertEqual(plan["estimated_cost"],"2.00")
        self.assertEqual(plan["alternatives"][0]["location_ids"],["EAST"])

    def test_split_route_uses_available_across_locations(self):
        self.seed_order(quantity="8",max_splits=2)
        self.seed_balance("east-stock","SKU-1","EAST","20","5")
        self.seed_balance("west-stock","SKU-1","WEST","20","3")
        plan=self.runtime.routing.prepare(self.actor,"SO-1")
        self.assertEqual({group["location_id"] for group in plan["groups"]},{"EAST","WEST"})
        self.assertEqual(sum(float(a["quantity"]) for g in plan["groups"] for a in g["allocations"]),8)

    def test_no_split_returns_no_feasible_route(self):
        self.seed_order(quantity="8",allow_split=False)
        self.seed_balance("east-stock","SKU-1","EAST","20","5")
        self.seed_balance("west-stock","SKU-1","WEST","20","3")
        plan=self.runtime.routing.prepare(self.actor,"SO-1")
        self.assertEqual(plan["status"],"no_feasible_route")
        self.assertEqual(plan["groups"],[])

    def test_approval_and_idempotent_execution_reduce_available_not_qoh(self):
        order=self.seed_order()
        balance=self.seed_balance("west-stock","SKU-1","WEST","10","10")
        plan=self.runtime.routing.prepare(self.actor,order["id"],request_id="route-so-1")
        with self.assertRaises(Conflict):
            self.runtime.routing.execute(self.actor,plan["id"])
        approved=self.runtime.routing.approve(self.actor,plan["id"],plan["version"])
        self.assertEqual(approved["status"],"approved")
        executed=self.runtime.routing.execute(self.actor,plan["id"])
        repeated=self.runtime.routing.execute(self.actor,plan["id"])
        self.assertEqual(executed["result"],repeated["result"])
        updated=self.runtime.domain.get(self.actor,"item_location_inventory",balance["id"])
        self.assertEqual(updated["qoh"],"10")
        self.assertEqual(updated["available"],"4")
        self.assertEqual(len(executed["result"]["assignment_ids"]),1)
        self.assertEqual(len(executed["result"]["fulfillment_order_ids"]),1)
        self.assertEqual(len(self.runtime.domain.store.list(self.actor.tenant,"operation")),1)

    def test_inventory_change_after_approval_makes_plan_stale(self):
        self.seed_order()
        balance=self.seed_balance("west-stock","SKU-1","WEST","10","10")
        plan=self.runtime.routing.prepare(self.actor,"SO-1")
        self.runtime.routing.approve(self.actor,plan["id"],plan["version"])
        with self.runtime.domain.store.transaction() as db:
            current=self.runtime.domain.store.get(self.actor.tenant,"item_location_inventory",balance["id"],db)
            current["available"]="2"
            self.runtime.domain.store.put(self.actor.tenant,"item_location_inventory",balance["id"],current,db,current["version"])
        with self.assertRaises(Conflict):
            self.runtime.routing.execute(self.actor,plan["id"])
        self.assertEqual(self.runtime.routing.get(self.actor,plan["id"])["status"],"stale")


class FulfillmentRoutingMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_exposed_and_return_structured_content(self):
        from mcp import Client

        runtime=Runtime.simulated();actor=local_actor()
        runtime.domain.seed_location(actor,"MAIN",name="Main",address={"city":"Austin","country":"US"})
        runtime.domain.seed_item_location_inventory(actor,"stock",item="SKU",location="MAIN",qoh="9",available="7")
        runtime.domain.seed_sales_order(actor,"SO",state="OPEN",
            lines=[{"id":"line","item":"SKU","quantity":"5","uom":"each"}],
            destination={"city":"Dallas","country":"US"})
        server=create_mcp_server(runtime,actor)
        async with Client(server,raise_exceptions=True) as client:
            tools=await client.list_tools()
            names={tool.name for tool in tools.tools}
            self.assertEqual(names,{
                "inventory.get_routing_capabilities","inventory.explain_order_fulfillment_state",
                "inventory.prepare_fulfillment_route","inventory.get_route_plan",
                "inventory.approve_route_plan","inventory.execute_route_plan",
            })
            result=await client.call_tool("inventory.prepare_fulfillment_route",{"order_id":"SO"})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["status"],"prepared")
            self.assertEqual(result.structured_content["groups"][0]["origin_address"]["city"],"Austin")


if __name__=="__main__":
    unittest.main()
