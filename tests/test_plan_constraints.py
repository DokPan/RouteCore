import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.core.plan_validation import validate_plan
from backend.app.core.solver import official_shift_planner
from backend.app.models.official_spec import DayInput, ReferenceData, TravelMatrix


def read_example(model, name):
    return model.model_validate(json.loads((PROJECT_ROOT / "api" / "examples" / name).read_text(encoding="utf-8")))


class PlanConstraintsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = read_example(ReferenceData, "ReferenceDataExample.json")
        cls.day = read_example(DayInput, "DayInputExample.json")
        cls.matrix = read_example(TravelMatrix, "TravelMatrixExample.json")
        official_shift_planner.dist_engine.set_matrix(cls.matrix)
        cls.plan = official_shift_planner.plan_day(
            cls.day.date, cls.day.now, cls.reference.staff, cls.day.orders,
            {center.service_center_id: center for center in cls.reference.service_centers},
            {work.code: work for work in cls.reference.work_types},
        )

    def test_detects_each_violation(self):
        for rule in range(1, 11):
            with self.subTest(rule=rule):
                plan = self.plan.model_copy(deep=True)
                reference = self.reference.model_copy(deep=True)
                day = self.day.model_copy(deep=True)
                route = next(route for route in plan.routes if route.totals.visits_count)
                visit = next(stop for stop in route.stops if stop.type == "VISIT")
                person = next(person for person in reference.staff if person.staff_id == route.staff_id)
                order = next(order for order in day.orders if order.order_id == visit.order_id)
                previous_plan = None

                if rule == 1:
                    person.technologies = []
                elif rule == 2:
                    person.shift.end = "09:00"
                elif rule == 3:
                    route.stops = [stop for stop in route.stops if stop.type != "BREAK"]
                elif rule == 4:
                    order.latest_start = day.now
                elif rule == 5:
                    visit.end = (datetime.fromisoformat(visit.end) + timedelta(minutes=1)).isoformat()
                elif rule == 6:
                    visit.arrival = visit.end
                elif rule == 7:
                    order.required_equipment = {"ONT": 1000}
                elif rule == 8:
                    route.stops[0].location_id = "OTHER"
                elif rule == 9:
                    plan.cancelled.append(visit.order_id)
                elif rule == 10:
                    previous_plan = self.plan
                    plan.as_of = (datetime.fromisoformat(visit.start) + timedelta(minutes=1)).isoformat()
                    visit.arrival = plan.as_of

                report = validate_plan(plan, day, reference, self.matrix, previous_plan)
                self.assertIn(f"H{rule}", {issue["rule"] for issue in report["violations"]})

    def test_installation_cutoff_applies_when_supplied_window_is_longer(self):
        day = self.day.model_copy(deep=True)
        plan = self.plan.model_copy(deep=True)
        order = next(order for order in day.orders if order.work_type_code.startswith("INST"))
        order.latest_start = order.slot.end
        visit = next(stop for route in plan.routes for stop in route.stops if stop.order_id == order.order_id)
        late_start = datetime.fromisoformat(order.slot.start) + timedelta(minutes=11)
        visit.start = late_start.isoformat()
        report = validate_plan(plan, day, self.reference, self.matrix)
        self.assertTrue(any(issue["rule"] == "H4" and issue["order_id"] == order.order_id for issue in report["violations"]))


if __name__ == "__main__":
    unittest.main()
