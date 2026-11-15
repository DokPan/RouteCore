from datetime import datetime

from fastapi import APIRouter

from .dispatch import get_active_dispatch_context
from .orchestrator import congestion_profiles
from ..core.plan_insights import describe_plan_changes
from ..core.reactive import replan_event


router = APIRouter(prefix="/api/v1/ui")
COMPARISON_EVENT = "PLAN_COMPARISON"


@router.get("/plan-comparison")
def compare_active_plan():
    plan, day, reference = get_active_dispatch_context()
    event = {"type": COMPARISON_EVENT, "event_id": COMPARISON_EVENT, "timestamp": plan.as_of}
    options = {"congestion_factors": congestion_profiles.get(plan.date)}
    grouped, _, _ = replan_event(plan, day, reference, event, **options)
    ungrouped, _, _ = replan_event(plan, day, reference, event, group_incidents=False, **options)
    model_time = datetime.fromisoformat(plan.as_of)
    affected_route = max(
        plan.routes,
        key=lambda route: sum(stop.type == "VISIT" and stop.status == "PLANNED"
                              and datetime.fromisoformat(stop.start) >= model_time for stop in route.stops),
        default=None,
    )
    comparison_event = (
        {**event, "type": "STAFF_UNAVAILABLE", "staff_id": affected_route.staff_id}
        if affected_route else event
    )
    with_preference, _, _ = replan_event(plan, day, reference, comparison_event, **options)
    without_preference, _, _ = replan_event(plan, day, reference, comparison_event, preserve_assignments=False, **options)
    grouped_ids = {order_id for group in grouped.pgp_groups for order_id in group.order_ids}
    avoided_visits = sum(
        stop.type == "VISIT" and stop.order_id in grouped_ids
        for route in ungrouped.routes for stop in route.stops
    )

    def count_transfers(proposal):
        return sum(
            change["before"] is not None and change["after"] is not None
            and change["before"][0] != change["after"][0]
            for change in describe_plan_changes(plan, proposal)
        )

    return {
        "avoided_visits": avoided_visits,
        "grouped_orders": len(grouped_ids),
        "scenario_staff_id": affected_route.staff_id if affected_route else None,
        "transfers_with_preference": count_transfers(with_preference),
        "transfers_without_preference": count_transfers(without_preference),
        "assigned_with_preference": with_preference.summary.visits_planned,
        "assigned_without_preference": without_preference.summary.visits_planned,
    }
