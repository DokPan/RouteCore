from collections import Counter
from .joint_visit_rules import PARTNER_ORDER_PREFIX, linked_order_ids
from .visit_slots import latest_allowed_start
from datetime import datetime, time, timedelta


def validate_plan(plan, day, reference, matrix=None, previous=None, event=None, visit_end_overrides=None,
                  order_service_centers=None):
    orders = {order.order_id: order for order in day.orders}
    staff = {person.staff_id: person for person in reference.staff}
    centers = {center.service_center_id: center for center in reference.service_centers}
    works = {work.code: work for work in reference.work_types}
    indices = {point: index for index, point in enumerate(matrix.points)} if matrix else {}
    violations = []
    occurrences = Counter(plan.cancelled)
    occurrences.update(item.order_id for item in plan.unassigned)
    occurrences.update(order_id for group in plan.pgp_groups for order_id in group.order_ids)
    on_time = set()
    in_slot = set()
    current_stops = {}

    def issue(rule, staff_id=None, order_id=None, seq=None):
        violations.append({"rule": rule, "staff_id": staff_id, "order_id": order_id, "seq": seq})

    for route in plan.routes:
        person = staff.get(route.staff_id)
        if person is None:
            issue("H1", route.staff_id)
            continue
        stock = dict(person.equipment_start)
        visits = [stop for stop in route.stops if stop.type == "VISIT" and stop.status != "CANCELLED"]
        breaks = [stop for stop in route.stops if stop.type == "BREAK"]
        if visits and len(breaks) != 1:
            issue("H3", route.staff_id)
        previous_stop = None
        for stop in route.stops:
            if stop.status == "CANCELLED":
                continue
            arrival, start, end = map(datetime.fromisoformat, (stop.arrival, stop.start, stop.end))
            duration = (end - start).total_seconds()
            current_stops[route.staff_id, stop.seq] = stop

            def fail(rule):
                issue(rule, route.staff_id, stop.order_id, stop.seq)

            if start.time() < time.fromisoformat(person.shift.start) or end.time() > time.fromisoformat(person.shift.end):
                fail("H2")
            if route.unavailable_from and start >= datetime.fromisoformat(route.unavailable_from):
                if not route.unavailable_until or start < datetime.fromisoformat(route.unavailable_until):
                    fail("H2")
            if arrival > start or start > end:
                fail("H6")
            if previous_stop:
                travel = stop.travel_sec
                if matrix and previous_stop.location_id in indices and stop.location_id in indices:
                    travel = matrix.durations_sec[indices[previous_stop.location_id]][indices[stop.location_id]]
                if (arrival - datetime.fromisoformat(previous_stop.end)).total_seconds() < travel:
                    fail("H6")
            previous_stop = stop

            if stop.type == "BREAK":
                window = person.shift.break_window
                if not time.fromisoformat(window.from_) <= start.time() <= time.fromisoformat(window.to) or duration < person.shift.break_duration_min * 60:
                    fail("H3")
            elif stop.type in {"DEPOT_START", "DEPOT_END"}:
                if stop.location_id != person.service_center_id:
                    fail("H8")
            elif stop.type == "RESTOCK":
                center = centers.get(stop.location_id)
                if (not center or start.time() < time.fromisoformat(center.opening_hours.open)
                        or end.time() > time.fromisoformat(center.opening_hours.close)
                        or duration < center.restock_duration_min * 60):
                    fail("H8")
                stock = dict(person.equipment_capacity)
            elif stop.type == "VISIT":
                occurrences[stop.order_id] += 1
                order = orders.get(stop.order_id)
                if order is None:
                    continue
                work = works.get(order.work_type_code)
                if (not work or not set(person.profiles).intersection(work.profiles)
                        or order.technology not in person.technologies
                        or (order.requires_height and not person.height_permit)):
                    fail("H1")
                slot_start = datetime.fromisoformat(order.slot.start)
                deadline = latest_allowed_start(
                    order.work_type_code,
                    slot_start,
                    datetime.fromisoformat(order.latest_start),
                )
                if not slot_start <= start <= deadline:
                    fail("H4")
                else:
                    in_slot.add(stop.order_id)
                actual_end = (visit_end_overrides or {}).get((route.staff_id, stop.order_id))
                if event and event.get("order_id") == stop.order_id and event.get("staff_id") == route.staff_id:
                    actual_end = event.get("actual_end") or event.get("expected_end")
                expected_minutes = stop.planned_duration_min or (work.duration_min if work else None)
                if expected_minutes and duration != expected_minutes * 60 and stop.end != actual_end:
                    fail("H5")
                for key, quantity in order.required_equipment.items():
                    stock[key] = stock.get(key, 0) - quantity
                if any(quantity < 0 for quantity in stock.values()):
                    fail("H7")
                if end <= datetime.fromisoformat(order.deadline):
                    on_time.add(stop.order_id)

    for order_id in sorted(orders.keys() | occurrences.keys(), key=str):
        if order_id not in orders or occurrences[order_id] != 1:
            issue("H9", order_id=order_id)

    if previous:
        now = datetime.fromisoformat(plan.as_of)
        previous_links = {
            (route.staff_id, stop.type, stop.order_id, stop.location_id):
            (predecessor.type, predecessor.order_id, predecessor.location_id)
            for route in previous.routes for predecessor, stop in zip(route.stops, route.stops[1:])
            if stop.type == "VISIT"
        }
        for route in plan.routes:
            for predecessor, stop in zip(route.stops, route.stops[1:]):
                if stop.type != "VISIT" or datetime.fromisoformat(stop.start) < now:
                    continue
                key = (route.staff_id, stop.type, stop.order_id, stop.location_id)
                link = (predecessor.type, predecessor.order_id, predecessor.location_id)
                if previous_links.get(key) != link:
                    departure = max(now, datetime.fromisoformat(predecessor.end))
                    if datetime.fromisoformat(stop.arrival) < departure + timedelta(seconds=stop.travel_sec):
                        issue("H10", route.staff_id, stop.order_id, stop.seq)
        for route in previous.routes:
            for stop in route.stops:
                if datetime.fromisoformat(stop.start) >= now or stop.status == "CANCELLED":
                    continue
                current = current_stops.get((route.staff_id, stop.seq))
                fields = ("type", "order_id", "location_id", "arrival", "start")
                allowed_end = event and event.get("type") in {"VISIT_DELAYED", "ORDER_TRANSFERRED", "VISIT_COMPLETED"} and event.get("order_id") == stop.order_id and event.get("staff_id") == route.staff_id
                if current is None or any(getattr(current, field) != getattr(stop, field) for field in fields) or (not allowed_end and current.end != stop.end):
                    issue("H10", route.staff_id, stop.order_id, stop.seq)

    active_visit_stops = {}
    for route in plan.routes:
        for stop in route.stops:
            if stop.type == "VISIT" and stop.status != "CANCELLED" and stop.order_id:
                active_visit_stops.setdefault(stop.order_id, []).append((route.staff_id, stop))

    for partner_id in sorted(orders):
        if not partner_id.startswith(PARTNER_ORDER_PREFIX):
            continue
        base_id = partner_id[len(PARTNER_ORDER_PREFIX):]
        base_hits = active_visit_stops.get(base_id, [])
        partner_hits = active_visit_stops.get(partner_id, [])
        if not base_hits and not partner_hits:
            continue
        if len(base_hits) != 1 or len(partner_hits) != 1:
            issue("H1", order_id=base_id)
            continue
        base_staff, base_stop = base_hits[0]
        partner_staff, partner_stop = partner_hits[0]
        if base_staff == partner_staff:
            issue("H1", partner_staff, partner_id, partner_stop.seq)
        if base_stop.start != partner_stop.start or base_stop.end != partner_stop.end:
            issue("H4", partner_staff, partner_id, partner_stop.seq)

    active_orders = {order_id for order_id in orders.keys() - set(plan.cancelled)
                     if not order_id.startswith(PARTNER_ORDER_PREFIX)}
    invalid_orders = {linked_order_ids(violation["order_id"])[0] for violation in violations
                      if violation["rule"] != "H4" and violation["order_id"]}
    deadline_met = (on_time & active_orders) - invalid_orders
    service_center_sla = {}
    if order_service_centers is not None:
        for order_id in active_orders:
            center_id = order_service_centers.get(order_id)
            if center_id is None:
                continue
            totals = service_center_sla.setdefault(center_id, {"weighted_total": 0, "weighted_on_time": 0})
            weight = 1 + orders[order_id].is_repeat
            totals["weighted_total"] += weight
            if order_id in deadline_met:
                totals["weighted_on_time"] += weight
    return {
        "service_center_sla": service_center_sla,
        "violations": violations,
        "checked_history": previous is not None,
        "on_time": len(deadline_met),
        "in_slot": len((in_slot & active_orders) - invalid_orders),
        "weighted_on_time": sum(1 + orders[order_id].is_repeat for order_id in deadline_met),
        "weighted_total": sum(1 + orders[order_id].is_repeat for order_id in active_orders),
        "weighted_in_slot": sum(1 + orders[order_id].is_repeat for order_id in (in_slot & active_orders) - invalid_orders),
        "travel_sec": sum(stop.travel_sec for route in plan.routes for stop in route.stops if stop.status != "CANCELLED"),
        "distance_m": sum(stop.distance_m for route in plan.routes for stop in route.stops if stop.status != "CANCELLED"),
        "utilization_cv": plan.summary.utilization_cv if plan.summary else 0,
    }
