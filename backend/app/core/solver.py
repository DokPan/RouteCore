import math
import time
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
import numpy as np

from ..models.official_spec import (
    Staff, ServiceCenter, WorkType, Order, Plan, Route, Stop,
    UnassignedOrder, PgpGroup, Alert, PlanSummary, RouteTotals
)
from .distance import distance_engine
from .next_workday import next_workday_slot
from .roads import OSRM_URL, road_table
from .visit_slots import latest_allowed_start

VISIT_EXPLANATION = "Допуск к {technology}. Дорога: {minutes} мин. Визит в слот: {start}. {equipment}"
RESTOCKED = "Оборудование после пополнения в СЦ."
EQUIPMENT_AVAILABLE = "Оборудование в наличии."
SHIFT_START_EXPLANATION = "Начало смены в СЦ"
MANUAL_RESTOCK_EXPLANATION = "Заезд в СЦ по запросу мастера"
RESTOCK_EXPLANATION = "Пополнение АО в СЦ"
BREAK_EXPLANATION = "Обеденный перерыв"
RETURN_EXPLANATION = "Возврат в СЦ {center_name}"
REJECTION_TEXT = {
    "NO_QUALIFIED_STAFF": "Нет доступного мастера с нужным допуском.",
    "SLOT_UNREACHABLE": "Подходящие мастера не успевают в согласованное время.",
    "NO_EQUIPMENT": "Подходящим мастерам не хватает оборудования.",
    "NO_SHIFT_TIME": "Визит и возвращение в сервисный центр не помещаются в смену.",
}

def parse_time_to_minutes(time_val: str) -> int:
    if not time_val:
        return 0
    if "T" in time_val:
        time_part = time_val.split("T")[1][:5]
    else:
        time_part = time_val[:5]
    h, m = map(int, time_part.split(":"))
    return h * 60 + m

def minutes_to_iso(date_str: str, minutes: int, tz: str = "+05:00") -> str:
    h = (minutes // 60) % 24
    m = minutes % 60
    return f"{date_str}T{h:02d}:{m:02d}:00{tz}"

class OfficialShiftPlanner:
    def __init__(self):
        self.dist_engine = distance_engine

    def is_staff_qualified(self, staff: Staff, order: Order, work_type: Optional[WorkType]) -> Tuple[bool, str]:

        if work_type:
            profile_match = any(p in staff.profiles for p in work_type.profiles)
            if not profile_match:
                return False, "NO_QUALIFIED_STAFF"


        if order.technology and order.technology not in staff.technologies:
            return False, "NO_QUALIFIED_STAFF"


        if order.requires_height and not staff.height_permit:
            return False, "NO_QUALIFIED_STAFF"

        return True, "OK"

    def plan_day(
        self,
        date_str: str,
        now_iso: str,
        staff_list: List[Staff],
        orders: List[Order],
        service_centers: Dict[str, ServiceCenter],
        work_types: Dict[str, WorkType],
        absent_staff_ids: Optional[List[str]] = None,
        pgp_groups: Optional[List[PgpGroup]] = None,
        plan_version: int = 1,
        frozen_routes: Optional[Dict[str, Route]] = None,
        known_orders: Optional[List[Order]] = None,
        assignments: Optional[Dict[str, str]] = None,
        preferred_assignments: Optional[Dict[str, str]] = None,
        duration_forecasts: Optional[Dict[str, int]] = None,
        available_from: Optional[Dict[str, str]] = None,
        manual_restock_staff_id: Optional[str] = None,
        congestion_factors: Optional[Dict[int, float]] = None,
        order_priority: Optional[Any] = None,
        prioritize_slot_order: bool = False,
    ) -> Plan:
        start_comp_time = time.time()
        absent_set = set(absent_staff_ids or [])
        sc_map = service_centers

        matrix = self.dist_engine.matrix
        factors = {item.hour: item.factor for item in (matrix.hourly_factors or [])} if matrix else {}
        factors.update(congestion_factors or {})
        matrix_points = set(matrix.points) if matrix else set()
        points = {(sc.lat, sc.lon) for sc in service_centers.values()}
        points.update((order.lat, order.lon) for order in orders)
        historical = {order.building_id: (order.lat, order.lon) for order in (known_orders or orders)}
        points.update(historical.values())
        now_min = parse_time_to_minutes(now_iso)
        use_demo_roads = matrix is None or matrix.matrix_id == "ekb-metro-2026-v1"
        covered = matrix_points if not use_demo_roads else set()
        needs_roads = use_demo_roads or any(
            order.building_id not in covered for order in orders
        ) or any(sc_id not in covered for sc_id in service_centers)
        costs = road_table(tuple(sorted(points))) if OSRM_URL and needs_roads else None

        def travel(origin_id, target_id, hour, origin, target):
            if origin == target:
                return 0, 0
            if not (costs is not None and use_demo_roads):
                if origin_id in matrix_points and target_id in matrix_points:
                    seconds, meters = self.dist_engine.get_travel(origin_id, target_id)
                    return round(seconds * factors.get(hour, 1.0)), meters
            if costs is not None:
                cost = costs[origin, target]
                if cost is None:
                    return None
                seconds, meters = cost
                return round(seconds * factors.get(hour, 1.0)), meters
            distance = self.dist_engine.calculate_distance_km(*origin, *target)
            seconds = self.dist_engine.calculate_travel_time_sec(*origin, *target)
            return round(seconds * factors.get(hour, 1.0)), int(distance * 1000)


        def order_sort_key(order: Order):
            if order_priority is not None:
                return order_priority(order)
            work_type = work_types.get(order.work_type_code)
            standard_duration = work_type.duration_min if work_type else 60
            planned_duration = (duration_forecasts or {}).get(order.order_id, standard_duration)
            slot_start = parse_time_to_minutes(order.slot.start)
            if prioritize_slot_order:
                return (slot_start, not order.is_repeat, planned_duration, order.order_id)
            latest_start = parse_time_to_minutes(latest_allowed_start(
                order.work_type_code,
                datetime.fromisoformat(order.slot.start),
                datetime.fromisoformat(order.latest_start),
            ).isoformat())
            return (slot_start, latest_start, not order.is_repeat, planned_duration, order.order_id)

        sorted_orders = sorted(orders, key=order_sort_key)


        worker_state = {}
        for s in staff_list:
            if s.staff_id in absent_set:
                continue

            sc = sc_map.get(s.service_center_id)
            if sc is None:
                continue
            sc_lat = sc.lat
            sc_lon = sc.lon if sc else 60.6010
            sc_loc_id = sc.service_center_id if sc else s.service_center_id

            shift_start_min = parse_time_to_minutes(s.shift.start)
            shift_end_min = parse_time_to_minutes(s.shift.end)

            worker_state[s.staff_id] = {
                "staff": s,
                "sc": sc,
                "sc_loc_id": sc_loc_id,
                "curr_time_min": max(shift_start_min, now_min, parse_time_to_minutes((available_from or {}).get(s.staff_id, "00:00"))),
                "curr_lat": sc_lat,
                "curr_lon": sc_lon,
                "curr_loc_id": sc_loc_id,
                "stock": dict(s.equipment_start),
                "had_break": False,
                "stops": [
                    Stop(
                        seq=1,
                        type="DEPOT_START",
                        location_id=sc_loc_id,
                        order_id=None,
                        arrival=minutes_to_iso(date_str, shift_start_min),
                        start=minutes_to_iso(date_str, shift_start_min),
                        end=minutes_to_iso(date_str, shift_start_min),
                        travel_sec=0,
                        distance_m=0,
                        status="PLANNED",
                        equipment_after=dict(s.equipment_start),
                        explanation=SHIFT_START_EXPLANATION
                    )
                ],
                "total_travel_sec": 0,
                "total_work_sec": 0,
                "total_dist_m": 0,
                "visits_count": 0
            }
            prefix = frozen_routes.get(s.staff_id) if frozen_routes is not None else None
            if prefix and prefix.stops:
                state = worker_state[s.staff_id]
                last = prefix.stops[-1]
                center = sc_map.get(last.location_id)
                location = (center.lat, center.lon) if center else historical[last.location_id]
                state.update(
                    stops=list(prefix.stops),
                    curr_time_min=max(state["curr_time_min"], parse_time_to_minutes(last.end)),
                    curr_loc_id=last.location_id,
                    curr_lat=location[0],
                    curr_lon=location[1],
                    stock=dict(last.equipment_after if last.equipment_after is not None else s.equipment_start),
                    had_break=any(stop.type == "BREAK" for stop in prefix.stops),
                    visits_count=sum(stop.type == "VISIT" for stop in prefix.stops),
                    total_travel_sec=sum(stop.travel_sec for stop in prefix.stops),
                    total_dist_m=sum(stop.distance_m for stop in prefix.stops),
                    total_work_sec=sum(
                        (parse_time_to_minutes(stop.end) - parse_time_to_minutes(stop.start)) * 60
                        for stop in prefix.stops if stop.type == "VISIT"
                    ),
                )

            if s.staff_id == manual_restock_staff_id:
                state = worker_state[s.staff_id]
                departure = state["curr_time_min"]
                cost = travel(state["curr_loc_id"], sc_loc_id, departure // 60,
                              (state["curr_lat"], state["curr_lon"]), (sc_lat, sc_lon))
                if cost is None:
                    continue
                seconds, meters = cost
                arrival = departure + math.ceil(seconds / 60)
                start = max(arrival, parse_time_to_minutes(sc.opening_hours.open))
                end = start + sc.restock_duration_min
                break_start = max(end, parse_time_to_minutes(s.shift.break_window.from_))
                if (end > min(shift_end_min, parse_time_to_minutes(sc.opening_hours.close))
                        or (not state["had_break"] and (
                            break_start > parse_time_to_minutes(s.shift.break_window.to)
                            or break_start + s.shift.break_duration_min > shift_end_min))):
                    continue
                state["stock"] = dict(s.equipment_capacity)
                state["stops"].append(Stop(
                    seq=len(state["stops"]) + 1, type="RESTOCK", location_id=sc_loc_id,
                    arrival=minutes_to_iso(date_str, arrival), start=minutes_to_iso(date_str, start),
                    end=minutes_to_iso(date_str, end), travel_sec=seconds, distance_m=meters,
                    equipment_after=dict(state["stock"]), explanation=MANUAL_RESTOCK_EXPLANATION,
                ))
                state.update(curr_time_min=end, curr_loc_id=sc_loc_id, curr_lat=sc_lat, curr_lon=sc_lon)
                state["total_travel_sec"] += seconds
                state["total_dist_m"] += meters

        unassigned_orders: List[UnassignedOrder] = []

        pinned_orders_by_staff: Dict[str, List[Order]] = {}
        if assignments:
            orders_by_id = {o.order_id: o for o in sorted_orders}
            for p_oid, p_sid in assignments.items():
                if p_oid in orders_by_id:
                    pinned_orders_by_staff.setdefault(p_sid, []).append(orders_by_id[p_oid])

        for order in sorted_orders:
            wt = work_types.get(order.work_type_code)
            duration_min = (duration_forecasts or {}).get(order.order_id, wt.duration_min if wt else 60)
            slot_start_min = parse_time_to_minutes(order.slot.start)
            latest_start_min = parse_time_to_minutes(latest_allowed_start(
                order.work_type_code,
                datetime.fromisoformat(order.slot.start),
                datetime.fromisoformat(order.latest_start),
            ).isoformat())

            best_staff_id = None
            best_cost = (True, True, float("inf"))
            district_id = order.district_id
            if not district_id and use_demo_roads and order.access_node_id:

                district_id = f"SC-{order.access_node_id.removeprefix('AN-')[:2]}"
            if district_id not in service_centers:
                district_id = None
            best_arrival_min = None
            best_travel_sec = 0
            best_travel_dist = 0
            best_needs_restock = False
            best_break_inserted = False
            best_extra_restock_sec = 0
            best_extra_restock_dist = 0
            rejection_reasons = []
            has_qualified_staff = False

            for st_id, st_data in worker_state.items():
                if assignments and assignments.get(order.order_id, st_id) != st_id:
                    continue
                if st_data["stops"][-1].type == "DEPOT_END":
                    continue
                staff: Staff = st_data["staff"]
                sc = st_data["sc"]


                qualified, r_code = self.is_staff_qualified(staff, order, wt)
                if not qualified:
                    rejection_reasons.append(r_code)
                    continue
                has_qualified_staff = True

                curr_t = st_data["curr_time_min"]
                curr_loc = st_data["curr_loc_id"]
                curr_lat = st_data["curr_lat"]
                curr_lon = st_data["curr_lon"]
                stock = st_data["stock"]
                had_break = st_data["had_break"]


                needs_restock = False
                for eq_k, needed_qty in order.required_equipment.items():
                    if stock.get(eq_k, 0) < needed_qty:
                        needs_restock = True
                        break

                temp_t = curr_t
                temp_loc = curr_loc
                temp_lat = curr_lat
                temp_lon = curr_lon
                extra_restock_travel_sec = 0
                extra_restock_dist_m = 0

                if needs_restock:
                    if any(staff.equipment_capacity.get(key, 0) < qty for key, qty in order.required_equipment.items()):
                        rejection_reasons.append("NO_EQUIPMENT")
                        continue

                    leg = travel(curr_loc, sc.service_center_id, curr_t // 60, (curr_lat, curr_lon), (sc.lat, sc.lon))
                    if leg is None:
                        rejection_reasons.append("SLOT_UNREACHABLE")
                        continue
                    t_sec, d_m = leg

                    restock_dur_min = sc.restock_duration_min if sc else 15
                    travel_m = int(math.ceil(t_sec / 60))
                    opening = sc.opening_hours
                    if (temp_t + travel_m < parse_time_to_minutes(opening.open)
                            or temp_t + travel_m + restock_dur_min > parse_time_to_minutes(opening.close)):
                        rejection_reasons.append("NO_EQUIPMENT")
                        continue
                    temp_t += travel_m + restock_dur_min
                    temp_loc = sc.service_center_id
                    temp_lat = sc.lat
                    temp_lon = sc.lon
                    extra_restock_travel_sec = t_sec
                    extra_restock_dist_m = d_m


                leg = travel(temp_loc, order.building_id, temp_t // 60, (temp_lat, temp_lon), (order.lat, order.lon))
                if leg is None:
                    rejection_reasons.append("SLOT_UNREACHABLE")
                    continue
                t_sec, d_m = leg

                travel_m = int(math.ceil(t_sec / 60))
                earliest_arrival_min = temp_t + travel_m

                break_window_from = parse_time_to_minutes(staff.shift.break_window.from_)
                break_window_to = parse_time_to_minutes(staff.shift.break_window.to)
                break_dur = staff.shift.break_duration_min
                shift_end_min = parse_time_to_minutes(staff.shift.end)

                break_candidates = []
                if max(earliest_arrival_min, slot_start_min) <= latest_start_min:
                    a_start = max(earliest_arrival_min, slot_start_min)
                    a_end = a_start + duration_min
                    if had_break or max(a_end, break_window_from) <= break_window_to:
                        break_candidates.append((False, earliest_arrival_min, a_start, a_end, t_sec, d_m))

                if not had_break:
                    break_start = max(temp_t, break_window_from)
                    if break_start <= break_window_to:
                        departure = break_start + break_dur
                        b_leg = travel(temp_loc, order.building_id, departure // 60, (temp_lat, temp_lon), (order.lat, order.lon))
                        if b_leg is not None:
                            b_sec, b_dist = b_leg
                            b_arr = departure + math.ceil(b_sec / 60)
                            if max(b_arr, slot_start_min) <= latest_start_min:
                                b_start = max(b_arr, slot_start_min)
                                b_end = b_start + duration_min
                                break_candidates.append((True, b_arr, b_start, b_end, b_sec, b_dist))

                if not break_candidates:
                    if not had_break and max(temp_t, break_window_from) > break_window_to:
                        rejection_reasons.append("NO_SHIFT_TIME")
                    else:
                        rejection_reasons.append("SLOT_UNREACHABLE")
                    continue

                chosen = None
                chosen_key = None
                for cand in break_candidates:
                    cand_break, cand_arr, cand_start, cand_end, cand_t_sec, cand_d_m = cand
                    if assignments and order.order_id not in assignments and st_id in pinned_orders_by_staff:
                        pinned_conflict = False
                        for p_ord in pinned_orders_by_staff[st_id]:
                            if any(stp.order_id == p_ord.order_id for stp in st_data["stops"]):
                                continue
                            p_latest = parse_time_to_minutes(latest_allowed_start(
                                p_ord.work_type_code,
                                datetime.fromisoformat(p_ord.slot.start),
                                datetime.fromisoformat(p_ord.latest_start),
                            ).isoformat())
                            p_leg = travel(
                                order.building_id,
                                p_ord.building_id,
                                cand_end // 60,
                                (order.lat, order.lon),
                                (p_ord.lat, p_ord.lon),
                            )
                            if p_leg is None or cand_end + int(math.ceil(p_leg[0] / 60)) > p_latest:
                                pinned_conflict = True
                                break
                        if pinned_conflict:
                            continue

                    return_from = cand_end
                    if not had_break and not cand_break:
                        return_from = max(cand_end, break_window_from) + break_dur
                    back = travel(order.building_id, sc.service_center_id, return_from // 60, (order.lat, order.lon), (sc.lat, sc.lon))
                    if back is None:
                        continue
                    cand_back_sec, _ = back
                    cand_back_min = int(math.ceil(cand_back_sec / 60))
                    if return_from + cand_back_min > shift_end_min:
                        continue
                    ready_min = cand_end if (had_break or cand_break) else max(cand_end, break_window_from) + break_dur
                    cand_key = (ready_min, cand_end, cand_t_sec)
                    if chosen_key is None or cand_key < chosen_key:
                        chosen_key = cand_key
                        chosen = (cand_break, cand_arr, cand_start, cand_end, cand_t_sec, cand_d_m, cand_back_sec)

                if chosen is None:
                    rejection_reasons.append("NO_SHIFT_TIME")
                    continue

                break_inserted, earliest_arrival_min, actual_start_min, end_min, t_sec, d_m, back_sec = chosen


                workload_penalty = (st_data["total_work_sec"] / 3600.0) * 120
                wait_penalty = max(0, actual_start_min - earliest_arrival_min) * 18

                cross_district = district_id is not None and district_id != sc.service_center_id
                if district_id is None:
                    distance = self.dist_engine.calculate_distance_km(sc.lat, sc.lon, order.lat, order.lon)
                    travel_cost = t_sec + distance * 70
                else:
                    travel_cost = t_sec + 0.25 * back_sec
                preferred_staff = (preferred_assignments or {}).get(order.order_id)
                changed_assignment = preferred_staff is not None and preferred_staff != st_id
                cost = (changed_assignment, cross_district, travel_cost + extra_restock_travel_sec + workload_penalty + wait_penalty)

                if cost < best_cost:
                    best_cost = cost
                    best_staff_id = st_id
                    best_arrival_min = actual_start_min
                    best_travel_sec = t_sec
                    best_travel_dist = d_m
                    best_needs_restock = needs_restock
                    best_break_inserted = break_inserted
                    best_extra_restock_sec = extra_restock_travel_sec
                    best_extra_restock_dist = extra_restock_dist_m


            if best_staff_id:
                st = worker_state[best_staff_id]
                staff = st["staff"]
                sc = st["sc"]


                if best_needs_restock:
                    to_sc_min = int(math.ceil(best_extra_restock_sec / 60))
                    arr_sc = st["curr_time_min"] + to_sc_min
                    restock_dur = sc.restock_duration_min if sc else 15
                    dep_sc = arr_sc + restock_dur

                    st["stock"] = dict(staff.equipment_capacity)
                    st["stops"].append(Stop(
                        seq=len(st["stops"]) + 1,
                        type="RESTOCK",
                        location_id=sc.service_center_id,
                        order_id=None,
                        arrival=minutes_to_iso(date_str, arr_sc),
                        start=minutes_to_iso(date_str, arr_sc),
                        end=minutes_to_iso(date_str, dep_sc),
                        travel_sec=best_extra_restock_sec,
                        distance_m=best_extra_restock_dist,
                        status="PLANNED",
                        equipment_after=dict(st["stock"]),
                        explanation=RESTOCK_EXPLANATION
                    ))
                    st["total_travel_sec"] += best_extra_restock_sec
                    st["total_dist_m"] += best_extra_restock_dist
                    st["curr_time_min"] = dep_sc
                    st["curr_loc_id"] = sc.service_center_id
                    st["curr_lat"] = sc.lat
                    st["curr_lon"] = sc.lon


                if best_break_inserted and not st["had_break"]:
                    br_start = max(st["curr_time_min"], parse_time_to_minutes(staff.shift.break_window.from_))
                    br_end = br_start + staff.shift.break_duration_min
                    st["stops"].append(Stop(
                        seq=len(st["stops"]) + 1,
                        type="BREAK",
                        location_id=st["curr_loc_id"],
                        order_id=None,
                        arrival=minutes_to_iso(date_str, br_start),
                        start=minutes_to_iso(date_str, br_start),
                        end=minutes_to_iso(date_str, br_end),
                        travel_sec=0,
                        distance_m=0,
                        status="PLANNED",
                        equipment_after=dict(st["stock"]),
                        explanation=BREAK_EXPLANATION
                    ))
                    st["had_break"] = True
                    st["curr_time_min"] = br_end


                vis_arrival_min = st["curr_time_min"] + int(math.ceil(best_travel_sec / 60))
                vis_start_min = max(vis_arrival_min, slot_start_min)
                vis_end_min = vis_start_min + duration_min


                for eq_k, needed_qty in order.required_equipment.items():
                    st["stock"][eq_k] = max(0, st["stock"].get(eq_k, 0) - needed_qty)

                st["stops"].append(Stop(
                    seq=len(st["stops"]) + 1,
                    type="VISIT",
                    location_id=order.building_id,
                    order_id=order.order_id,
                    arrival=minutes_to_iso(date_str, vis_arrival_min),
                    start=minutes_to_iso(date_str, vis_start_min),
                    end=minutes_to_iso(date_str, vis_end_min),
                    travel_sec=best_travel_sec,
                    distance_m=best_travel_dist,
                    status="PLANNED",
                    planned_duration_min=duration_min,
                    equipment_after=dict(st["stock"]),
                    explanation=VISIT_EXPLANATION.format(
                        technology=order.technology, minutes=math.ceil(best_travel_sec / 60),
                        start=minutes_to_iso(date_str, vis_start_min)[11:16],
                        equipment=RESTOCKED if best_needs_restock else EQUIPMENT_AVAILABLE,
                    ),
                ))

                st["curr_time_min"] = vis_end_min
                st["curr_loc_id"] = order.building_id
                st["curr_lat"] = order.lat
                st["curr_lon"] = order.lon
                st["total_travel_sec"] += best_travel_sec
                st["total_work_sec"] += duration_min * 60
                st["total_dist_m"] += best_travel_dist
                st["visits_count"] += 1
            else:
                reason_code = "NO_SHIFT_TIME"
                if not has_qualified_staff:
                    reason_code = "NO_QUALIFIED_STAFF"
                elif "NO_EQUIPMENT" in rejection_reasons:
                    reason_code = "NO_EQUIPMENT"
                elif "NO_SHIFT_TIME" in rejection_reasons:
                    reason_code = "NO_SHIFT_TIME"
                elif "SLOT_UNREACHABLE" in rejection_reasons:
                    reason_code = "SLOT_UNREACHABLE"

                proposed_slot = next_workday_slot(order)
                unassigned_orders.append(UnassignedOrder(
                    order_id=order.order_id,
                    reason_code=reason_code,
                    reason_text=REJECTION_TEXT[reason_code],
                    suggested_action="RESCHEDULE_TO_NEXT_DAY" if proposed_slot else "MANUAL_REVIEW",
                    proposed_slot=proposed_slot,
                ))


        routes: List[Route] = []
        work_times = []
        total_travel_sec_all = 0
        planned_visits_count = 0

        for st_id, st in worker_state.items():
            staff = st["staff"]
            sc = st["sc"]
            if st["stops"][-1].type == "DEPOT_END":
                previous = frozen_routes[st_id]
                routes.append(previous)
                total_travel_sec_all += st["total_travel_sec"]
                planned_visits_count += st["visits_count"]
                work_times.append(st["total_work_sec"])
                continue


            if st["visits_count"] > 0 and not st["had_break"]:
                br_start = max(st["curr_time_min"], parse_time_to_minutes(staff.shift.break_window.from_))
                br_end = br_start + staff.shift.break_duration_min
                st["stops"].append(Stop(
                    seq=len(st["stops"]) + 1,
                    type="BREAK",
                    location_id=st["curr_loc_id"],
                    order_id=None,
                    arrival=minutes_to_iso(date_str, br_start),
                    start=minutes_to_iso(date_str, br_start),
                    end=minutes_to_iso(date_str, br_end),
                    travel_sec=0,
                    distance_m=0,
                    status="PLANNED",
                    equipment_after=dict(st["stock"]),
                    explanation=BREAK_EXPLANATION
                ))
                st["had_break"] = True
                st["curr_time_min"] = br_end


            back_sec, back_dist = travel(
                st["curr_loc_id"], sc.service_center_id, st["curr_time_min"] // 60,
                (st["curr_lat"], st["curr_lon"]), (sc.lat, sc.lon),
            )

            back_min = int(math.ceil(back_sec / 60))
            finish_arr_min = st["curr_time_min"] + back_min
            shift_end_min = parse_time_to_minutes(staff.shift.end)
            final_end_min = max(finish_arr_min, shift_end_min)

            st["stops"].append(Stop(
                seq=len(st["stops"]) + 1,
                type="DEPOT_END",
                location_id=sc.service_center_id,
                order_id=None,
                arrival=minutes_to_iso(date_str, finish_arr_min),
                start=minutes_to_iso(date_str, finish_arr_min),
                end=minutes_to_iso(date_str, final_end_min),
                travel_sec=back_sec,
                distance_m=back_dist,
                status="PLANNED",
                equipment_after=dict(st["stock"]),
                explanation=RETURN_EXPLANATION.format(center_name=sc.name)
            ))
            st["total_travel_sec"] += back_sec
            st["total_dist_m"] += back_dist

            route_totals = RouteTotals(
                visits_count=st["visits_count"],
                travel_sec=st["total_travel_sec"],
                work_sec=st["total_work_sec"],
                distance_m=st["total_dist_m"]
            )

            routes.append(Route(
                staff_id=staff.staff_id,
                service_center_id=staff.service_center_id,
                status="ACTIVE",
                stops=st["stops"],
                totals=route_totals
            ))

            if st["total_work_sec"] > 0:
                work_times.append(st["total_work_sec"])
            total_travel_sec_all += st["total_travel_sec"]
            planned_visits_count += st["visits_count"]


        util_cv = 0.0
        if len(work_times) > 1:
            mean_w = float(np.mean(work_times))
            std_w = float(np.std(work_times))
            util_cv = round(std_w / mean_w, 3) if mean_w > 0 else 0.0

        pgp_list = pgp_groups or []
        pgp_orders_cnt = sum(len(g.order_ids) for g in pgp_list)
        total_orders_cnt = len(orders) + pgp_orders_cnt

        summary = PlanSummary(
            orders_total=total_orders_cnt,
            visits_planned=planned_visits_count,
            unassigned=len(unassigned_orders),
            pgp_orders=pgp_orders_cnt,
            cancelled=0,
            travel_sec_total=total_travel_sec_all,
            utilization_cv=util_cv
        )

        comp_ms = int((time.time() - start_comp_time) * 1000)

        return Plan(
            date=date_str,
            plan_version=plan_version,
            based_on_event_id=None,
            as_of=now_iso,
            computation_ms=comp_ms,
            routes=routes,
            unassigned=unassigned_orders,
            pgp_groups=pgp_list,
            cancelled=[],
            alerts=[],
            summary=summary
        )

official_shift_planner = OfficialShiftPlanner()
