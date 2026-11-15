from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from .dispatch import CONFLICT, NO_VISIT, find_route_for_order, get_active_dispatch_context
from .orchestrator import congestion_profiles, db, handle_day_event, ui_reset_hooks, visit_end_overrides
from .dispatch import get_validation_matrix, save_reversible_change
from ..core.plan_validation import validate_plan
from ..core.reactive import replan_event
from ..core.solver import MANUAL_RESTOCK_EXPLANATION


router = APIRouter(prefix="/api/v1/ui")
CALL_WINDOW_OPEN_MINUTES = 60
CALL_WINDOW_CLOSE_MINUTES = 30
SPECIALIST_DEPARTMENT = "PROFILE_DEPARTMENT"
DEMO_PIN = "0426"
INVALID_DEMO_LOGIN = "Проверьте ID мастера и демо-PIN."


class StaffLogin(BaseModel):
    staff_id: str
    pin: str


@router.get("/staff-login/defaults")
def get_demo_login_defaults():
    _, _, reference = get_active_dispatch_context()
    return {"staff_id": reference.staff[0].staff_id if reference.staff else "", "pin": DEMO_PIN}


@router.get("/staff/{staff_id}/equipment")
def get_starting_equipment(staff_id: str):
    _, _, reference = get_active_dispatch_context()
    staff = next((person for person in reference.staff if person.staff_id == staff_id), None)
    if staff is None:
        raise HTTPException(404, INVALID_DEMO_LOGIN)
    return staff.equipment_start


@router.post("/staff-login")
def login_demo_staff(credentials: StaffLogin):
    plan, _, reference = get_active_dispatch_context()
    staff = next((person for person in reference.staff if person.staff_id == credentials.staff_id), None)
    if staff is None or credentials.pin != DEMO_PIN:
        raise HTTPException(401, INVALID_DEMO_LOGIN)
    center = next((item for item in reference.service_centers if item.service_center_id == staff.service_center_id), None)
    route = next((item for item in plan.routes if item.staff_id == staff.staff_id), None)
    backpack = staff.equipment_start
    if route:
        now = datetime.fromisoformat(plan.as_of)
        backpack = next((stop.equipment_after for stop in reversed(route.stops)
                         if stop.equipment_after is not None and (stop.status in {"IN_PROGRESS", "DONE"}
                         or (stop.type == "RESTOCK" and datetime.fromisoformat(stop.end) <= now))), backpack)
    return {"staff_id": staff.staff_id, "name": staff.display_name, "center": center.name if center else staff.service_center_id,
            "shift": staff.shift, "profiles": staff.profiles, "technologies": staff.technologies,
            "height_permit": staff.height_permit, "equipment": backpack}


def call_window(order, now):
    start = datetime.fromisoformat(order.slot.start)
    opens_at = start - timedelta(minutes=CALL_WINDOW_OPEN_MINUTES)
    closes_at = start - timedelta(minutes=CALL_WINDOW_CLOSE_MINUTES)
    return {"opens_at": opens_at.isoformat(), "closes_at": closes_at.isoformat(),
            "available": opens_at <= now <= closes_at, "closed": now > closes_at}


@router.get("/visits")
def list_staff_visits(staff_id: str):
    plan, day, _ = get_active_dispatch_context()
    orders = {order.order_id: order for order in day.orders}
    return [
        {"id": stop.order_id, "address": orders[stop.order_id].address, "start": stop.start,
         "actual_start": stop.actual_start, "actual_end": stop.actual_end,
         "status": stop.status, "why": stop.explanation, "lat": orders[stop.order_id].lat,
         "lon": orders[stop.order_id].lon, "called": (plan.date, stop.order_id) in recorded_calls,
         "demo_called": (plan.date, stop.order_id) in recorded_demo_calls,
         "slot": orders[stop.order_id].slot,
         "client_name": orders[stop.order_id].client_name,
         "client_phone": orders[stop.order_id].client_phone,
         "entrance": orders[stop.order_id].entrance,
         "floor": orders[stop.order_id].floor,
         "call_window": call_window(orders[stop.order_id], datetime.fromisoformat(plan.as_of))}
        for route in plan.routes if route.staff_id == staff_id
        for stop in route.stops if stop.type == "VISIT" and stop.status != "CANCELLED" and stop.order_id in orders
    ]


recorded_calls = set()
recorded_demo_calls = set()
CALL_WINDOW_ERROR = "Отметка доступна за 30–60 минут до начала слота."


def _clear_mobile_calls():
    recorded_calls.clear()
    recorded_demo_calls.clear()


ui_reset_hooks.append(_clear_mobile_calls)


@router.post("/visits/{order_id}/call")
def record_subscriber_call(order_id: str, demo: bool = False):
    plan, day, _ = get_active_dispatch_context()
    order = next((order for order in day.orders if order.order_id == order_id), None)
    if order is None or find_route_for_order(plan, order_id) is None:
        raise HTTPException(404, NO_VISIT)
    if not demo and not call_window(order, datetime.fromisoformat(plan.as_of))["available"]:
        raise HTTPException(409, CALL_WINDOW_ERROR)
    call_records = recorded_demo_calls if demo else recorded_calls
    call_records.add((plan.date, order_id))
    return {"called": not demo, "demo_called": demo}


@router.post("/visits/{order_id}/transfer")
def transfer_visit_to_specialist(order_id: str):
    plan, day, _ = get_active_dispatch_context()
    route = find_route_for_order(plan, order_id)
    if route is None:
        raise HTTPException(404, NO_VISIT)
    visit = next(stop for stop in route.stops if stop.type == "VISIT" and stop.order_id == order_id and stop.status != "CANCELLED")
    if visit.status != "IN_PROGRESS":
        raise HTTPException(409, CONFLICT)
    finished_at = max(datetime.fromisoformat(plan.as_of), datetime.fromisoformat(visit.start) + timedelta(minutes=1))
    result = handle_day_event(day.date, {
        "event_id": uuid4().hex, "type": "ORDER_TRANSFERRED", "timestamp": finished_at.isoformat(),
        "staff_id": route.staff_id, "order_id": order_id, "actual_end": finished_at.isoformat(),
        "recorded_end": datetime.now(timezone.utc).isoformat(),
        "transferred_to": SPECIALIST_DEPARTMENT,
    })
    return {"status": "TRANSFERRED", "department": SPECIALIST_DEPARTMENT, "processing_ms": result.processing_ms}


def change_visit_status(order_id, expected, status):
    plan, _, _ = get_active_dispatch_context()
    route = find_route_for_order(plan, order_id)
    if not route:
        raise HTTPException(404, NO_VISIT)
    updated = plan.model_copy(deep=True)
    stop = next(stop for stop in find_route_for_order(updated, order_id).stops if stop.type == "VISIT" and stop.order_id == order_id and stop.status != "CANCELLED")
    if stop.status == status:
        return stop
    if stop.status != expected:
        raise HTTPException(409, CONFLICT)
    stop.status = status
    stop.actual_start = datetime.now(timezone.utc).isoformat()
    updated.plan_version += 1
    db["plans"][plan.date] = updated
    return stop


@router.post("/visits/{order_id}/arrive")
def arrive(order_id: str):
    return change_visit_status(order_id, "PLANNED", "IN_PROGRESS")


@router.post("/visits/{order_id}/no-answer")
def mark_subscriber_unavailable(order_id: str, demo: bool = False):
    plan, day, _ = get_active_dispatch_context()
    route = find_route_for_order(plan, order_id)
    order = next((order for order in day.orders if order.order_id == order_id), None)
    if route is None or order is None:
        raise HTTPException(404, NO_VISIT)
    visit = next((stop for stop in route.stops if stop.order_id == order_id and stop.type == "VISIT" and stop.status != "CANCELLED"), None)
    if visit is None:
        raise HTTPException(404, NO_VISIT)
    now = datetime.fromisoformat(plan.as_of)
    if visit.status == "DONE" or (not demo and (visit.status != "PLANNED" or now >= datetime.fromisoformat(visit.start))):
        raise HTTPException(409, CONFLICT)
    if not demo and not call_window(order, now)["available"]:
        raise HTTPException(409, CALL_WINDOW_ERROR)
    return handle_day_event(day.date, {
        "type": "ORDER_DEFERRED", "event_id": uuid4().hex,
        "timestamp": plan.as_of, "order_id": order_id,
    })


@router.post("/visits/{order_id}/finish")
def finish(order_id: str):
    plan, day, _ = get_active_dispatch_context()
    route = find_route_for_order(plan, order_id)
    if route is None:
        raise HTTPException(404, NO_VISIT)
    visit = next(stop for stop in route.stops if stop.type == "VISIT" and stop.order_id == order_id and stop.status != "CANCELLED")
    if visit.status == "DONE":
        return visit
    if visit.status != "IN_PROGRESS":
        raise HTTPException(409, CONFLICT)
    finished_at = max(datetime.fromisoformat(plan.as_of), datetime.fromisoformat(visit.start) + timedelta(minutes=1))
    return handle_day_event(day.date, {
        "type": "VISIT_COMPLETED", "event_id": uuid4().hex, "timestamp": finished_at.isoformat(),
        "staff_id": route.staff_id, "order_id": order_id, "actual_end": finished_at.isoformat(),
        "recorded_end": datetime.now(timezone.utc).isoformat(),
    })

RESTOCK_NOT_AVAILABLE = "Заезд в СЦ не помещается в график. Проверьте время смены, обеда и работы склада."
RESTOCK_ALREADY_PLANNED = "Заезд в СЦ уже запланирован."
RESTOCK_NOT_NEXT = "Сейчас следующий шаг маршрута не является заездом в СЦ."


@router.post("/staff/{staff_id}/restock")
def request_staff_restock(staff_id: str, credentials: StaffLogin):
    if credentials.staff_id != staff_id or credentials.pin != DEMO_PIN:
        raise HTTPException(401, INVALID_DEMO_LOGIN)
    return schedule_staff_restock(staff_id)


@router.post("/routes/{staff_id}/restock")
def dispatcher_restock(staff_id: str):
    return schedule_staff_restock(staff_id)


@router.post("/routes/{staff_id}/restock/complete")
def dispatcher_complete_restock(staff_id: str):
    plan, _, _ = get_active_dispatch_context()
    updated = plan.model_copy(deep=True)
    route = next((route for route in updated.routes if route.staff_id == staff_id), None)
    pending = next((
        stop for stop in route.stops if stop.type in {"RESTOCK", "VISIT"}
        and stop.status not in {"DONE", "CANCELLED"}
        and (stop.type != "RESTOCK" or stop.end > plan.as_of)
    ), None) if route else None
    if pending is None or pending.type != "RESTOCK":
        raise HTTPException(409, RESTOCK_NOT_NEXT)
    pending.status = "DONE"
    updated.plan_version += 1
    db["plans"][plan.date] = updated
    return {"equipment": pending.equipment_after, "plan_version": updated.plan_version}


def schedule_staff_restock(staff_id: str):
    plan, day, reference = get_active_dispatch_context()
    route = next((item for item in plan.routes if item.staff_id == staff_id), None)
    if route is None or route.status == "UNAVAILABLE" or route.unavailable_until and route.unavailable_until > plan.as_of:
        raise HTTPException(409, RESTOCK_NOT_AVAILABLE)
    if any(stop.type == "RESTOCK" and stop.status not in {"DONE", "CANCELLED"} and stop.end > plan.as_of for stop in route.stops):
        raise HTTPException(409, RESTOCK_ALREADY_PLANNED)
    event = {"type": "MANUAL_RESTOCK", "event_id": uuid4().hex, "timestamp": plan.as_of}
    updated, updated_day, result = replan_event(
        plan, day, reference, event, congestion_factors=congestion_profiles.get(plan.date),
        manual_restock_staff_id=staff_id,
    )
    stop = next((
        stop for route in updated.routes if route.staff_id == staff_id for stop in route.stops
        if stop.type == "RESTOCK" and stop.explanation == MANUAL_RESTOCK_EXPLANATION and stop.end > plan.as_of
    ), None)
    if not stop or validate_plan(updated, updated_day, reference, matrix=get_validation_matrix(),
                                 visit_end_overrides=visit_end_overrides.get(plan.date))["violations"]:
        raise HTTPException(409, RESTOCK_NOT_AVAILABLE)
    undo_res = save_reversible_change(plan, (updated, updated_day, event))
    return {"start": stop.start, "end": stop.end, "processing_ms": result.processing_ms, "undo_token": undo_res["undo_token"]}
