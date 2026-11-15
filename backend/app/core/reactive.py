import time
from datetime import datetime, timedelta
from ..models.official_spec import (
    Alert, EventResult, RouteTotals, Order, UnassignedOrder
)

from .distance import distance_engine
from .solver import MANUAL_RESTOCK_EXPLANATION, official_shift_planner
from .pgp_detector import pgp_detector
from .next_workday import next_workday_slot
from .visit_warnings import forecast_visit_warnings
from .plan_improvement import REPLAN_SEARCH_SECONDS, improve_plan
from .joint_visit_rules import JOINT_VISIT_REVIEW, PARTNER_ORDER_PREFIX, linked_order_ids


PLAN_RECALCULATED = "План пересчитан"
TIME_UPDATED = "Модельное время обновлено"
LOST_VISIT_WARNING = "После события наряд {order_id} не помещается в текущий план."

SUBSCRIBER_UNAVAILABLE = 'Абонент не ответил. Нужен повторный визит.'
STAFF_REMOVED_ORDER = 'Инженер снят со смены. Нужен перенос или переназначение наряда.'

REPLAN_EVENTS = {"NEW_ORDER", "ORDER_CANCELLED", "ORDER_DEFERRED", "STAFF_UNAVAILABLE", "VISIT_DELAYED", "ORDER_TRANSFERRED", "VISIT_COMPLETED"}


def replan_event(plan, day, reference, event, assignments=None, congestion_factors=None,
                 preserve_assignments=True, group_incidents=True,
                 search_seconds=REPLAN_SEARCH_SECONDS, manual_restock_staff_id=None, prioritize_slot_order=False):
    started_at = time.perf_counter()
    timestamp = event["timestamp"]
    now = datetime.fromisoformat(timestamp)
    kind = event["type"]
    frozen = {}
    locked = set()
    prev_routes = {route.staff_id: route for route in plan.routes}
    prev_unassigned = {item.order_id: item for item in plan.unassigned}
    for route in plan.routes:
        prefix = [
            stop.model_copy(deep=True) for stop in route.stops
            if stop.status != "CANCELLED" and (
                stop.status in {"IN_PROGRESS", "DONE"} or datetime.fromisoformat(stop.start) < now
                or (kind in {"VISIT_DELAYED", "ORDER_TRANSFERRED", "VISIT_COMPLETED"}
                    and route.staff_id == event.get("staff_id") and stop.order_id
                    and stop.order_id == event.get("order_id") and datetime.fromisoformat(stop.start) == now)
                or (stop.type == "RESTOCK" and stop.explanation == MANUAL_RESTOCK_EXPLANATION)
            )
        ]
        for stop in prefix:
            if stop.order_id:
                locked.add(stop.order_id)
            if (kind in {"VISIT_DELAYED", "ORDER_TRANSFERRED", "VISIT_COMPLETED"}
                    and route.staff_id == event.get("staff_id")
                    and stop.order_id and stop.order_id == event.get("order_id")):
                value = event.get("expected_end") or event.get("actual_end")
                if value and datetime.fromisoformat(value) >= datetime.fromisoformat(stop.start):
                    stop.end = value
                    if kind in {"ORDER_TRANSFERRED", "VISIT_COMPLETED"}:
                        stop.status = "DONE"
                        stop.actual_end = event.get("recorded_end", value)
        frozen[route.staff_id] = route.model_copy(update={"stops": prefix})

    orders = {order.order_id: order for order in day.orders}
    cancelled = set(plan.cancelled)
    deferred = set(day.deferred_order_ids)
    reassignment_queue = set()
    if kind == 'ORDER_DEFERRED' and event['order_id'] in orders.keys() - locked - cancelled:
        related_ids = set(linked_order_ids(event['order_id'])) & orders.keys()
        if not related_ids & locked:
            deferred.update(related_ids)
    unavailable = set(day.absent_staff_ids or [])
    temporary_absences = {
        route.staff_id: route.unavailable_until for route in plan.routes
        if route.unavailable_until and datetime.fromisoformat(route.unavailable_until) > now
    }
    if kind == "NEW_ORDER":
        order = Order.model_validate(event["order"])
        orders.setdefault(order.order_id, order)
    elif kind == "ORDER_CANCELLED":
        linked = set(linked_order_ids(event["order_id"]))
        affected = linked if linked <= orders.keys() else {event["order_id"]}
        if not affected & locked:
            cancelled.update(affected)
    elif kind == "STAFF_UNAVAILABLE":
        if event.get("until"):
            temporary_absences[event["staff_id"]] = event["until"]
        else:
            unavailable.add(event["staff_id"])
            if event.get("reassign_mode") in {"backlog", "reschedule_tomorrow"}:
                reassignment_queue = {
                    stop.order_id for route in plan.routes if route.staff_id == event["staff_id"]
                    for stop in route.stops if stop.type == "VISIT" and stop.order_id and stop.status != "CANCELLED"
                } - locked - cancelled
                deferred.update(reassignment_queue)

    previous_assignments = {
        stop.order_id: route.staff_id for route in plan.routes for stop in route.stops
        if stop.type == "VISIT" and stop.status != "CANCELLED"
    }
    joint_pairs = [
        set(linked_order_ids(order_id)) for order_id in orders
        if order_id.startswith(PARTNER_ORDER_PREFIX)
    ]
    known_assignments = previous_assignments.keys() | (assignments or {}).keys()
    incomplete_pairs = set().union(*(pair for pair in joint_pairs if pair - known_assignments))
    fixed_assignments = dict(assignments or {})
    for pair in joint_pairs:
        fixed_assignments.update({order_id: previous_assignments[order_id] for order_id in pair & previous_assignments.keys()})

    pending = [order for key, order in orders.items() if key not in locked | cancelled | deferred]
    work_types = {work.code: work for work in reference.work_types}

    deferred_staff_ids = set()
    if kind == "ORDER_DEFERRED":
        deferred_order_id = event.get("order_id")
        if deferred_order_id:
            deferred_related = set(linked_order_ids(deferred_order_id)) & orders.keys()
            deferred_staff_ids = {
                previous_assignments[oid] for oid in deferred_related if oid in previous_assignments
            }

    if kind == "ORDER_DEFERRED":
        groups = [
            group for group in plan.pgp_groups
            if not (set(group.order_ids) & (cancelled | deferred))
        ]
        removed_ids = deferred & set(linked_order_ids(event.get("order_id") or "")) if event.get("order_id") else set()
        if deferred_staff_ids and removed_ids:
            staff_by_id = {s.staff_id: s for s in reference.staff}
            merged_routes = [
                remove_visits_and_close_gap(route, removed_ids, orders, reference, staff_by_id.get(route.staff_id), now,
                                            congestion_factors)
                if route.staff_id in deferred_staff_ids else route.model_copy(deep=True)
                for route in plan.routes if route.staff_id not in (unavailable & frozen.keys())
            ]
            recalc_unassigned = []
        else:
            merged_routes = [
                route.model_copy(deep=True) for route in plan.routes if route.staff_id not in (unavailable & frozen.keys())
            ]
            recalc_unassigned = []
        kept_unassigned_ids = {item.order_id for item in recalc_unassigned}
        carried_unassigned = [
            item.model_copy(deep=True) for item in plan.unassigned
            if item.order_id in orders and item.order_id not in (cancelled | deferred | kept_unassigned_ids)
        ]
        updated = plan.model_copy(deep=True)
        updated.plan_version = plan.plan_version + 1
        updated.as_of = timestamp
        updated.routes = merged_routes
        updated.pgp_groups = groups
        updated.unassigned = carried_unassigned + recalc_unassigned
    else:
        dispatchable, groups = (
            pgp_detector.detect_and_filter(orders=pending, as_of=timestamp, work_types=reference.work_types)
            if group_incidents else (pending, [])
        )
        planner_options = dict(
            date_str=plan.date, now_iso=timestamp, staff_list=reference.staff,
            orders=dispatchable, service_centers={center.service_center_id: center for center in reference.service_centers},
            work_types=work_types,
            absent_staff_ids=list(unavailable), pgp_groups=groups, plan_version=plan.plan_version + 1,
            frozen_routes=frozen, known_orders=list(orders.values()),
            assignments=fixed_assignments,
            preferred_assignments=previous_assignments if preserve_assignments else None,
            available_from=temporary_absences,
            manual_restock_staff_id=manual_restock_staff_id,
            congestion_factors=congestion_factors,
            prioritize_slot_order=prioritize_slot_order,
        )
        updated = official_shift_planner.plan_day(**planner_options)
        if not joint_pairs and kind in {"MANUAL_REPLAN", "UI_REPLAN"}:
            updated = improve_plan(official_shift_planner, updated, planner_options,
                                   search_seconds - (time.perf_counter() - started_at))
        interrupted = set()
        while joint_pairs:
            assigned_visits = {
                stop.order_id: (stop.start, stop.end)
                for route in updated.routes for stop in route.stops if stop.type == "VISIT"
            }
            failed_pairs = set().union(*(
                pair for pair in joint_pairs
                if len({assigned_visits.get(order_id) for order_id in pair}) > 1
            ))
            newly_interrupted = (incomplete_pairs | failed_pairs) - locked - cancelled - deferred - interrupted
            if not newly_interrupted:
                break
            interrupted.update(newly_interrupted)
            planner_options["orders"] = [order for order in dispatchable if order.order_id not in interrupted]
            updated = official_shift_planner.plan_day(**planner_options)
        if interrupted:
            updated.unassigned.extend(
                UnassignedOrder(order_id=order_id, reason_code="OTHER", reason_text=JOINT_VISIT_REVIEW,
                                suggested_action="REASSIGN")
                for order_id in sorted(interrupted)
            )
            updated.summary.unassigned = len(updated.unassigned)
    for route in updated.routes:
        prior_route = prev_routes.get(route.staff_id)
        if route.staff_id in temporary_absences:
            prior = frozen.get(route.staff_id)
            route.unavailable_from = prior.unavailable_from if prior and prior.unavailable_from else timestamp
            route.unavailable_until = temporary_absences[route.staff_id]
        if kind == "STAFF_UNAVAILABLE" and event.get("staff_id") == route.staff_id:
            route.unavailable_reason = event.get("reason") or (prior_route.unavailable_reason if prior_route else None)
            raw_comment = event.get("comment")
            if raw_comment is not None:
                cleaned_comment = str(raw_comment).strip()
                route.unavailable_comment = cleaned_comment if cleaned_comment else "-"
            elif prior_route and prior_route.unavailable_comment is not None:
                route.unavailable_comment = str(prior_route.unavailable_comment).strip() or "-"
            else:
                route.unavailable_comment = "-"
        elif prior_route and (prior_route.unavailable_reason or prior_route.unavailable_comment):
            route.unavailable_reason = prior_route.unavailable_reason
            route.unavailable_comment = prior_route.unavailable_comment
    for staff_id in sorted(unavailable & frozen.keys()):
        route = frozen[staff_id]
        prior_route = prev_routes.get(staff_id)
        route.status = "UNAVAILABLE"
        route.unavailable_from = route.unavailable_from or timestamp
        if kind == "STAFF_UNAVAILABLE" and event.get("staff_id") == staff_id:
            route.unavailable_reason = event.get("reason") or (prior_route.unavailable_reason if prior_route else None)
            raw_comment = event.get("comment")
            if raw_comment is not None:
                cleaned_comment = str(raw_comment).strip()
                route.unavailable_comment = cleaned_comment if cleaned_comment else "-"
            elif prior_route and prior_route.unavailable_comment is not None:
                route.unavailable_comment = str(prior_route.unavailable_comment).strip() or "-"
            else:
                route.unavailable_comment = "-"
        elif prior_route:
            route.unavailable_reason = prior_route.unavailable_reason
            route.unavailable_comment = prior_route.unavailable_comment
        if prior_route:
            kept_keys = {(stop.type, stop.order_id, stop.start) for stop in route.stops}
            cancelled_stops = [
                stop.model_copy(update={"status": "CANCELLED"})
                for stop in prior_route.stops
                if stop.type == "VISIT" and (stop.type, stop.order_id, stop.start) not in kept_keys
            ]
            if cancelled_stops:
                route.stops.extend(cancelled_stops)
                route.stops.sort(key=lambda stop: stop.start)
        active_stops = [stop for stop in route.stops if stop.status != "CANCELLED"]
        route.totals = RouteTotals(
            visits_count=sum(stop.type == "VISIT" for stop in active_stops),
            travel_sec=sum(stop.travel_sec for stop in active_stops),
            distance_m=sum(stop.distance_m for stop in active_stops),
            work_sec=sum(
                int((datetime.fromisoformat(stop.end) - datetime.fromisoformat(stop.start)).total_seconds())
                for stop in active_stops if stop.type == "VISIT"
            ),
        )
        updated.routes.append(route)

    deferred.difference_update(cancelled)
    for order_id in sorted(deferred & orders.keys()):
        prev_item = prev_unassigned.get(order_id)
        if order_id not in reassignment_queue and prev_item and not (kind == "ORDER_DEFERRED" and event.get("order_id") == order_id):
            proposed_slot = prev_item.proposed_slot or next_workday_slot(orders[order_id])
            updated.unassigned.append(prev_item.model_copy(update={
                "proposed_slot": proposed_slot,
            }))
        else:
            proposed_slot = None if order_id in reassignment_queue and event.get("reassign_mode") == "backlog" \
                else next_workday_slot(orders[order_id])
            updated.unassigned.append(UnassignedOrder(
                order_id=order_id, reason_code='OTHER',
                reason_text=STAFF_REMOVED_ORDER if order_id in reassignment_queue else SUBSCRIBER_UNAVAILABLE,
                suggested_action='RESCHEDULE_TO_NEXT_DAY' if proposed_slot else 'MANUAL_REVIEW',
                proposed_slot=proposed_slot,
            ))
    updated.cancelled = sorted(cancelled)
    updated.based_on_event_id = event["event_id"]
    updated.summary.orders_total = sum(not order_id.startswith(PARTNER_ORDER_PREFIX) for order_id in orders)
    updated.summary.cancelled = sum(not order_id.startswith(PARTNER_ORDER_PREFIX) for order_id in cancelled)
    updated.summary.visits_planned = sum(
        stop.type == "VISIT" and stop.status != "CANCELLED" and stop.order_id is not None
        and not stop.order_id.startswith(PARTNER_ORDER_PREFIX)
        for route in updated.routes for stop in route.stops
    )
    updated.summary.unassigned = sum(not order.order_id.startswith(PARTNER_ORDER_PREFIX) for order in updated.unassigned)
    updated.summary.pgp_orders = sum(len(group.order_ids) for group in updated.pgp_groups)
    updated.computation_ms = round((time.perf_counter() - started_at) * 1000)
    updated.summary.travel_sec_total = sum(route.totals.travel_sec for route in updated.routes)
    work_times = [route.totals.work_sec for route in updated.routes if route.totals and route.totals.work_sec > 0]
    if len(work_times) > 1:
        mean_w = sum(work_times) / len(work_times)
        var_w = sum((w - mean_w) ** 2 for w in work_times) / len(work_times)
        updated.summary.utilization_cv = round((var_w ** 0.5) / mean_w, 3) if mean_w > 0 else 0.0
    else:
        updated.summary.utilization_cv = 0.0
    new_alerts = forecast_visit_warnings(plan, updated, orders, event)
    warned_orders = {alert.order_id for alert in new_alerts}
    fm_staff_id = event.get("staff_id") if kind == "STAFF_UNAVAILABLE" else None
    fm_reason = (event.get("reason") or "").strip() if kind == "STAFF_UNAVAILABLE" else ""
    fm_comment = (str(event.get("comment") or "").strip() or "-") if kind == "STAFF_UNAVAILABLE" else ""
    new_alerts.extend([
        Alert(
            alert_id=f"{event['event_id']}:{order.order_id}",
            created_at=timestamp,
            type="LATE_RISK",
            order_id=order.order_id,
            staff_id=previous_assignments[order.order_id],
            suggested_action="REASSIGN",
            message=(
                f"{LOST_VISIT_WARNING.format(order_id=order.order_id)}"
                + (f" Причина: {fm_reason or 'Форс-мажор'}. Комментарий: {fm_comment}"
                   if fm_staff_id and previous_assignments[order.order_id] == fm_staff_id else "")
            ),
        )
        for order in updated.unassigned if order.order_id in previous_assignments and order.order_id not in warned_orders
    ])
    unassigned_ids = {order.order_id for order in updated.unassigned}
    updated.alerts = [
        alert for alert in plan.alerts if alert.order_id in unassigned_ids and alert.order_id not in {new.order_id for new in new_alerts}
    ] + new_alerts
    before = {route.staff_id: route for route in plan.routes}
    after = {route.staff_id: route for route in updated.routes}
    result = EventResult(
        event_id=event["event_id"], plan_version=updated.plan_version, as_of=timestamp,
        processing_ms=round((time.perf_counter() - started_at) * 1000),
        changed_staff_ids=sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key)),
        message=PLAN_RECALCULATED,
        new_alerts=new_alerts,
    )
    return updated, day.model_copy(update={"orders": list(orders.values()), "absent_staff_ids": sorted(unavailable), "now": timestamp, "deferred_order_ids": sorted(deferred)}), result


def advance_model_time(plan, day, event=None):
    if event is None and isinstance(day, dict):
        day, event = None, day
    started_at = time.perf_counter()
    updated_plan = plan.model_copy(update={
        "plan_version": plan.plan_version + 1,
        "as_of": event["timestamp"],
        "based_on_event_id": event["event_id"],
    })
    orders = {order.order_id: order for order in day.orders} if day is not None else {}
    new_alerts = forecast_visit_warnings(plan, updated_plan, orders, event) if orders else []
    if new_alerts:
        existing_ids = {alert.order_id for alert in updated_plan.alerts}
        updated_plan.alerts = list(updated_plan.alerts) + [
            alert for alert in new_alerts if alert.order_id not in existing_ids
        ]
    result = EventResult(
        event_id=event["event_id"], plan_version=updated_plan.plan_version,
        as_of=updated_plan.as_of,
        processing_ms=round((time.perf_counter() - started_at) * 1000),
        message=TIME_UPDATED,
        new_alerts=new_alerts,
    )
    return updated_plan, result



def _location_coords(location_id, orders_by_building, centers):
    center = centers.get(location_id)
    if center:
        return center.lat, center.lon
    order = orders_by_building.get(location_id)
    if order:
        return order.lat, order.lon
    return None


def _leg(origin_id, target_id, origin, target, departure, congestion_factors):
    if origin_id == target_id or origin is None or target is None or origin == target:
        return 0, 0
    hour = departure.hour
    factor = (congestion_factors or {}).get(hour, distance_engine.get_hourly_factor(hour))
    matrix = distance_engine.matrix
    if matrix and origin_id in distance_engine._point_indices and target_id in distance_engine._point_indices:
        seconds, meters = distance_engine.get_travel(origin_id, target_id)
        return round(seconds * max(factor, 1.0)), meters
    seconds = distance_engine.calculate_travel_time_sec(*origin, *target)
    meters = int(distance_engine.calculate_distance_km(*origin, *target) * 1000)
    return round(seconds * max(factor, 1.0)), meters


def remove_visits_and_close_gap(route, removed_ids, orders, reference, staff, now, congestion_factors=None):
    updated = route.model_copy(deep=True)
    stops = list(updated.stops)
    removed_positions = [
        index for index, stop in enumerate(stops)
        if stop.type == "VISIT" and stop.order_id in removed_ids and stop.status != "CANCELLED"
    ]
    if not removed_positions:
        return updated
    first_removed = removed_positions[0]
    returned_equipment = {}
    for index in removed_positions:
        order = orders.get(stops[index].order_id)
        for key, quantity in ((order.required_equipment or {}).items() if order else []):
            returned_equipment[key] = returned_equipment.get(key, 0) + quantity
    kept = [stop for index, stop in enumerate(stops) if index not in set(removed_positions)]
    centers = {center.service_center_id: center for center in reference.service_centers}
    orders_by_building = {order.building_id: order for order in orders.values()}
    restocked = False
    for index in range(first_removed, len(kept)):
        stop = kept[index]
        if stop.status == "CANCELLED":
            continue
        if stop.type == "RESTOCK":
            restocked = True
        if not restocked and returned_equipment and stop.equipment_after is not None:
            after = dict(stop.equipment_after)
            for key, quantity in returned_equipment.items():
                after[key] = after.get(key, 0) + quantity
            stop.equipment_after = after
        if stop.status in {"IN_PROGRESS", "DONE"} or datetime.fromisoformat(stop.start) < now:
            continue
        previous = next((item for item in reversed(kept[:index]) if item.status != "CANCELLED"), None)
        if previous is None:
            continue
        departure = max(now, datetime.fromisoformat(previous.end))
        if index == first_removed:
            seconds, meters = _leg(
                previous.location_id, stop.location_id,
                _location_coords(previous.location_id, orders_by_building, centers),
                _location_coords(stop.location_id, orders_by_building, centers),
                departure, congestion_factors,
            )
            stop.travel_sec = seconds
            stop.distance_m = meters
        old_start = datetime.fromisoformat(stop.start)
        old_end = datetime.fromisoformat(stop.end)
        duration = old_end - old_start
        arrival = departure + timedelta(seconds=stop.travel_sec)
        if arrival.second:
            arrival = arrival + timedelta(seconds=60 - arrival.second)
        earliest = arrival
        if stop.type == "VISIT":
            order = orders.get(stop.order_id)
            if order:
                earliest = max(arrival, datetime.fromisoformat(order.slot.start))
        elif stop.type == "BREAK" and staff:
            window_from = datetime.combine(old_start.date(), datetime.strptime(staff.shift.break_window.from_, "%H:%M").time(), old_start.tzinfo)
            earliest = max(arrival, window_from)
        elif stop.type == "RESTOCK":
            center = centers.get(stop.location_id)
            if center:
                opening = datetime.combine(old_start.date(), datetime.strptime(center.opening_hours.open, "%H:%M").time(), old_start.tzinfo)
                earliest = max(arrival, opening)
        if stop.type == "DEPOT_END":
            stop.arrival = arrival.isoformat()
            stop.start = arrival.isoformat()
            stop.end = max(old_end, arrival).isoformat()
            continue
        stop.arrival = arrival.isoformat()
        stop.start = earliest.isoformat()
        stop.end = (earliest + duration).isoformat()
    for seq, stop in enumerate(kept, 1):
        stop.seq = seq
    updated.stops = kept
    active = [stop for stop in kept if stop.status != "CANCELLED"]
    updated.totals = RouteTotals(
        visits_count=sum(stop.type == "VISIT" for stop in active),
        travel_sec=sum(stop.travel_sec for stop in active),
        distance_m=sum(stop.distance_m for stop in active),
        work_sec=sum(
            int((datetime.fromisoformat(stop.end) - datetime.fromisoformat(stop.start)).total_seconds())
            for stop in active if stop.type == "VISIT"
        ),
    )
    return updated
