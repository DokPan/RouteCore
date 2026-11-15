from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .dispatch import CONFLICT, STALE, get_active_dispatch_context, get_validation_matrix, preview_plan_change, save_reversible_change
from .orchestrator import congestion_profiles
from ..core.plan_validation import validate_plan
from ..core.reactive import replan_event


router = APIRouter(prefix="/api/v1/ui")
scenarios = {}


class Backpack(BaseModel):
    staff_id: str
    equipment: dict[str, int] = Field(default_factory=dict)


@router.post("/whatif")
def preview_backpack_scenario(payload: Backpack):
    plan, day, reference = get_active_dispatch_context()
    staff = next((staff for staff in reference.staff if staff.staff_id == payload.staff_id), None)
    if not staff or any(qty < 0 or qty > staff.equipment_capacity.get(key, 0) for key, qty in payload.equipment.items()):
        raise HTTPException(409, CONFLICT)
    staff = staff.model_copy(update={"equipment_start": {**staff.equipment_start, **payload.equipment}})
    changed = reference.model_copy(update={"staff": [staff if person.staff_id == staff.staff_id else person for person in reference.staff]})
    if any(route.staff_id == staff.staff_id and any(datetime.fromisoformat(stop.start) < datetime.fromisoformat(plan.as_of) for stop in route.stops) for route in plan.routes):
        raise HTTPException(409, CONFLICT)
    prepared = preview_plan_change(plan, day, changed)
    return register_scenario(plan, day, reference, changed, prepared)


class StaffingScenario(BaseModel):
    staff_ids: list[str] = Field(min_length=1, max_length=3)


@router.post("/whatif/staffing")
def preview_staffing_scenario(payload: StaffingScenario):
    plan, day, reference = get_active_dispatch_context()
    active_ids = {route.staff_id for route in plan.routes if route.status == "ACTIVE"}
    staff_ids = set(payload.staff_ids)
    if len(staff_ids) != len(payload.staff_ids) or not staff_ids <= active_ids:
        raise HTTPException(409, CONFLICT)
    updated, updated_day = plan, day
    for staff_id in payload.staff_ids:
        event = {"type": "STAFF_UNAVAILABLE", "staff_id": staff_id,
                 "event_id": uuid4().hex, "timestamp": plan.as_of}
        updated, updated_day, _ = replan_event(
            updated, updated_day, reference, event,
            congestion_factors=congestion_profiles.get(plan.date),
        )
    return register_scenario(plan, day, reference, reference, (updated, updated_day, event))


def register_scenario(plan, day, reference, changed, prepared):
    scenario_id = uuid4().hex
    scenarios[scenario_id] = plan, prepared, changed, reference, day
    if len(scenarios) > 32:
        scenarios.pop(next(iter(scenarios)))

    matrix = get_validation_matrix()
    before_report = validate_plan(plan, day, reference, matrix)
    after_report = validate_plan(prepared[0], prepared[1], changed, matrix)

    def metrics(value, report):
        return {
            "summary": value.summary,
            "restocks": sum(stop.type == "RESTOCK" and stop.status != "CANCELLED" for route in value.routes for stop in route.stops),
            "on_time": report["on_time"],
            "travel_min": round(report["travel_sec"] / 60),
        }

    before_metrics = metrics(plan, before_report)
    after_metrics = metrics(prepared[0], after_report)
    delta_on_time = after_metrics["on_time"] - before_metrics["on_time"]
    delta_unassigned = prepared[0].summary.unassigned - plan.summary.unassigned
    delta_restocks = after_metrics["restocks"] - before_metrics["restocks"]
    delta_travel_min = round((after_report["travel_sec"] - before_report["travel_sec"]) / 60)
    summary_text = (
        f"В срок: {delta_on_time:+d} (было {before_metrics['on_time']}, станет {after_metrics['on_time']}) · "
        f"Без назначения: {delta_unassigned:+d} (было {plan.summary.unassigned}, станет {prepared[0].summary.unassigned}) · "
        f"Пополнения в СЦ: {delta_restocks:+d} (было {before_metrics['restocks']}, станет {after_metrics['restocks']}) · "
        f"Время в пути: {delta_travel_min:+d} мин."
    )

    return {
        "id": scenario_id,
        "before": before_metrics,
        "after": after_metrics,
        "summary": summary_text,
    }


@router.post("/whatif/{scenario_id}/apply")
def apply_staff_scenario(scenario_id: str):
    plan, day, reference = get_active_dispatch_context()
    scenario = scenarios.get(scenario_id)
    if not scenario or scenario[0] is not plan or scenario[3] is not reference or scenario[4] is not day:
        raise HTTPException(409, STALE)
    response = save_reversible_change(plan, scenario[1], reference=scenario[2])
    scenarios.pop(scenario_id)
    return response
