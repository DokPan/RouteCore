from datetime import datetime, timedelta
import re
from uuid import uuid4
from time import monotonic

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .orchestrator import (
    add_custom_order, address_district, congestion_profiles, db, get_current_plan,
    get_validation_matrix, handle_day_event, plan_history, reset_demo,
    ui_reset_hooks, upload_day_json, visit_end_overrides,
)
from .workspace_undo import UNDO_SECONDS, WorkspaceSnapshot, WorkspaceUndo
from ..models.official_spec import DayInput, Order, Plan
from ..core.reactive import replan_event
from ..core.plan_validation import validate_plan
from ..core.plan_insights import describe_plan_changes, locate_incidents
from ..core.roads import OSRM_URL
from ..core.joint_visit_rules import JOINT_VISIT_REVIEW, PARTNER_ORDER_PREFIX, linked_order_ids
from ..core.solver import parse_time_to_minutes
from ..core.plan_improvement import SEARCH_SECONDS
from ..core.visit_slots import latest_allowed_start
from ..core.visit_warnings import RESCHEDULE_NOTICE_MINUTES
from ..constants import RU_TEXT_0001, RU_TEXT_0002


router = APIRouter(prefix="/api/v1/ui")
undo_records = {}
demo_comparisons = {}
workspace_undo = None


def _clear_ui_state():
    global workspace_undo
    undo_records.clear()
    demo_comparisons.clear()
    workspace_undo = None


ui_reset_hooks.append(_clear_ui_state)
CONFLICT = "Перенос невозможен"
INVALID_RESCHEDULE_SLOT = "Выберите время в пределах даты плана. Начало должно быть раньше конца, а до нового визита должно оставаться не меньше 30 минут."
RESCHEDULE_NO_ROUTE = "В новый слот не удалось назначить визит с учётом смены, обеда, дороги и оборудования."
NO_VISIT = "Визит не найден"
STALE = "План изменился. Пересчитайте сценарий."
NOTICE_REQUIRED = "Менять согласованный слот можно не позднее чем за 30 минут до его начала."
DEMO_DELAY_MINUTES = 35
INVALID_CONGESTION_WINDOW = "Конец периода должен быть позже начала."


class VisitAssignmentRequest(BaseModel):
    staff_id: str


class VisitRescheduleRequest(BaseModel):
    start: datetime
    end: datetime
    latest_start: datetime | None = None


def get_active_dispatch_context():
    plan = get_current_plan()
    return plan, db["days"][plan.date], db["reference"]


def get_editable_order(plan, day, order_id):
    order = next((order for order in day.orders if order.order_id == order_id), None)
    if order is None:
        raise HTTPException(404, NO_VISIT)
    if set(linked_order_ids(order_id)) <= {item.order_id for item in day.orders}:
        raise HTTPException(409, JOINT_VISIT_REVIEW)
    if order_id in plan.cancelled or any(
        stop.order_id == order_id and (stop.status in {"IN_PROGRESS", "DONE"}
                                      or datetime.fromisoformat(stop.start) < datetime.fromisoformat(plan.as_of))
        for route in plan.routes for stop in route.stops
    ):
        raise HTTPException(409, CONFLICT)
    return order


def preview_plan_change(plan, day, reference, assignments=None, event_type="UI_CHANGE", *, prioritize_slot_order=False):
    event = {"type": event_type, "event_id": uuid4().hex, "timestamp": plan.as_of}
    updated, updated_day, result = replan_event(
        plan, day, reference, event, assignments, congestion_profiles.get(plan.date),
        search_seconds=SEARCH_SECONDS if event_type == "UI_REPLAN" else 0,
        prioritize_slot_order=prioritize_slot_order,
    )
    return updated, updated_day, event


def save_plan_change(plan, prepared):
    updated, day, event = prepared
    db["plans"][plan.date] = updated
    db["days"][plan.date] = day
    db["pgp_groups"][plan.date] = updated.pgp_groups
    plan_history[plan.date] = plan, event
    if event.get("type") in {"VISIT_DELAYED", "ORDER_TRANSFERRED", "VISIT_COMPLETED"}:
        staff_id = event["staff_id"]
        order_id = event["order_id"]
        expected_end = event.get("expected_end") or event.get("actual_end")
        if any(stop.order_id == order_id and stop.end == expected_end
               for route in updated.routes if route.staff_id == staff_id for stop in route.stops):
            visit_end_overrides.setdefault(plan.date, {})[staff_id, order_id] = expected_end
    return updated


def save_reversible_change(plan, prepared, congestion_profile=None, reference=None):
    if db["plans"].get(plan.date) is not plan or db["active_date"] != plan.date:
        raise HTTPException(409, STALE)
    original_day = db["days"][plan.date]
    original_history = plan_history.get(plan.date)
    original_congestion = congestion_profiles.get(plan.date)
    original_overrides = dict(visit_end_overrides.get(plan.date, {}))
    original_reference = db["reference"]
    updated = save_plan_change(plan, prepared)
    if congestion_profile is not None:
        congestion_profiles[plan.date] = congestion_profile
    if reference is not None:
        db["reference"] = reference
    token = uuid4().hex
    undo_records[plan.date] = (
        token, monotonic() + UNDO_SECONDS, updated, plan, original_day, original_history,
        original_congestion, original_overrides, original_reference, db["reference"],
    )
    return {"plan": updated, "undo_token": token, "undo_seconds": UNDO_SECONDS}


@router.post("/undo/{token}")
def undo_dispatch_change(token: str):
    global workspace_undo
    if workspace_undo and token == workspace_undo.token:
        record = workspace_undo
        if monotonic() >= record.expires or WorkspaceSnapshot.capture(demo_comparisons) != record.after:
            raise HTTPException(409, STALE)
        workspace_undo = None
        current = get_current_plan()
        record.before.restore(demo_comparisons)
        previous = get_current_plan()
        restored = previous.model_copy(update={"plan_version": max(current.plan_version, previous.plan_version) + 1})
        db["plans"][restored.date] = restored
        if restored.date in demo_comparisons:
            compared_plan, changes = demo_comparisons[restored.date]
            if compared_plan is previous:
                demo_comparisons[restored.date] = restored, changes
        return restored
    return undo_plan_change(token)


def undo_plan_change(token: str):
    current, _, _ = get_active_dispatch_context()
    record = undo_records.get(current.date)
    if not record:
        raise HTTPException(409, STALE)
    expected, expires, updated, previous, day, history, congestion_profile, original_overrides, previous_reference, current_reference = record
    if expected != token or monotonic() >= expires or current is not updated or db["reference"] is not current_reference:
        raise HTTPException(409, STALE)
    undo_records.pop(current.date)
    restored = previous.model_copy(update={"plan_version": current.plan_version + 1})
    db["plans"][current.date] = restored
    db["days"][current.date] = day
    db["reference"] = previous_reference
    db["pgp_groups"][current.date] = restored.pgp_groups
    if congestion_profile is None:
        congestion_profiles.pop(current.date, None)
    else:
        congestion_profiles[current.date] = congestion_profile
    if original_overrides:
        visit_end_overrides[current.date] = original_overrides
    else:
        visit_end_overrides.pop(current.date, None)
    if history is None:
        plan_history.pop(current.date, None)
    else:
        plan_history[current.date] = history
    return restored


@router.post("/replan")
def replan_dispatch_day():
    plan, day, reference = get_active_dispatch_context()
    return save_reversible_change(plan, preview_plan_change(plan, day, reference, event_type="UI_REPLAN"))


def find_route_for_order(plan, order_id):
    return next((route for route in plan.routes if any(
        stop.type == "VISIT" and stop.order_id == order_id and stop.status != "CANCELLED"
        for stop in route.stops
    )), None)


class CongestionWindow(BaseModel):
    from_hour: int = Field(ge=0, le=23)
    to_hour: int = Field(ge=1, le=24)
    factor: float = Field(ge=1, le=5)


@router.get("/congestion")
def get_congestion_profile():
    date = db["active_date"]
    matrix = db["travel_matrix"]
    base = {item.hour: item.factor for item in matrix.hourly_factors or []} if matrix else {}
    manual = congestion_profiles.get(date, {})
    return [{"hour": hour, "factor": manual.get(hour, base.get(hour, 1.0)),
             "source": "manual" if hour in manual else "matrix" if hour in base else "default"}
            for hour in range(24)]


@router.delete("/congestion")
def reset_congestion_profile():
    return save_congestion_profile({})


@router.post("/congestion")
def apply_congestion_window(payload: CongestionWindow):
    if payload.from_hour >= payload.to_hour:
        raise HTTPException(422, INVALID_CONGESTION_WINDOW)
    profile = dict(congestion_profiles.get(db["active_date"], {}))
    profile.update({hour: payload.factor for hour in range(payload.from_hour, payload.to_hour)})
    return save_congestion_profile(profile)


def save_congestion_profile(profile):
    plan, day, reference = get_active_dispatch_context()
    event = {"type": "CONGESTION_CHANGED", "event_id": uuid4().hex, "timestamp": plan.as_of}
    updated, updated_day, _ = replan_event(plan, day, reference, event, congestion_factors=profile)
    result = save_reversible_change(plan, (updated, updated_day, event), congestion_profile=profile)
    result["changed_visits"] = len(describe_plan_changes(plan, updated))
    return result


@router.get("/validation")
def get_plan_validation():
    plan, day, reference = get_active_dispatch_context()
    previous, event = plan_history.get(plan.date, (None, None))
    matrix = get_validation_matrix()
    order_centers = {order.order_id: address_district(order) for order in day.orders}
    report = validate_plan(
        plan, day, reference, matrix, previous, event,
        visit_end_overrides.get(plan.date),
        order_service_centers=order_centers,
    )
    report["plan_version"] = plan.plan_version
    report["initial_kpis"] = db["initial_kpis"].get(plan.date)
    report["changes"] = describe_plan_changes(previous, plan)
    demo_plan, demo_changes = demo_comparisons.get(plan.date, (None, []))
    if demo_plan is plan:
        report["changes"] = demo_changes
    report["incidents"] = locate_incidents(plan, reference, day.orders)
    report["grouped_orders"] = sum(len(group.order_ids) for group in plan.pgp_groups)
    report["planned_orders"] = sum(not order.order_id.startswith(PARTNER_ORDER_PREFIX) for order in day.orders)
    return report


@router.get("/order-cards")
def list_order_cards():
    plan, day, reference = get_active_dispatch_context()
    people = {person.staff_id: person.display_name for person in reference.staff}
    visits = {
        stop.order_id: (route.staff_id, stop) for route in plan.routes for stop in route.stops
        if stop.type == "VISIT" and stop.status != "CANCELLED"
    }
    unassigned = {order.order_id: order for order in plan.unassigned}
    grouped = {order_id for group in plan.pgp_groups for order_id in group.order_ids}
    risky = {alert.order_id for alert in plan.alerts if alert.type == "LATE_RISK" and not alert.applied}
    cards = []
    for order in day.orders:
        staff_id, visit = visits.get(order.order_id, (None, None))
        rejected = unassigned.get(order.order_id)
        status = visit.status if visit else "UNASSIGNED"
        if order.order_id in grouped:
            status = "PGP"
        elif order.order_id in plan.cancelled:
            status = "CANCELLED"
        cards.append({
            "id": order.order_id, "address": order.address, "status": status,
            "work_type": order.work_type_code, "risky": order.order_id in risky,
            "requires_height": order.requires_height,
            "staff_id": staff_id, "staff_name": people.get(staff_id),
            "slot": order.slot,
            "why": visit.explanation if visit else rejected.reason_text if rejected else None,
        })
    return {"version": plan.plan_version, "orders": cards}


def run_workspace_change(operation, *args):
    global workspace_undo
    get_current_plan()
    before = WorkspaceSnapshot.capture(demo_comparisons)
    try:
        result = operation(*args)
        plan = get_current_plan()
    except Exception:
        before.restore(demo_comparisons)
        raise
    workspace_undo = WorkspaceUndo.create(before, WorkspaceSnapshot.capture(demo_comparisons))
    return {**result, "plan": plan, "undo_token": workspace_undo.token, "undo_seconds": UNDO_SECONDS}


INVALID_PHONE = "Некорректный формат телефона. Укажите 10 цифр или номер в формате +7XXXXXXXXXX."


def prepare_custom_order(order: Order) -> Order:
    supplied_latest = datetime.fromisoformat(order.latest_start)
    cutoff = latest_allowed_start(order.work_type_code, datetime.fromisoformat(order.slot.start), supplied_latest)
    if cutoff < supplied_latest:
        order = order.model_copy(update={"latest_start": cutoff.isoformat()})
    phone = order.client_phone
    if phone is not None and phone.strip() != "":
        compact_phone = re.sub(r"[ ()-]", "", phone.strip())
        if not re.fullmatch(r"(?:(?:\+7|7|8)?[0-9]{10})", compact_phone):
            raise HTTPException(422, INVALID_PHONE)
        order = order.model_copy(update={"client_phone": "+7" + compact_phone[-10:]})
    elif phone is not None:
        order = order.model_copy(update={"client_phone": None})
    return order


@router.post("/custom-order")
def add_dispatch_order(order: Order):
    order = prepare_custom_order(order)
    return run_workspace_change(add_custom_order, order)


@router.post("/upload-day")
def import_dispatch_day(day: DayInput):
    return run_workspace_change(upload_day_json, day)


@router.post("/reset-demo")
def reset_dispatch_demo():
    return run_workspace_change(reset_demo)


@router.post("/demo")
def run_demo_scenario():
    return run_workspace_change(play_demo_events)


def play_demo_events():
    plan, day, _ = get_active_dispatch_context()
    start = datetime.fromisoformat(plan.as_of)
    route = next((route for route in plan.routes if any(
        stop.type == "VISIT" and datetime.fromisoformat(stop.start) >= start for stop in route.stops
    )), None)
    if route is None:
        raise HTTPException(409, NO_VISIT)
    visit = next(stop for stop in route.stops if stop.type == "VISIT" and datetime.fromisoformat(stop.start) >= start)
    visit_start = datetime.fromisoformat(visit.start)
    delay_time = visit_start + timedelta(seconds=1)
    delayed_end = datetime.fromisoformat(visit.end) + timedelta(minutes=DEMO_DELAY_MINUTES)
    unavailable_route = max((
        candidate for candidate in plan.routes
        if candidate.staff_id != route.staff_id and candidate.status == "ACTIVE"
        and any(stop.type == "VISIT" for stop in candidate.stops)
        and all(datetime.fromisoformat(stop.start) >= delay_time
                for stop in candidate.stops if stop.type == "VISIT")
    ), key=lambda candidate: candidate.stops[0].start, default=None)
    cancellation = next((
        stop for other_route in plan.routes if other_route.staff_id != route.staff_id
        for stop in other_route.stops
        if stop.type == "VISIT" and datetime.fromisoformat(stop.start) > delay_time
    ), None)
    if cancellation is None or unavailable_route is None:
        raise HTTPException(409, NO_VISIT)
    replacement_order = next(order for order in day.orders if order.order_id == cancellation.order_id)
    token = uuid4().hex[:10]
    events = [
        {"type": "TIME_TICK", "timestamp": visit_start.isoformat()},
        {"type": "VISIT_DELAYED", "staff_id": route.staff_id, "order_id": visit.order_id,
         "expected_end": delayed_end.isoformat()},
        {"type": "ORDER_CANCELLED", "order_id": cancellation.order_id},
        {"type": "NEW_ORDER", "order": replacement_order.model_copy(
            update={"order_id": "DEMO-" + token, "registered_at": delay_time.isoformat()}
        ).model_dump()},
        {"type": "STAFF_UNAVAILABLE", "staff_id": unavailable_route.staff_id},
    ]
    results = []
    for index, event in enumerate(events):
        results.append(handle_day_event(plan.date, {
            "timestamp": delay_time.isoformat(), **event, "event_id": f"demo-{token}-{index}",
        }))
    result = get_current_plan()
    changes = describe_plan_changes(plan, result)
    if result is not plan:
        demo_comparisons[result.date] = (result, changes)
    return {"plan": result, "events": results, "event_types": [event["type"] for event in events],
            "changed_orders": [change["order_id"] for change in changes]}


@router.get("/orders/{order_id}/candidates")
def list_reassignment_candidates(order_id: str):
    plan, day, reference = get_active_dispatch_context()
    order = get_editable_order(plan, day, order_id)
    before = {route.staff_id: route for route in plan.routes}
    centers = {center.service_center_id: center.name for center in reference.service_centers}
    orders_map = {item.order_id: item for item in day.orders}
    order_sc_id = address_district(order)
    if order_sc_id is None and reference.service_centers:
        order_sc_id = min(
            reference.service_centers,
            key=lambda center: (order.lat - center.lat) ** 2 + (order.lon - center.lon) ** 2,
        ).service_center_id
    req_eq = order.required_equipment or {}
    results = []
    for staff in reference.staff:
        if staff.staff_id in (day.absent_staff_ids or []):
            continue
        proposal, _, _ = preview_plan_change(plan, day, reference, {order_id: staff.staff_id})
        route = find_route_for_order(proposal, order_id)
        if route and route.staff_id == staff.staff_id:
            previous = before.get(staff.staff_id)
            curr_stock = dict(staff.equipment_start)
            prev_restocks = 0
            if previous:
                for stop in previous.stops:
                    if stop.status == "CANCELLED":
                        continue
                    if stop.type == "RESTOCK":
                        curr_stock = dict(staff.equipment_capacity)
                        prev_restocks += 1
                    elif stop.type == "VISIT" and stop.order_id != order_id:
                        visit_order = orders_map.get(stop.order_id)
                        if visit_order and visit_order.required_equipment:
                            for eq_k, needed_qty in visit_order.required_equipment.items():
                                curr_stock[eq_k] = curr_stock.get(eq_k, 0) - needed_qty
            stock_sufficient = all(curr_stock.get(eq_k, 0) >= needed_qty for eq_k, needed_qty in req_eq.items())
            new_restocks = sum(1 for stop in route.stops if stop.type == "RESTOCK" and stop.status != "CANCELLED")
            needs_restock = (not stock_sufficient) or (new_restocks > prev_restocks)
            backpack_ok = not needs_restock
            district_match = staff.service_center_id == order_sc_id
            visits_count = previous.totals.visits_count if previous else 0
            shift_str = f"{staff.shift.start}–{staff.shift.end}"
            results.append({
                "staff_id": staff.staff_id, "name": staff.display_name,
                "extra_travel_sec": route.totals.travel_sec - (previous.totals.travel_sec if previous else 0),
                "work_sec_before": previous.totals.work_sec if previous else 0,
                "work_sec_after": route.totals.work_sec,
                "shift_sec": (parse_time_to_minutes(staff.shift.end) - parse_time_to_minutes(staff.shift.start)) * 60,
                "service_center_id": staff.service_center_id,
                "service_center_name": centers.get(staff.service_center_id, staff.service_center_id),
                "district_match": district_match,
                "district_label": f"Свой район ({staff.service_center_id})" if district_match else f"Другой район ({staff.service_center_id})",
                "shift": shift_str,
                "visits_count": visits_count,
                "workload_label": f"{visits_count} виз. · смена {shift_str}",
                "backpack_ok": backpack_ok,
                "needs_restock": needs_restock,
                "equipment_label": RU_TEXT_0001 if needs_restock else RU_TEXT_0002,
            })
    return sorted(results, key=lambda item: item["extra_travel_sec"])


@router.post("/orders/{order_id}/reassign")
def reassign_visit(order_id: str, payload: VisitAssignmentRequest):
    plan, day, reference = get_active_dispatch_context()
    get_editable_order(plan, day, order_id)
    day = day.model_copy(update={"deferred_order_ids": [item for item in day.deferred_order_ids if item != order_id]})
    prepared = preview_plan_change(plan, day, reference, {order_id: payload.staff_id})
    route = find_route_for_order(prepared[0], order_id)
    if not route or route.staff_id != payload.staff_id:
        raise HTTPException(409, CONFLICT)
    return save_reversible_change(plan, prepared)


@router.post("/orders/{order_id}/reschedule")
def reschedule_visit(order_id: str, payload: VisitRescheduleRequest):
    plan, day, reference = get_active_dispatch_context()
    order = get_editable_order(plan, day, order_id)
    latest = payload.latest_start or payload.end
    earliest_notice = datetime.fromisoformat(plan.as_of) + timedelta(minutes=RESCHEDULE_NOTICE_MINUTES)
    if datetime.fromisoformat(order.slot.start) < earliest_notice:
        raise HTTPException(409, NOTICE_REQUIRED)
    if (not all(value.tzinfo for value in (payload.start, latest, payload.end))
            or not payload.start <= latest <= payload.end or payload.start >= payload.end or str(payload.start.date()) != plan.date
            or str(payload.end.date()) != plan.date or payload.start < earliest_notice):
        raise HTTPException(409, INVALID_RESCHEDULE_SLOT)
    latest = latest_allowed_start(order.work_type_code, payload.start, latest)
    updated = order.model_copy(update={
        "slot": order.slot.model_copy(update={"start": payload.start.isoformat(), "end": payload.end.isoformat()}),
        "latest_start": latest.isoformat(),
    })
    day = day.model_copy(update={
        "orders": [updated if item.order_id == order_id else item for item in day.orders],
        "deferred_order_ids": [item for item in day.deferred_order_ids if item != order_id],
    })
    prepared = preview_plan_change(plan, day, reference)
    if not find_route_for_order(prepared[0], order_id):
        rejection = next((item.reason_text for item in prepared[0].unassigned if item.order_id == order_id), None)
        raise HTTPException(409, rejection or RESCHEDULE_NO_ROUTE)
    return save_reversible_change(plan, prepared)


@router.post("/orders/{order_id}/delay")
def delay_visit(order_id: str):
    plan, day, reference = get_active_dispatch_context()
    if not any(order.order_id == order_id for order in day.orders):
        raise HTTPException(404, NO_VISIT)
    if set(linked_order_ids(order_id)) <= {item.order_id for item in day.orders}:
        raise HTTPException(409, JOINT_VISIT_REVIEW)
    route = find_route_for_order(plan, order_id)
    visit = next(
        (stop for stop in (route.stops if route else [])
         if stop.type == "VISIT" and stop.order_id == order_id and stop.status != "CANCELLED"),
        None,
    )
    if not route or not visit or order_id in plan.cancelled or visit.status == "DONE":
        raise HTTPException(409, CONFLICT)
    visit_start = datetime.fromisoformat(visit.start)
    delay_time = max(datetime.fromisoformat(plan.as_of), visit_start + timedelta(seconds=1))
    delayed_end = datetime.fromisoformat(visit.end) + timedelta(minutes=30)
    event = {
        "event_id": uuid4().hex,
        "type": "VISIT_DELAYED",
        "timestamp": delay_time.isoformat(),
        "staff_id": route.staff_id,
        "order_id": order_id,
        "expected_end": delayed_end.isoformat(),
    }
    updated, updated_day, _ = replan_event(
        plan, day, reference, event, congestion_factors=congestion_profiles.get(plan.date),
    )
    return save_reversible_change(plan, (updated, updated_day, event))


@router.post("/orders/{order_id}/cancel")
def cancel_visit(order_id: str):
    plan, day, reference = get_active_dispatch_context()
    get_editable_order(plan, day, order_id)
    event = {
        "event_id": uuid4().hex,
        "type": "ORDER_CANCELLED",
        "timestamp": plan.as_of,
        "order_id": order_id,
    }
    updated, updated_day, _ = replan_event(
        plan, day, reference, event, congestion_factors=congestion_profiles.get(plan.date),
    )
    return save_reversible_change(plan, (updated, updated_day, event))

BULK_NO_SLOTS = "Нет предложенных слотов для массового переноса."
BULK_NO_CAPACITY = "В предложенные слоты не удалось добавить визиты без нарушения существующего плана."


class BulkRescheduleRequest(BaseModel):
    order_id: str | None = None
    order_ids: list[str] | None = None
    plan_version: int


@router.post("/unassigned/reschedule")
def reschedule_unassigned_orders(payload: BulkRescheduleRequest):
    global workspace_undo
    plan, day, reference = get_active_dispatch_context()
    if payload.plan_version != plan.plan_version:
        raise HTTPException(409, STALE)
    orders = {order.order_id: order for order in day.orders}
    selected_ids = set(payload.order_ids) if payload.order_ids is not None else None
    candidates = {}
    for item in plan.unassigned:
        if payload.order_id and item.order_id != payload.order_id:
            continue
        if selected_ids is not None and item.order_id not in selected_ids:
            continue
        if item.suggested_action != "RESCHEDULE_TO_NEXT_DAY" or not item.proposed_slot:
            continue
        order = orders[item.order_id]
        if set(linked_order_ids(order.order_id)) <= orders.keys():
            continue
        start = datetime.fromisoformat(item.proposed_slot["start"])
        end = datetime.fromisoformat(item.proposed_slot["end"])
        if start.date().isoformat() <= plan.date or end > datetime.fromisoformat(order.deadline):
            continue
        latest = start + (datetime.fromisoformat(order.latest_start) - datetime.fromisoformat(order.slot.start))
        latest = latest_allowed_start(order.work_type_code, start, latest)
        candidates.setdefault(start.date().isoformat(), []).append(order.model_copy(update={
            "slot": order.slot.model_copy(update=item.proposed_slot),
            "latest_start": min(end, latest).isoformat(),
        }))
    if not candidates:
        raise HTTPException(409, BULK_NO_SLOTS)

    before = WorkspaceSnapshot.capture(demo_comparisons)
    moved = set()
    destination_updates = {}
    for date, proposed in sorted(candidates.items()):
        day_start = datetime.fromisoformat(proposed[0].slot.start).replace(hour=0, minute=0, second=0)
        target_day = db["days"].get(date) or DayInput(date=date, now=day_start.isoformat(), orders=[])
        target_plan = db["plans"].get(date) or Plan(date=date, as_of=target_day.now, plan_version=0)
        existing_ids = {order.order_id for order in target_day.orders}
        additions = [order for order in proposed if order.order_id not in existing_ids]
        expanded_day = target_day.model_copy(update={"orders": [*target_day.orders, *additions]})
        prepared = preview_plan_change(target_plan, expanded_day, reference)
        preview = prepared[0]
        previous_visits = {stop.order_id for route in target_plan.routes for stop in route.stops if stop.type == "VISIT"}
        scheduled = {stop.order_id for route in preview.routes for stop in route.stops if stop.type == "VISIT"}
        accepted = scheduled & {order.order_id for order in additions}
        if not accepted or not previous_visits <= scheduled:
            continue
        preview.unassigned = [item for item in preview.unassigned if item.order_id in existing_ids]
        if any(set(group.order_ids) - existing_ids for group in preview.pgp_groups):
            continue
        accepted_day = prepared[1].model_copy(update={
            "orders": [*target_day.orders, *(order for order in additions if order.order_id in accepted)],
        })
        preview.summary.orders_total = len(accepted_day.orders)
        preview.summary.unassigned = len(preview.unassigned)
        if validate_plan(preview, accepted_day, reference, matrix=get_validation_matrix())["violations"]:
            continue
        destination_updates[date] = target_plan, (preview, accepted_day, prepared[2])
        moved.update(accepted)
    if not moved:
        raise HTTPException(409, BULK_NO_CAPACITY)
    if WorkspaceSnapshot.capture(demo_comparisons) != before:
        raise HTTPException(409, STALE)

    remaining_day = day.model_copy(update={
        "orders": [order for order in day.orders if order.order_id not in moved],
        "deferred_order_ids": [order_id for order_id in day.deferred_order_ids if order_id not in moved],
    })
    remaining = plan.model_copy(deep=True)
    remaining.plan_version += 1
    remaining.unassigned = [item for item in remaining.unassigned if item.order_id not in moved]
    remaining.alerts = [alert for alert in remaining.alerts if alert.order_id not in moved]
    remaining.summary.orders_total -= len(moved)
    remaining.summary.unassigned -= len(moved)
    for target, prepared in destination_updates.values():
        save_plan_change(target, prepared)
    event = {"type": "BULK_RESCHEDULE", "event_id": uuid4().hex, "timestamp": plan.as_of}
    save_plan_change(plan, (remaining, remaining_day, event))
    workspace_undo = WorkspaceUndo.create(before, WorkspaceSnapshot.capture(demo_comparisons))
    return {"plan": remaining, "moved": len(moved),
            "skipped": sum(map(len, candidates.values())) - len(moved),
            "undo_token": workspace_undo.token, "undo_seconds": UNDO_SECONDS}

ASSISTANT_NO_REASON = "Для этого наряда в текущем плане нет причины неназначения."
ASSISTANT_DRAFT = "Здравствуйте! Предлагаем перенести визит мастера на {date}, с {start} до {end}. Подходит ли вам это время?"


@router.get("/orders/{order_id}/reschedule-draft")
def get_reschedule_draft(order_id: str):
    plan, day, _ = get_active_dispatch_context()
    if not any(order.order_id == order_id for order in day.orders):
        raise HTTPException(404, NO_VISIT)
    rejected = next((item for item in plan.unassigned if item.order_id == order_id), None)
    reason = rejected.reason_text if rejected else ASSISTANT_NO_REASON
    draft = ""
    if rejected and rejected.proposed_slot:
        start = datetime.fromisoformat(rejected.proposed_slot["start"])
        end = datetime.fromisoformat(rejected.proposed_slot["end"])
        draft = ASSISTANT_DRAFT.format(date=start.strftime("%d.%m.%Y"),
                                       start=start.strftime("%H:%M"), end=end.strftime("%H:%M"))
    return {"reason": reason, "draft": draft}
