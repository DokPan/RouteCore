from collections import Counter


STOP_LABELS = {
    "DEPOT_START": "Выезд из СЦ",
    "DEPOT_END": "Возврат в СЦ",
    "BREAK": "Обед",
    "RESTOCK": "Пополнение оборудования",
}


def build_route_sheet(route, day, reference):
    staff = next(person for person in reference.staff if person.staff_id == route.staff_id)
    centers = {center.service_center_id: center for center in reference.service_centers}
    work_names = {work.code: work.name for work in reference.work_types}
    orders = {order.order_id: order for order in day.orders}
    consumption = Counter()
    remaining = dict(staff.equipment_start)
    stops = []
    active_stops = [stop for stop in route.stops if stop.status != "CANCELLED"]
    visit_stops = [stop for stop in active_stops if stop.type == "VISIT"]
    done_seqs = [stop.seq for stop in visit_stops if stop.status in ("DONE", "COMPLETED")]
    max_done_seq = max(done_seqs) if done_seqs else 0
    all_visits_done = bool(visit_stops) and all(stop.status in ("DONE", "COMPLETED") for stop in visit_stops)
    for stop in active_stops:
        order = orders.get(stop.order_id)
        center = centers.get(stop.location_id)
        equipment = order.required_equipment if order else {}
        consumption.update(equipment)
        if stop.equipment_after is not None:
            remaining = dict(stop.equipment_after)
        is_completed = (
            stop.status in ("DONE", "COMPLETED")
            or (max_done_seq > 0 and stop.seq < max_done_seq)
            or (stop.type == "DEPOT_END" and all_visits_done)
        )
        dist_km = round(stop.distance_m / 1000, 2)
        fact_end_iso = stop.actual_end or stop.end
        stops.append({
            "start": stop.start[11:16], "end": stop.end[11:16],
            "order_id": stop.order_id, "equipment": equipment,
            "distance_km": dist_km,
            "address": order.address if order else center.address if center else "",
            "work": work_names.get(order.work_type_code, order.work_type_code) if order else STOP_LABELS.get(stop.type, stop.type),
            "status": stop.status,
            "completed": is_completed,
            "actual_arrival": stop.arrival[11:16] if is_completed else "",
            "actual_departure": fact_end_iso[11:16] if is_completed else "",
            "actual_km": dist_km if is_completed else "",
        })
    return {
        "date": day.date, "staff_id": staff.staff_id, "name": staff.display_name,
        "center": centers[staff.service_center_id].name, "shift": staff.shift,
        "initial_load": staff.equipment_start, "capacity": staff.equipment_capacity,
        "consumption": consumption, "remaining": remaining, "stops": stops,
        "base_address": centers[staff.service_center_id].address,
    }
