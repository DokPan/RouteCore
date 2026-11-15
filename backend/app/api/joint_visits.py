from datetime import datetime, time, timedelta
from itertools import permutations, zip_longest
from statistics import mean, pstdev
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .dispatch import (
    CONFLICT,
    NO_VISIT,
    STALE,
    get_active_dispatch_context,
    get_validation_matrix,
    preview_plan_change,
    run_workspace_change,
    save_plan_change,
    save_reversible_change,
)
from ..core.joint_visit_rules import PARTNER_ORDER_PREFIX
from ..core.plan_validation import validate_plan
from ..core.solver import official_shift_planner
from ..core.visit_slots import latest_allowed_start
from ..core.visit_warnings import RESCHEDULE_NOTICE_MINUTES


router = APIRouter(prefix="/api/v1/ui")
HEIGHT_VISIT_REQUIRED = "Совместный выезд доступен для высотных работ."
DISTINCT_STAFF_REQUIRED = "Выберите двух разных инженеров."
JOINT_VISIT_UNAVAILABLE = (
    "Для выбранной пары не удалось построить совместный визит к этому времени. "
    "Выберите другое время в согласованном слоте или другого напарника."
)
JOINT_SLOT_INVALID = "Начало совместного визита должно попадать в согласованный слот наряда."
JOINT_ALREADY_PLANNED = "Для этого наряда уже существует запись совместного выезда."
NO_JOINT_VISIT_FOUND = "Подходящая пара со свободным общим временем не найдена."
JOINT_CREW_SHORTAGE = "На смене меньше двух доступных инженеров с нужным профилем, технологией и допуском к высоте."
JOINT_SHIFT_CONFLICT = "В текущем слоте работа выходит за пределы смены инженеров с нужными допусками. Согласуйте более ранний слот."
JOINT_QUEUE_CHANGED = "Список визитов, требующих переноса, изменился. Запустите подбор ещё раз."
JOINT_SLOT_PASSED = "Допустимое время начала совместного визита уже прошло. Согласуйте новый слот."
JOINT_SEARCH_LIMIT = "Не удалось подобрать пару за отведённое время. Можно проверить выбранных инженеров вручную."
JOINT_SEARCH_SECONDS = 5
ALTERNATE_SLOT_STEP_MINUTES = 15
BATCH_PAIR_SECONDS = 8


class JointVisitRequest(BaseModel):
    lead_staff_id: str
    partner_staff_id: str
    start: datetime
    queue_order_ids: list[str] = Field(default_factory=list)
    new_slot: bool = False


def get_joint_order(plan, day, order_id):
    order = next((order for order in day.orders if order.order_id == order_id), None)
    if order is None:
        raise HTTPException(404, NO_VISIT)
    if order_id in plan.cancelled or any(
        stop.order_id == order_id
        and (stop.status in {"IN_PROGRESS", "DONE"} or datetime.fromisoformat(stop.start) < datetime.fromisoformat(plan.as_of))
        for route in plan.routes
        for stop in route.stops
    ):
        raise HTTPException(409, CONFLICT)
    return order


def _search_joint_visit_for_order(plan, day, reference, order, search_seconds=JOINT_SEARCH_SECONDS, raise_errors=True):
    order_id = order.order_id
    if not order.requires_height:
        if raise_errors:
            raise HTTPException(409, HEIGHT_VISIT_REQUIRED)
        return None
    work_type = next((work for work in reference.work_types if work.code == order.work_type_code), None)
    if not work_type:
        if raise_errors:
            raise HTTPException(409, NO_JOINT_VISIT_FOUND)
        return None
    routes_by_staff = {route.staff_id: route for route in plan.routes}
    eligible_engineers = [
        staff
        for staff in reference.staff
        if staff.staff_id in routes_by_staff
        and staff.staff_id not in (day.absent_staff_ids or [])
        and official_shift_planner.is_staff_qualified(staff, order, work_type)[0]
    ]
    if len(eligible_engineers) < 2:
        if raise_errors:
            raise HTTPException(409, JOINT_CREW_SHORTAGE)
        return None
    current_assignment = next(
        (
            route.staff_id
            for route in plan.routes
            if any(stop.order_id == order_id and stop.type == "VISIT" for stop in route.stops)
        ),
        None,
    )
    eligible_engineers.sort(
        key=lambda engineer: (
            engineer.staff_id != current_assignment,
            routes_by_staff[engineer.staff_id].totals.work_sec,
            engineer.staff_id,
        )
    )
    slot_start = datetime.fromisoformat(order.slot.start)
    latest_start = latest_allowed_start(order.work_type_code, slot_start, datetime.fromisoformat(order.latest_start))
    start = max(slot_start, datetime.fromisoformat(plan.as_of))
    if start.second or start.microsecond:
        start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if start > latest_start:
        if raise_errors:
            raise HTTPException(409, JOINT_SLOT_PASSED)
        return None
    visit_duration = timedelta(minutes=work_type.duration_min)
    availability = {
        engineer.staff_id: engineer_visit_window(
            engineer,
            routes_by_staff[engineer.staff_id],
            order_id,
            slot_start,
            datetime.fromisoformat(plan.as_of),
        )
        for engineer in eligible_engineers
    }
    earliest_end = start + visit_duration
    shift_conflict = sum(shift_end >= earliest_end for _, shift_end, _, _ in availability.values()) < 2
    search_deadline = monotonic() + search_seconds
    best_with_changes = None
    search_expired = False
    for request in joint_visit_candidates(availability, start, latest_start, visit_duration):
        if monotonic() >= search_deadline:
            search_expired = True
            break
        candidate = prepare_joint_visit(plan, day, reference, order, request)
        if candidate:
            prepared, visits, queue_order_ids = candidate
            if not queue_order_ids:
                return request, prepared, visits, queue_order_ids
            if best_with_changes is None or len(queue_order_ids) < len(best_with_changes[3]):
                best_with_changes = request, prepared, visits, queue_order_ids
    if best_with_changes:
        return best_with_changes
    earliest_notice = datetime.fromisoformat(plan.as_of) + timedelta(minutes=RESCHEDULE_NOTICE_MINUTES)
    if earliest_notice <= slot_start:
        search_deadline = monotonic() + search_seconds
        earliest_shift = min(window[0] for window in availability.values())
        latest_shift = max(window[1] for window in availability.values())
        latest_alternate = min(latest_shift, datetime.fromisoformat(order.deadline)) - visit_duration
        first_alternate = max(earliest_notice, earliest_shift)
        if first_alternate.second or first_alternate.microsecond:
            first_alternate = first_alternate.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for request in joint_visit_candidates(
            availability, first_alternate, latest_alternate, visit_duration, step=ALTERNATE_SLOT_STEP_MINUTES,
        ):
            if monotonic() >= search_deadline:
                break
            if slot_start <= request.start <= latest_start:
                continue
            alternate = request.model_copy(update={"new_slot": True})
            candidate = prepare_joint_visit(plan, day, reference, order, alternate)
            if candidate:
                prepared, visits, queue_order_ids = candidate
                if not queue_order_ids:
                    return alternate, prepared, visits, queue_order_ids
                if best_with_changes is None or len(queue_order_ids) < len(best_with_changes[3]):
                    best_with_changes = alternate, prepared, visits, queue_order_ids
        if best_with_changes:
            return best_with_changes
    if raise_errors:
        if shift_conflict:
            raise HTTPException(409, JOINT_SHIFT_CONFLICT)
        raise HTTPException(409, JOINT_SEARCH_LIMIT if search_expired else NO_JOINT_VISIT_FOUND)
    return None


@router.post("/orders/{order_id}/joint-visit/auto")
def suggest_joint_visit(order_id: str):
    plan, day, reference = get_active_dispatch_context()
    order = get_joint_order(plan, day, order_id)
    found = _search_joint_visit_for_order(plan, day, reference, order, search_seconds=JOINT_SEARCH_SECONDS, raise_errors=True)
    request, _, visits, queue_order_ids = found
    return describe_joint_visit(reference, request, visits, order_id, queue_order_ids)


def _apply_auto_pair_vls():
    plan, day, reference = get_active_dispatch_context()
    as_of_dt = datetime.fromisoformat(plan.as_of)
    already_paired = {
        item.order_id[len(PARTNER_ORDER_PREFIX):]
        for item in day.orders
        if item.order_id.startswith(PARTNER_ORDER_PREFIX)
    }
    pgp_orders = {order_id for group in plan.pgp_groups for order_id in group.order_ids}
    locked_orders = {
        stop.order_id
        for route in plan.routes
        for stop in route.stops
        if stop.type == "VISIT"
        and stop.status != "CANCELLED"
        and stop.order_id
        and (stop.status in {"IN_PROGRESS", "DONE"} or datetime.fromisoformat(stop.start) < as_of_dt)
    }
    candidates = [
        order
        for order in day.orders
        if order.requires_height
        and not order.order_id.startswith(PARTNER_ORDER_PREFIX)
        and order.order_id not in already_paired
        and order.order_id not in plan.cancelled
        and order.order_id not in pgp_orders
        and order.order_id not in locked_orders
    ]
    current_plan = plan
    current_day = day
    paired = []
    skipped = []
    deadline = monotonic() + BATCH_PAIR_SECONDS
    for order in candidates:
        remaining = deadline - monotonic()
        if remaining <= 0.1:
            skipped.append(order.order_id)
            continue
        found = _search_joint_visit_for_order(
            current_plan,
            current_day,
            reference,
            order,
            search_seconds=min(1.5, remaining),
            raise_errors=False,
        )
        if not found:
            skipped.append(order.order_id)
            continue
        request, prepared, visits, queue_order_ids = found
        current_plan, current_day, _ = prepared
        paired.append(
            {
                "order_id": order.order_id,
                **describe_joint_visit(reference, request, visits, order.order_id, queue_order_ids),
            }
        )

    if paired:
        event = {"type": "UI_JOINT_VISIT_AUTO_PAIR", "event_id": uuid4().hex, "timestamp": current_plan.as_of}
        save_plan_change(plan, (current_plan, current_day, event))

    return {
        "paired_count": len(paired),
        "already_paired_count": len(already_paired),
        "skipped_count": len(skipped),
        "paired": paired,
    }


@router.post("/joint-visits/auto-pair")
def auto_pair_vls_orders():
    return run_workspace_change(_apply_auto_pair_vls)


def joint_visit_candidates(availability, slot_start, latest_start, duration, step=1):
    pairs_by_start = []
    start = slot_start
    while start <= latest_start:
        end = start + duration
        conflicts = {
            staff_id: sum(busy_start < end and busy_end > start for busy_start, busy_end in movable_visits)
            for staff_id, (shift_start, shift_end, frozen_stops, movable_visits) in availability.items()
            if shift_start <= start
            and end <= shift_end
            and all(end <= busy_start or start >= busy_end for busy_start, busy_end in frozen_stops)
        }
        pairs = sorted(permutations(conflicts, 2), key=lambda pair: conflicts[pair[0]] + conflicts[pair[1]])
        if pairs:
            pairs_by_start.append([(start, lead, partner) for lead, partner in pairs])
        start += timedelta(minutes=step)

    for round_candidates in zip_longest(*pairs_by_start):
        for candidate in round_candidates:
            if candidate:
                start, lead_staff_id, partner_staff_id = candidate
                yield JointVisitRequest(lead_staff_id=lead_staff_id, partner_staff_id=partner_staff_id, start=start)


def engineer_visit_window(engineer, route, order_id, slot_start, current_time):
    shift_start = datetime.combine(slot_start.date(), time.fromisoformat(engineer.shift.start), slot_start.tzinfo)
    if route.unavailable_until:
        shift_start = max(shift_start, datetime.fromisoformat(route.unavailable_until))
    shift_end = datetime.combine(slot_start.date(), time.fromisoformat(engineer.shift.end), slot_start.tzinfo)
    frozen_stops = []
    movable_visits = []
    partner_order_id = PARTNER_ORDER_PREFIX + order_id
    for stop in route.stops:
        if stop.type not in {"VISIT", "RESTOCK", "BREAK"} or stop.status == "CANCELLED":
            continue
        if stop.order_id in {order_id, partner_order_id}:
            continue
        start = datetime.fromisoformat(stop.start)
        end = datetime.fromisoformat(stop.end)
        if start < current_time or stop.status in {"IN_PROGRESS", "DONE"} or stop.type == "RESTOCK":
            frozen_stops.append((start, end))
        elif stop.type == "VISIT":
            movable_visits.append((start, end))
    return shift_start, shift_end, frozen_stops, movable_visits


@router.post("/orders/{order_id}/joint-visit")
def preview_joint_visit(order_id: str, request: JointVisitRequest):
    return calculate_joint_visit(order_id, request, apply=False)


@router.post("/orders/{order_id}/joint-visit/confirm")
def confirm_joint_visit(order_id: str, request: JointVisitRequest):
    return calculate_joint_visit(order_id, request, apply=True)


def calculate_joint_visit(order_id, request, apply):
    plan, day, reference = get_active_dispatch_context()
    order = get_joint_order(plan, day, order_id)
    if not order.requires_height:
        raise HTTPException(409, HEIGHT_VISIT_REQUIRED)
    if request.lead_staff_id == request.partner_staff_id:
        raise HTTPException(422, DISTINCT_STAFF_REQUIRED)
    start = request.start
    slot_start = datetime.fromisoformat(order.slot.start)
    latest_start = latest_allowed_start(order.work_type_code, slot_start, datetime.fromisoformat(order.latest_start))
    model_time = datetime.fromisoformat(plan.as_of)
    earliest_notice = model_time + timedelta(minutes=RESCHEDULE_NOTICE_MINUTES)
    valid_start = (
        earliest_notice <= min(start, slot_start) and start.date() == slot_start.date()
        if request.new_slot
        else slot_start <= start <= latest_start
    )
    if start.utcoffset() != model_time.utcoffset() or start.second or start.microsecond or not valid_start:
        raise HTTPException(409, JOINT_SLOT_INVALID)

    candidate = prepare_joint_visit(plan, day, reference, order, request)
    if not candidate:
        raise HTTPException(409, JOINT_VISIT_UNAVAILABLE)
    prepared, visits, queue_order_ids = candidate
    if apply:
        if sorted(request.queue_order_ids) != queue_order_ids:
            raise HTTPException(409, JOINT_QUEUE_CHANGED)
        active_plan, active_day, active_reference = get_active_dispatch_context()
        if active_plan is not plan or active_day is not day or active_reference is not reference:
            raise HTTPException(409, STALE)
        return save_reversible_change(plan, prepared)
    return describe_joint_visit(reference, request, visits, order_id, queue_order_ids)


def prepare_joint_visit(plan, day, reference, order, request):
    order_id = order.order_id
    start = request.start
    start_iso = start.isoformat()
    slot_end = datetime.fromisoformat(order.slot.end)
    if request.new_slot:
        slot_end = start + (slot_end - datetime.fromisoformat(order.slot.start))
    fixed_slot = order.slot.model_copy(update={"start": start_iso, "end": slot_end.isoformat()})
    lead_order = order.model_copy(update={"slot": fixed_slot, "latest_start": start_iso})
    partner_order_id = PARTNER_ORDER_PREFIX + order_id
    if order_id.startswith(PARTNER_ORDER_PREFIX):
        raise HTTPException(409, JOINT_ALREADY_PLANNED)
    partner_order = lead_order.model_copy(
        update={
            "order_id": partner_order_id,
            "required_equipment": {},
            "access_node_id": None,
        }
    )
    preview_day = day.model_copy(
        update={
            "orders": [lead_order if item.order_id == order_id else item for item in day.orders if item.order_id != partner_order_id]
            + [partner_order],
            "deferred_order_ids": [item for item in day.deferred_order_ids if item != order_id],
        }
    )
    assignments = {
        lead_order.order_id: request.lead_staff_id,
        partner_order.order_id: request.partner_staff_id,
    }
    affected_staff = set(assignments.values()) | {
        route.staff_id for route in plan.routes if any(stop.order_id in assignments for stop in route.stops)
    }
    affected_routes = [route for route in plan.routes if route.staff_id in affected_staff]
    affected_orders = set(assignments) | {stop.order_id for route in affected_routes for stop in route.stops if stop.order_id}
    scoped_plan = plan.model_copy(
        update={
            "routes": affected_routes,
            "unassigned": [],
            "pgp_groups": [],
            "cancelled": [],
            "alerts": [],
        }
    )
    scoped_day = preview_day.model_copy(
        update={
            "orders": [item for item in preview_day.orders if item.order_id in affected_orders],
        }
    )
    scoped_reference = reference.model_copy(
        update={
            "staff": [engineer for engineer in reference.staff if engineer.staff_id in affected_staff],
        }
    )
    proposal, _, event = preview_plan_change(
        scoped_plan, scoped_day, scoped_reference, assignments, prioritize_slot_order=True,
    )
    visits = {
        stop.order_id: stop
        for route in proposal.routes
        for stop in route.stops
        if stop.order_id in assignments and route.staff_id == assignments[stop.order_id] and datetime.fromisoformat(stop.start) == start
    }
    if len(visits) != 2 or len({stop.end for stop in visits.values()}) != 1:
        return None
    if request.new_slot and datetime.fromisoformat(visits[order_id].end) > datetime.fromisoformat(order.deadline):
        return None
    previous_orders = {
        stop.order_id for route in affected_routes for stop in route.stops if stop.type == "VISIT" and stop.status != "CANCELLED"
    }
    proposed_orders = {
        stop.order_id for route in proposal.routes for stop in route.stops if stop.type == "VISIT" and stop.status != "CANCELLED"
    }
    queue_order_ids = sorted(previous_orders - proposed_orders)
    if any(item.startswith(PARTNER_ORDER_PREFIX) for item in queue_order_ids):
        return None
    changed_routes = {route.staff_id: route for route in proposal.routes}
    routes = [changed_routes.get(route.staff_id, route) for route in plan.routes]
    routes.extend(route for route in proposal.routes if route.staff_id not in {item.staff_id for item in plan.routes})
    unassigned = [item for item in plan.unassigned if item.order_id not in assignments]
    unassigned.extend(item for item in proposal.unassigned if item.order_id in queue_order_ids)
    work_times = [route.totals.work_sec for route in routes if route.totals and route.status != "UNAVAILABLE"]
    average_work = mean(work_times) if work_times else 0
    summary = plan.summary.model_copy(
        update={
            "visits_planned": sum(
                stop.type == "VISIT" and stop.status != "CANCELLED" and not (stop.order_id or "").startswith(PARTNER_ORDER_PREFIX)
                for route in routes
                for stop in route.stops
            ),
            "unassigned": sum(not item.order_id.startswith(PARTNER_ORDER_PREFIX) for item in unassigned),
            "travel_sec_total": sum(route.totals.travel_sec for route in routes if route.totals),
            "utilization_cv": round(pstdev(work_times) / average_work, 3) if average_work else 0,
        }
    )
    updated_plan = plan.model_copy(
        update={
            "routes": routes,
            "unassigned": unassigned,
            "summary": summary,
            "plan_version": proposal.plan_version,
            "based_on_event_id": event["event_id"],
            "computation_ms": proposal.computation_ms,
            "alerts": [
                alert for alert in plan.alerts if alert.staff_id not in affected_staff and alert.order_id not in assignments
            ]
            + proposal.alerts,
        }
    )
    if validate_plan(updated_plan, preview_day, reference, matrix=get_validation_matrix(), previous=plan)["violations"]:
        return None
    return (updated_plan, preview_day, event), visits, queue_order_ids


def describe_joint_visit(reference, request, visits, order_id, queue_order_ids):
    staff_names = {staff.staff_id: staff.display_name for staff in reference.staff}
    return {
        **request.model_dump(mode="json"),
        "queue_order_ids": queue_order_ids,
        "end": visits[order_id].end,
        "crew": [
            {"staff_id": staff_id, "name": staff_names[staff_id], "travel_sec": visits[visit_id].travel_sec}
            for visit_id, staff_id in (
                (order_id, request.lead_staff_id),
                (PARTNER_ORDER_PREFIX + order_id, request.partner_staff_id),
            )
        ],
    }
