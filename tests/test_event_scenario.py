import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.api.orchestrator import db, load_default_official_data
from backend.app.main import app

EVENT_TYPES = {
    "cancel_visit": "ORDER_CANCELLED",
    "tech_delay": "VISIT_DELAYED",
    "new_order": "NEW_ORDER",
    "tech_unavailable": "STAFF_UNAVAILABLE",
}
EVENT_LIMIT_MS = 10_000


class EventScenarioTest(unittest.TestCase):
    def test_legacy_fixture_on_current_demo(self):
        fixture = PROJECT_ROOT / "data" / "mock" / "events_example.json"
        events = json.loads(fixture.read_text(encoding="utf-8"))
        with TestClient(app) as client:
            plan = client.post("/api/v1/plan").json()
            for source_event in events:
                with self.subTest(event=source_event["id"]):
                    event = self.adapt_event(source_event, plan)
                    response = client.post("/api/v1/events", json=event)
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    self.assertLessEqual(result["processing_ms"], EVENT_LIMIT_MS)
                    updated_plan = client.get("/api/v1/plan").json()
                    self.assertEqual(updated_plan["plan_version"], plan["plan_version"] + 1)
                    validation = client.get("/api/v1/ui/validation").json()
                    self.assertFalse(validation["violations"])
                    print(event["type"], result["processing_ms"], "ms", validation["violations"])
                    plan = updated_plan

    def test_no_answer_stays_in_queue_after_replanning(self):
        self.addCleanup(load_default_official_data)
        with TestClient(app) as client:
            load_default_official_data()
            plan = client.get("/api/v1/plan").json()
            order_id = next(
                stop["order_id"] for route in plan["routes"] for stop in route["stops"]
                if stop["type"] == "VISIT" and stop["start"] > plan["as_of"]
            )
            response = client.post(f"/api/v1/ui/visits/{order_id}/no-answer?demo=true")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.post(f"/api/v1/days/{plan['date']}/plan").status_code, 409)
            response = client.post("/api/v1/plan")
            self.assertEqual(response.status_code, 200, response.text)
            recalculated = response.json()
            self.assertNotIn(order_id, recalculated["cancelled"])
            self.assertIn(order_id, {order["order_id"] for order in recalculated["unassigned"]})
            self.assertFalse(client.get("/api/v1/ui/validation").json()["violations"])

    def test_force_majeure_staff_unavailable_modes(self):
        self.addCleanup(load_default_official_data)
        for idx, mode in enumerate(("auto", "reschedule_tomorrow", "backlog")):
            load_default_official_data()
            with TestClient(app) as client:
                plan = client.get("/api/v1/plan").json()
                staff_id = next(
                    route["staff_id"] for route in plan["routes"]
                    if any(stop["type"] == "VISIT" and stop["status"] == "PLANNED" for stop in route["stops"])
                )
                response = client.post("/api/v1/events", json={
                    "event_id": f"test-fm-{mode}-{idx}",
                    "type": "STAFF_UNAVAILABLE",
                    "timestamp": plan["as_of"],
                    "staff_id": staff_id,
                    "reason": "ACCIDENT",
                    "reassign_mode": mode,
                })
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertTrue(payload["accepted"])
                self.assertIn(staff_id, payload["changed_staff_ids"])

    def adapt_event(self, source_event, plan):
        timestamp = plan["date"] + source_event["timestamp"][10:] + "+05:00"
        event_type = EVENT_TYPES[source_event["event_type"]]
        event = {"event_id": source_event["id"], "type": event_type, "timestamp": timestamp}
        visits = [(route, stop) for route in plan["routes"] for stop in route["stops"] if stop["type"] == "VISIT"]
        if event_type == "ORDER_CANCELLED":
            event["order_id"] = next(stop["order_id"] for _, stop in visits if stop["start"] >= timestamp)
        elif event_type == "VISIT_DELAYED":
            route, stop = next((route, stop) for route, stop in visits if stop["start"] < timestamp < stop["end"])
            expected_end = datetime.fromisoformat(stop["end"]) + timedelta(minutes=source_event["delay_minutes"])
            event.update(staff_id=route["staff_id"], order_id=stop["order_id"], expected_end=expected_end.isoformat())
        elif event_type == "STAFF_UNAVAILABLE":
            route, _ = next((route, stop) for route, stop in visits if stop["start"] >= timestamp)
            event.update(staff_id=route["staff_id"], until=(datetime.fromisoformat(timestamp) + timedelta(minutes=45)).isoformat())
        else:
            source_order = source_event["order"]
            template = next(order for order in db["days"][plan["date"]].orders if order.technology == source_order["technology"])
            order = template.model_dump()
            slot_start, slot_end = source_order["time_slot"].split("-")
            slot = {key: f"{plan['date']}T{value}:00+05:00" for key, value in (("start", slot_start), ("end", slot_end))}
            order.update(order_id=source_order["id"], building_id=source_order["id"], address=source_order["address"],
                         lat=source_order["lat"], lon=source_order["lon"], registered_at=timestamp,
                         slot=slot, latest_start=slot["end"], access_node_id=None, is_repeat=source_order["is_repeat"])
            event["order"] = order
        return event


if __name__ == "__main__":
    unittest.main()
