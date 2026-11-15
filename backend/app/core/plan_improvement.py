import math
from datetime import datetime
from time import monotonic

from ..models.official_spec import Route, RouteTotals, Stop, UnassignedOrder
from .joint_visit_rules import PARTNER_ORDER_PREFIX
from .next_workday import next_workday_slot
from .roads import OSRM_URL, road_table
from .solver import (
    BREAK_EXPLANATION,
    EQUIPMENT_AVAILABLE,
    MANUAL_RESTOCK_EXPLANATION,
    REJECTION_TEXT,
    RESTOCKED,
    RESTOCK_EXPLANATION,
    RETURN_EXPLANATION,
    VISIT_EXPLANATION,
    minutes_to_iso,
    parse_time_to_minutes,
)
from .visit_slots import latest_allowed_start


SEARCH_SECONDS = 75
REPLAN_SEARCH_SECONDS = 2


def _make_travel_fn(planner, options):
    matrix = planner.dist_engine.matrix
    factors = {item.hour: item.factor for item in (matrix.hourly_factors or [])} if matrix else {}
    factors.update(options.get("congestion_factors") or {})
    matrix_points = set(matrix.points) if matrix else set()
    service_centers = options["service_centers"]
    orders = options["orders"]
    known_orders = options.get("known_orders") or orders

    use_demo_roads = matrix is None or matrix.matrix_id == "ekb-metro-2026-v1"
    covered = matrix_points if not use_demo_roads else set()
    points = {(sc.lat, sc.lon) for sc in service_centers.values()}
    points.update((o.lat, o.lon) for o in orders)
    points.update((o.lat, o.lon) for o in known_orders)
    needs_roads = use_demo_roads or any(
        o.building_id not in covered for o in orders
    ) or any(sc_id not in covered for sc_id in service_centers)
    costs = road_table(tuple(sorted(points))) if OSRM_URL and needs_roads else None

    cache = {}

    def travel(origin_id, target_id, hour, origin, target):
        key = (origin_id, target_id, hour, origin, target)
        cached = cache.get(key)
        if cached is not None or key in cache:
            return cached
        if origin == target:
            res = (0, 0)
        elif not (costs is not None and use_demo_roads) and origin_id in matrix_points and target_id in matrix_points:
            seconds, meters = planner.dist_engine.get_travel(origin_id, target_id)
            res = (round(seconds * factors.get(hour, 1.0)), meters)
        elif costs is not None:
            cost = costs[origin, target]
            if cost is None:
                res = None
            else:
                seconds, meters = cost
                res = (round(seconds * factors.get(hour, 1.0)), meters)
        else:
            distance = planner.dist_engine.calculate_distance_km(*origin, *target)
            seconds = planner.dist_engine.calculate_travel_time_sec(*origin, *target)
            res = (round(seconds * factors.get(hour, 1.0)), int(distance * 1000))
        cache[key] = res
        return res

    return travel


def _rebuild_route(route, visit_orders, staff, sc, options, order_windows, travel, work_types,
                   duration_forecasts, known_locations, fixed, joint_orders, planner):
    if route.status == "UNAVAILABLE" or route.unavailable_from:
        return None
    date_str = options["date_str"]
    now_dt = datetime.fromisoformat(options["now_iso"])
    now_min = parse_time_to_minutes(options["now_iso"])
    avail_map = options.get("available_from") or {}
    shift_start_min = parse_time_to_minutes(staff.shift.start)
    shift_end_min = parse_time_to_minutes(staff.shift.end)
    break_from = parse_time_to_minutes(staff.shift.break_window.from_)
    break_to = parse_time_to_minutes(staff.shift.break_window.to)
    break_dur = staff.shift.break_duration_min
    sc_id = sc.service_center_id
    sc_open = parse_time_to_minutes(sc.opening_hours.open)
    sc_close = parse_time_to_minutes(sc.opening_hours.close)
    restock_dur = sc.restock_duration_min if sc else 15

    last_locked_idx = 0
    for idx, stop in enumerate(route.stops):
        if stop.type == "DEPOT_START":
            last_locked_idx = idx
            continue
        if stop.type == "DEPOT_END":
            continue
        is_past = datetime.fromisoformat(stop.start) < now_dt
        is_manual_restock = stop.type == "RESTOCK" and stop.explanation == MANUAL_RESTOCK_EXPLANATION
        is_joint = bool(stop.order_id and stop.order_id in joint_orders)
        if is_past or is_manual_restock or is_joint:
            last_locked_idx = idx

    prefix = [stop.model_copy(deep=True) for stop in route.stops[:last_locked_idx + 1]]
    if not prefix:
        return None

    last_stop = prefix[-1]
    curr_loc = last_stop.location_id
    curr_lat, curr_lon = (sc.lat, sc.lon) if curr_loc == sc_id else known_locations.get(curr_loc, (sc.lat, sc.lon))
    curr_t = max(
        shift_start_min,
        now_min,
        parse_time_to_minutes(avail_map.get(staff.staff_id, "00:00")),
        parse_time_to_minutes(last_stop.end),
    )
    stock = dict(last_stop.equipment_after if last_stop.equipment_after is not None else staff.equipment_start)
    had_break = any(stop.type == "BREAK" and stop.status != "CANCELLED" for stop in prefix)
    total_travel_sec = sum(stop.travel_sec for stop in prefix if stop.status != "CANCELLED")
    total_dist_m = sum(stop.distance_m for stop in prefix if stop.status != "CANCELLED")
    total_work_sec = sum(
        (parse_time_to_minutes(stop.end) - parse_time_to_minutes(stop.start)) * 60
        for stop in prefix if stop.type == "VISIT" and stop.status != "CANCELLED"
    )
    visits_count = sum(stop.type == "VISIT" and stop.status != "CANCELLED" for stop in prefix)
    stops = prefix

    for order in visit_orders:
        wt = work_types.get(order.work_type_code)
        if not planner.is_staff_qualified(staff, order, wt)[0]:
            return None
        if fixed.get(order.order_id, staff.staff_id) != staff.staff_id:
            return None
        duration_min = duration_forecasts.get(order.order_id, wt.duration_min if wt else 60)
        slot_start_min = parse_time_to_minutes(order.slot.start)
        latest_start_min = parse_time_to_minutes(order_windows[order.order_id][1].isoformat())

        needs_restock = any(stock.get(eq_k, 0) < qty for eq_k, qty in order.required_equipment.items())
        if needs_restock:
            if any(staff.equipment_capacity.get(eq_k, 0) < qty for eq_k, qty in order.required_equipment.items()):
                return None
            leg_sc = travel(curr_loc, sc_id, curr_t // 60, (curr_lat, curr_lon), (sc.lat, sc.lon))
            if leg_sc is None:
                return None
            sc_sec, sc_dist = leg_sc
            arr_sc = curr_t + math.ceil(sc_sec / 60)
            if arr_sc < sc_open or arr_sc + restock_dur > min(sc_close, shift_end_min):
                return None
            dep_sc = arr_sc + restock_dur
            stock = dict(staff.equipment_capacity)
            stops.append(Stop(
                seq=len(stops) + 1,
                type="RESTOCK",
                location_id=sc_id,
                order_id=None,
                arrival=minutes_to_iso(date_str, arr_sc),
                start=minutes_to_iso(date_str, arr_sc),
                end=minutes_to_iso(date_str, dep_sc),
                travel_sec=sc_sec,
                distance_m=sc_dist,
                status="PLANNED",
                equipment_after=dict(stock),
                explanation=RESTOCK_EXPLANATION,
            ))
            total_travel_sec += sc_sec
            total_dist_m += sc_dist
            curr_t = dep_sc
            curr_loc, curr_lat, curr_lon = sc_id, sc.lat, sc.lon

        leg = travel(curr_loc, order.building_id, curr_t // 60, (curr_lat, curr_lon), (order.lat, order.lon))
        if leg is None:
            return None
        t_sec, d_m = leg
        arr_a = curr_t + math.ceil(t_sec / 60)
        start_a = max(arr_a, slot_start_min)
        end_a = start_a + duration_min

        option_a = None
        if start_a <= latest_start_min and end_a <= shift_end_min:
            if had_break or max(end_a, break_from) <= break_to:
                ready_a = end_a if had_break else max(end_a, break_from) + break_dur
                option_a = (ready_a, end_a, t_sec, False, arr_a, start_a, end_a, d_m)

        option_b = None
        if not had_break:
            br_start = max(curr_t, break_from)
            if br_start <= break_to:
                dep_b = br_start + break_dur
                if dep_b <= shift_end_min:
                    leg_b = travel(curr_loc, order.building_id, dep_b // 60, (curr_lat, curr_lon), (order.lat, order.lon))
                    if leg_b is not None:
                        sec_b, dist_b = leg_b
                        arr_b = dep_b + math.ceil(sec_b / 60)
                        start_b = max(arr_b, slot_start_min)
                        end_b = start_b + duration_min
                        if start_b <= latest_start_min and end_b <= shift_end_min:
                            option_b = (end_b, end_b, sec_b, True, arr_b, start_b, end_b, dist_b)

        candidates = [c for c in (option_a, option_b) if c is not None]
        if not candidates:
            return None
        _, _, vis_sec, use_break, vis_arr, vis_start, vis_end, vis_dist = min(candidates)

        if use_break:
            br_start = max(curr_t, break_from)
            br_end = br_start + break_dur
            stops.append(Stop(
                seq=len(stops) + 1,
                type="BREAK",
                location_id=curr_loc,
                order_id=None,
                arrival=minutes_to_iso(date_str, br_start),
                start=minutes_to_iso(date_str, br_start),
                end=minutes_to_iso(date_str, br_end),
                travel_sec=0,
                distance_m=0,
                status="PLANNED",
                equipment_after=dict(stock),
                explanation=BREAK_EXPLANATION,
            ))
            had_break = True

        for eq_k, qty in order.required_equipment.items():
            stock[eq_k] = max(0, stock.get(eq_k, 0) - qty)

        stops.append(Stop(
            seq=len(stops) + 1,
            type="VISIT",
            location_id=order.building_id,
            order_id=order.order_id,
            arrival=minutes_to_iso(date_str, vis_arr),
            start=minutes_to_iso(date_str, vis_start),
            end=minutes_to_iso(date_str, vis_end),
            travel_sec=vis_sec,
            distance_m=vis_dist,
            status="PLANNED",
            planned_duration_min=duration_min,
            equipment_after=dict(stock),
            explanation=VISIT_EXPLANATION.format(
                technology=order.technology,
                minutes=math.ceil(vis_sec / 60),
                start=minutes_to_iso(date_str, vis_start)[11:16],
                equipment=RESTOCKED if needs_restock else EQUIPMENT_AVAILABLE,
            ),
        ))
        curr_t = vis_end
        curr_loc, curr_lat, curr_lon = order.building_id, order.lat, order.lon
        total_travel_sec += vis_sec
        total_work_sec += duration_min * 60
        total_dist_m += vis_dist
        visits_count += 1

    if visits_count > 0 and not had_break:
        br_start = max(curr_t, break_from)
        if br_start > break_to:
            return None
        br_end = br_start + break_dur
        stops.append(Stop(
            seq=len(stops) + 1,
            type="BREAK",
            location_id=curr_loc,
            order_id=None,
            arrival=minutes_to_iso(date_str, br_start),
            start=minutes_to_iso(date_str, br_start),
            end=minutes_to_iso(date_str, br_end),
            travel_sec=0,
            distance_m=0,
            status="PLANNED",
            equipment_after=dict(stock),
            explanation=BREAK_EXPLANATION,
        ))
        curr_t = br_end

    back_leg = travel(curr_loc, sc_id, curr_t // 60, (curr_lat, curr_lon), (sc.lat, sc.lon))
    if back_leg is None:
        return None
    back_sec, back_dist = back_leg
    finish_arr = curr_t + math.ceil(back_sec / 60)
    if finish_arr > shift_end_min:
        return None

    final_end = max(finish_arr, shift_end_min)
    stops.append(Stop(
        seq=len(stops) + 1,
        type="DEPOT_END",
        location_id=sc_id,
        order_id=None,
        arrival=minutes_to_iso(date_str, finish_arr),
        start=minutes_to_iso(date_str, finish_arr),
        end=minutes_to_iso(date_str, final_end),
        travel_sec=back_sec,
        distance_m=back_dist,
        status="PLANNED",
        equipment_after=dict(stock),
        explanation=RETURN_EXPLANATION.format(center_name=sc.name),
    ))
    total_travel_sec += back_sec
    total_dist_m += back_dist

    return Route(
        staff_id=route.staff_id,
        service_center_id=route.service_center_id,
        status=route.status,
        unavailable_from=route.unavailable_from,
        unavailable_until=route.unavailable_until,
        stops=stops,
        totals=RouteTotals(
            visits_count=visits_count,
            travel_sec=total_travel_sec,
            work_sec=total_work_sec,
            distance_m=total_dist_m,
        ),
    )


def _refresh_summary(plan, unassigned_list):
    work_times = [r.totals.work_sec for r in plan.routes if r.totals and r.totals.work_sec > 0]
    util_cv = 0.0
    if len(work_times) > 1:
        mean_w = sum(work_times) / len(work_times)
        if mean_w > 0:
            var_w = sum((w - mean_w) ** 2 for w in work_times) / len(work_times)
            util_cv = round(math.sqrt(var_w) / mean_w, 3)

    summary = plan.summary.model_copy(update={
        "visits_planned": sum(r.totals.visits_count for r in plan.routes if r.totals),
        "unassigned": len(unassigned_list),
        "travel_sec_total": sum(r.totals.travel_sec for r in plan.routes if r.totals),
        "utilization_cv": util_cv,
    })
    return plan.model_copy(update={"summary": summary, "unassigned": unassigned_list})


def _make_unassigned(order):
    proposed = next_workday_slot(order)
    return UnassignedOrder(
        order_id=order.order_id,
        reason_code="SLOT_UNREACHABLE",
        reason_text=REJECTION_TEXT["SLOT_UNREACHABLE"],
        suggested_action="RESCHEDULE_TO_NEXT_DAY" if proposed else "MANUAL_REVIEW",
        proposed_slot=proposed,
    )


def _route_editable_visits(route, orders, now_dt, fixed, joint_orders):
    last_locked_idx = 0
    for idx, stop in enumerate(route.stops):
        if stop.type == "DEPOT_START":
            last_locked_idx = idx
        elif stop.type != "DEPOT_END":
            is_past = datetime.fromisoformat(stop.start) < now_dt
            is_manual_restock = stop.type == "RESTOCK" and stop.explanation == MANUAL_RESTOCK_EXPLANATION
            is_joint = bool(stop.order_id and stop.order_id in joint_orders)
            if is_past or is_manual_restock or is_joint:
                last_locked_idx = idx

    editable = []
    for stop in route.stops[last_locked_idx + 1:]:
        if stop.type == "VISIT" and stop.status != "CANCELLED" and stop.order_id in orders:
            editable.append(orders[stop.order_id])
    return editable


def _insertion_positions(route_visits, order, order_windows):
    o_start, o_latest = order_windows[order.order_id][0], order_windows[order.order_id][1]
    n = len(route_visits)
    positions = list(range(n + 1))

    def pos_rank(pos):
        penalty = 0
        if pos > 0:
            prev_start = order_windows[route_visits[pos - 1].order_id][0]
            if prev_start > o_latest:
                penalty += 1000
        if pos < n:
            next_latest = order_windows[route_visits[pos].order_id][1]
            if o_start > next_latest:
                penalty += 1000
        mid_delta = 0
        if pos > 0:
            mid_delta += abs((o_start - order_windows[route_visits[pos - 1].order_id][0]).total_seconds())
        if pos < n:
            mid_delta += abs((order_windows[route_visits[pos].order_id][0] - o_start).total_seconds())
        return (penalty, mid_delta, pos)

    positions.sort(key=pos_rank)
    return [pos for pos in positions if pos_rank(pos)[0] == 0] or positions[:3]


def improve_plan(planner, baseline, options, time_limit=SEARCH_SECONDS):
    orders = {order.order_id: order for order in options["orders"]}
    joint_orders = set()
    order_windows = {}
    for order_id, order in orders.items():
        if order_id.startswith(PARTNER_ORDER_PREFIX):
            joint_orders.add(order_id)
            joint_orders.add(order_id.removeprefix(PARTNER_ORDER_PREFIX))
            continue
        start = datetime.fromisoformat(order.slot.start)
        latest = latest_allowed_start(order.work_type_code, start, datetime.fromisoformat(order.latest_start))
        order_windows[order_id] = start, latest, datetime.fromisoformat(order.deadline), 1 + order.is_repeat
    existing_orders = set(options.get("preferred_assignments") or ()) & orders.keys()
    deadline = monotonic() + min(SEARCH_SECONDS, max(0, time_limit))

    def score(plan):
        visits = [
            stop for route in plan.routes for stop in route.stops if stop.type == "VISIT"
            and stop.status != "CANCELLED" and stop.order_id in order_windows
        ]
        completed_weight = 0
        in_slot_weight = 0
        for stop in visits:
            start, latest, order_deadline, weight = order_windows[stop.order_id]
            if start <= datetime.fromisoformat(stop.start) <= latest:
                in_slot_weight += weight
                if datetime.fromisoformat(stop.end) <= order_deadline:
                    completed_weight += weight
        distance = sum(route.totals.distance_m for route in plan.routes if route.totals)
        retained_orders = len(existing_orders & {stop.order_id for stop in visits})
        return (completed_weight, in_slot_weight, len(visits), retained_orders, -plan.summary.travel_sec_total,
                -plan.summary.utilization_cv, -distance)

    best = baseline
    best_score = score(best)
    if monotonic() >= deadline:
        return best

    fixed = options.get("assignments") or {}
    staff = {person.staff_id: person for person in options["staff_list"]}
    work_types = options["work_types"]
    service_centers = options["service_centers"]
    duration_forecasts = options.get("duration_forecasts") or {}
    known_locations = {o.building_id: (o.lat, o.lon) for o in (options.get("known_orders") or options["orders"])}
    candidate_seconds = (baseline.computation_ms or 0) / 1000

    if not fixed and not options.get("frozen_routes") and (deadline - monotonic()) > max(0.8, candidate_seconds * 1.5):
        def _dur(order):
            wt = work_types.get(order.work_type_code)
            return duration_forecasts.get(order.order_id, wt.duration_min if wt else 60)

        sort_strategies = [
            lambda o: (parse_time_to_minutes(o.slot.start), not o.is_repeat, parse_time_to_minutes(order_windows[o.order_id][1].isoformat()) if o.order_id in order_windows else parse_time_to_minutes(o.latest_start), _dur(o), o.order_id),
            lambda o: (parse_time_to_minutes(o.slot.start), _dur(o), parse_time_to_minutes(order_windows[o.order_id][1].isoformat()) if o.order_id in order_windows else parse_time_to_minutes(o.latest_start), not o.is_repeat, o.order_id),
            lambda o: (parse_time_to_minutes(o.deadline), not o.is_repeat, parse_time_to_minutes(o.slot.start), _dur(o), o.order_id),
            lambda o: (parse_time_to_minutes(order_windows[o.order_id][1].isoformat()) if o.order_id in order_windows else parse_time_to_minutes(o.latest_start), not o.is_repeat, _dur(o), o.order_id),
        ]
        portfolio_cap = min(deadline, monotonic() + max(1.0, (deadline - monotonic()) * 0.25))
        for priority_fn in sort_strategies:
            if monotonic() + candidate_seconds >= portfolio_cap:
                break
            t0 = monotonic()
            proposal = planner.plan_day(**{**options, "order_priority": priority_fn})
            candidate_seconds = max(candidate_seconds, monotonic() - t0)
            p_score = score(proposal)
            if p_score > best_score:
                best, best_score = proposal, p_score

    travel = _make_travel_fn(planner, options)
    now_dt = datetime.fromisoformat(options["now_iso"])
    work_seconds = {
        order_id: duration_forecasts.get(order_id, (work_types.get(order.work_type_code).duration_min if work_types.get(order.work_type_code) else 60)) * 60
        for order_id, order in orders.items()
    }

    def rebuild(route, visit_list):
        s_obj = staff.get(route.staff_id)
        sc_obj = service_centers.get(s_obj.service_center_id) if s_obj else None
        if s_obj is None or sc_obj is None:
            return None
        return _rebuild_route(
            route, visit_list, s_obj, sc_obj, options, order_windows, travel,
            work_types, duration_forecasts, known_locations, fixed, joint_orders, planner,
        )

    def apply_route_updates(plan, updated_routes_by_idx, remove_unassigned_ids=(), add_unassigned_orders=()):
        routes = list(plan.routes)
        for idx, new_route in updated_routes_by_idx.items():
            routes[idx] = new_route
        unassigned = [
            item for item in plan.unassigned if item.order_id not in remove_unassigned_ids
        ] + [_make_unassigned(o) for o in add_unassigned_orders]
        new_plan = plan.model_copy(update={"routes": routes})
        return _refresh_summary(new_plan, unassigned)

    def try_insert_order(route, visit_list, order):
        for pos in _insertion_positions(visit_list, order, order_windows):
            candidate_visits = visit_list[:pos] + [order] + visit_list[pos:]
            rebuilt = rebuild(route, candidate_visits)
            if rebuilt is not None:
                return rebuilt, candidate_visits
        return None, None

    def geo_dist(o, sc):
        return (o.lat - sc.lat) ** 2 + (o.lon - sc.lon) ** 2

    def qualified_route_indices(order, routes, editable_by_idx):
        wt = work_types.get(order.work_type_code)
        target_sc = order.district_id
        cands = []
        for idx, route in enumerate(routes):
            if route.status == "UNAVAILABLE" or route.unavailable_from:
                continue
            s_obj = staff.get(route.staff_id)
            if s_obj is None or not planner.is_staff_qualified(s_obj, order, wt)[0]:
                continue
            sc_obj = service_centers.get(s_obj.service_center_id)
            if sc_obj is None:
                continue
            cross = target_sc is not None and target_sc != sc_obj.service_center_id
            cands.append((cross, geo_dist(order, sc_obj), route.totals.work_sec if route.totals else 0, idx))
        cands.sort()
        return [item[3] for item in cands]

    local_deadline = min(deadline, monotonic() + max(0.3, (deadline - monotonic()) * 0.75))
    while monotonic() < local_deadline:
        improved_in_pass = False
        routes = list(best.routes)
        editable_by_idx = {
            idx: _route_editable_visits(route, orders, now_dt, fixed, joint_orders)
            for idx, route in enumerate(routes)
        }

        unassigned_orders = [
            orders[item.order_id] for item in best.unassigned
            if item.order_id in orders and item.order_id in order_windows and item.order_id not in joint_orders
        ]
        unassigned_orders.sort(key=lambda o: (
            not o.is_repeat,
            (order_windows[o.order_id][1] - order_windows[o.order_id][0]).total_seconds(),
            order_windows[o.order_id][2],
            -work_seconds[o.order_id],
        ))
        for order in unassigned_orders:
            if monotonic() >= local_deadline:
                break
            best_cand = None
            best_cand_score = best_score
            for idx in qualified_route_indices(order, routes, editable_by_idx):
                if monotonic() >= local_deadline:
                    break
                rebuilt, _ = try_insert_order(routes[idx], editable_by_idx[idx], order)
                if rebuilt is None:
                    continue
                cand_plan = apply_route_updates(best, {idx: rebuilt}, remove_unassigned_ids=(order.order_id,))
                cand_score = score(cand_plan)
                if cand_score > best_cand_score:
                    best_cand = cand_plan
                    best_cand_score = cand_score
                    if not order.is_repeat and best_cand_score[:4] > best_score[:4]:
                        break
            if best_cand is not None:
                best, best_score = best_cand, best_cand_score
                routes = list(best.routes)
                editable_by_idx = {
                    idx: _route_editable_visits(route, orders, now_dt, fixed, joint_orders)
                    for idx, route in enumerate(routes)
                }
                improved_in_pass = True

        if monotonic() < local_deadline:
            remaining_unassigned = [
                orders[item.order_id] for item in best.unassigned
                if item.order_id in orders and item.order_id in order_windows and item.order_id not in joint_orders
            ]
            remaining_unassigned.sort(key=lambda o: (
                not o.is_repeat,
                (order_windows[o.order_id][1] - order_windows[o.order_id][0]).total_seconds(),
                order_windows[o.order_id][2],
            ))
            for u_order in remaining_unassigned:
                if monotonic() >= local_deadline:
                    break
                rescued = False
                u_start, u_latest = order_windows[u_order.order_id][0], order_windows[u_order.order_id][1]
                for idx_a in qualified_route_indices(u_order, routes, editable_by_idx)[:10]:
                    if monotonic() >= local_deadline or rescued:
                        break
                    visits_a = editable_by_idx[idx_a]
                    blockers = [
                        (pos, v) for pos, v in enumerate(visits_a)
                        if v.order_id not in fixed and v.order_id not in joint_orders
                        and not (order_windows[v.order_id][2] < u_start or order_windows[v.order_id][0] > u_latest)
                    ]
                    if not blockers:
                        blockers = [(pos, v) for pos, v in enumerate(visits_a) if v.order_id not in fixed and v.order_id not in joint_orders]
                    for pos_v, v_order in blockers:
                        if monotonic() >= local_deadline or rescued:
                            break
                        reduced_a = visits_a[:pos_v] + visits_a[pos_v + 1:]
                        rebuilt_a, _ = try_insert_order(routes[idx_a], reduced_a, u_order)
                        if rebuilt_a is None:
                            continue
                        for idx_b in qualified_route_indices(v_order, routes, editable_by_idx)[:10]:
                            if idx_b == idx_a:
                                continue
                            rebuilt_b, new_visits_b = try_insert_order(routes[idx_b], editable_by_idx[idx_b], v_order)
                            if rebuilt_b is not None:
                                cand_plan = apply_route_updates(
                                    best,
                                    {idx_a: rebuilt_a, idx_b: rebuilt_b},
                                    remove_unassigned_ids=(u_order.order_id,),
                                )
                                cand_score = score(cand_plan)
                                if cand_score > best_score:
                                    best, best_score = cand_plan, cand_score
                                    routes = list(best.routes)
                                    editable_by_idx[idx_a] = _route_editable_visits(rebuilt_a, orders, now_dt, fixed, joint_orders)
                                    editable_by_idx[idx_b] = new_visits_b
                                    improved_in_pass = True
                                    rescued = True
                                    break
                        if not rescued and (1 + u_order.is_repeat) >= (1 + v_order.is_repeat):
                            cand_plan = apply_route_updates(
                                best,
                                {idx_a: rebuilt_a},
                                remove_unassigned_ids=(u_order.order_id,),
                                add_unassigned_orders=(v_order,),
                            )
                            cand_score = score(cand_plan)
                            if cand_score > best_score:
                                best, best_score = cand_plan, cand_score
                                routes = list(best.routes)
                                editable_by_idx[idx_a] = _route_editable_visits(rebuilt_a, orders, now_dt, fixed, joint_orders)
                                improved_in_pass = True
                                rescued = True
                                break

        for idx, route in enumerate(routes):
            if monotonic() >= local_deadline:
                break
            visits = editable_by_idx[idx]
            if len(visits) < 2:
                continue
            cur_route = route
            for i in range(len(visits)):
                if monotonic() >= local_deadline:
                    break
                for j in range(i + 1, min(len(visits), i + 4)):
                    swapped = list(visits)
                    swapped[i], swapped[j] = swapped[j], swapped[i]
                    rebuilt = rebuild(cur_route, swapped)
                    if rebuilt is None:
                        continue
                    cand_plan = apply_route_updates(best, {idx: rebuilt})
                    cand_score = score(cand_plan)
                    if cand_score > best_score:
                        best, best_score = cand_plan, cand_score
                        routes = list(best.routes)
                        cur_route = rebuilt
                        visits = swapped
                        editable_by_idx[idx] = swapped
                        improved_in_pass = True

        if monotonic() < local_deadline:
            route_work = [(idx, r.totals.travel_sec if r.totals else 0) for idx, r in enumerate(routes)]
            route_work.sort(key=lambda item: -item[1])
            for idx_a, _ in route_work:
                if monotonic() >= local_deadline:
                    break
                visits_a = editable_by_idx[idx_a]
                if not visits_a:
                    continue
                stop_travel = {
                    s.order_id: s.travel_sec for s in routes[idx_a].stops
                    if s.type == "VISIT" and s.order_id
                }
                candidates_a = sorted(
                    enumerate(visits_a),
                    key=lambda pair: -(stop_travel.get(pair[1].order_id, 0) + (2000 if pair[1].district_id and pair[1].district_id != routes[idx_a].service_center_id else 0)),
                )
                for pos_a, v_order in candidates_a[:8]:
                    if monotonic() >= local_deadline:
                        break
                    if v_order.order_id in fixed or v_order.order_id in joint_orders:
                        continue
                    reduced_a = visits_a[:pos_a] + visits_a[pos_a + 1:]
                    rebuilt_a = rebuild(routes[idx_a], reduced_a)
                    if rebuilt_a is None:
                        continue
                    best_reloc = None
                    best_reloc_score = best_score
                    best_idx_b = None
                    best_visits_b = None
                    for idx_b in qualified_route_indices(v_order, routes, editable_by_idx)[:8]:
                        if idx_b == idx_a:
                            continue
                        rebuilt_b, new_visits_b = try_insert_order(routes[idx_b], editable_by_idx[idx_b], v_order)
                        if rebuilt_b is None:
                            continue
                        cand_plan = apply_route_updates(best, {idx_a: rebuilt_a, idx_b: rebuilt_b})
                        cand_score = score(cand_plan)
                        if cand_score > best_reloc_score:
                            best_reloc = cand_plan
                            best_reloc_score = cand_score
                            best_idx_b = idx_b
                            best_visits_b = new_visits_b
                    if best_reloc is not None:
                        best, best_score = best_reloc, best_reloc_score
                        routes = list(best.routes)
                        editable_by_idx[idx_a] = reduced_a
                        editable_by_idx[best_idx_b] = best_visits_b
                        visits_a = reduced_a
                        improved_in_pass = True
                        break

        if monotonic() < local_deadline:
            for idx_a, route_a in enumerate(routes):
                if monotonic() >= local_deadline:
                    break
                visits_a = editable_by_idx[idx_a]
                if not visits_a:
                    continue
                for pos_a, order_a in enumerate(visits_a):
                    if monotonic() >= local_deadline:
                        break
                    if order_a.order_id in fixed or order_a.order_id in joint_orders:
                        continue
                    swapped_done = False
                    for idx_b in qualified_route_indices(order_a, routes, editable_by_idx)[:6]:
                        if idx_b <= idx_a or monotonic() >= local_deadline:
                            continue
                        visits_b = editable_by_idx[idx_b]
                        for pos_b, order_b in enumerate(visits_b):
                            if order_b.order_id in fixed or order_b.order_id in joint_orders:
                                continue
                            if abs((order_windows[order_a.order_id][0] - order_windows[order_b.order_id][0]).total_seconds()) > 7200:
                                continue
                            if not planner.is_staff_qualified(staff[route_a.staff_id], order_b, work_types.get(order_b.work_type_code))[0]:
                                continue
                            new_a = visits_a[:pos_a] + [order_b] + visits_a[pos_a + 1:]
                            new_b = visits_b[:pos_b] + [order_a] + visits_b[pos_b + 1:]
                            rebuilt_a = rebuild(route_a, new_a)
                            if rebuilt_a is None:
                                continue
                            rebuilt_b = rebuild(routes[idx_b], new_b)
                            if rebuilt_b is None:
                                continue
                            cand_plan = apply_route_updates(best, {idx_a: rebuilt_a, idx_b: rebuilt_b})
                            cand_score = score(cand_plan)
                            if cand_score > best_score:
                                best, best_score = cand_plan, cand_score
                                routes = list(best.routes)
                                editable_by_idx[idx_a] = new_a
                                editable_by_idx[idx_b] = new_b
                                improved_in_pass = True
                                swapped_done = True
                                break
                        if swapped_done:
                            break
                    if swapped_done:
                        break

        if not improved_in_pass:
            break

    seen = set()
    while monotonic() + candidate_seconds < deadline:
        starting_score = best_score
        for moves in candidate_moves(planner, best, orders, staff, options["work_types"], fixed):
            if monotonic() + candidate_seconds >= deadline:
                return best
            key = tuple(sorted(moves.items()))
            if key in seen:
                continue
            seen.add(key)
            assignments = {**fixed, **moves}
            candidate_started = monotonic()
            proposal = planner.plan_day(**{**options, "assignments": assignments})
            candidate_seconds = max(candidate_seconds, monotonic() - candidate_started)
            placed = {
                stop.order_id: candidate.staff_id for candidate in proposal.routes
                for stop in candidate.stops if stop.type == "VISIT" and stop.order_id in moves
            }
            proposal_score = score(proposal)
            if placed == moves and proposal_score > best_score:
                best, best_score = proposal, proposal_score
        if best_score == starting_score:
            break
    return best


def candidate_moves(planner, baseline, orders, staff, work_types, fixed):
    routes = [route for route in baseline.routes if route.stops]
    movable = {
        route.staff_id: sorted(
            (stop for stop in route.stops if stop.type == "VISIT" and stop.order_id in orders
             and stop.order_id not in fixed),
            key=lambda stop: -stop.travel_sec,
        )
        for route in routes
    }

    def qualified(staff_id, order_id):
        order = orders[order_id]
        return planner.is_staff_qualified(staff[staff_id], order, work_types.get(order.work_type_code))[0]

    for route_index, route in enumerate(routes):
        neighbors = routes[route_index + 1:] + routes[:route_index]
        neighbors.sort(key=lambda candidate: candidate.service_center_id != route.service_center_id)
        for visit in movable[route.staff_id]:
            for neighbor in neighbors:
                if not qualified(neighbor.staff_id, visit.order_id):
                    continue
                move = {visit.order_id: neighbor.staff_id}
                yield move
                matching = (stop for stop in movable[neighbor.staff_id] if qualified(route.staff_id, stop.order_id))
                exchange = min(matching, key=lambda stop: abs(
                    datetime.fromisoformat(stop.start) - datetime.fromisoformat(visit.start)
                ), default=None)
                if exchange:
                    yield {**move, exchange.order_id: route.staff_id}
