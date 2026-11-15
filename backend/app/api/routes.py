import copy
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse

from ..constants import RU_TEXT_0003, RU_TEXT_0004, RU_TEXT_0005, RU_TEXT_0006, RU_TEXT_0007, RU_TEXT_0008, RU_TEXT_0009, RU_TEXT_0010, RU_TEXT_0011, RU_TEXT_0012, RU_TEXT_0013, RU_TEXT_0014, RU_TEXT_0015, RU_TEXT_0016, RU_TEXT_0017, RU_TEXT_0020, RU_TEXT_0021, RU_TEXT_0022, RU_TEXT_0023
from .mobile_visits import recorded_calls
from ..core.distance import distance_engine
from ..core.next_workday import next_workday_slot
from ..core.osrm_client import osrm_client
from ..core.pgp_detector import pgp_detector
from ..core.reactive import replan_event
from ..core.route_sheet import build_route_sheet
from ..core.solver import MANUAL_RESTOCK_EXPLANATION, official_shift_planner
from ..models.official_spec import (
    BreakWindow,
    EventResult,
    Order,
    Plan,
    ReferenceData,
    Shift,
    Staff,
    UnassignedOrder,
)
from .dispatch import (
    find_route_for_order,
    get_active_dispatch_context,
    prepare_custom_order,
    preview_plan_change,
    save_reversible_change,
)
from .orchestrator import (
    add_custom_order as orchestrator_add_custom_order,
    address_district,
    build_plan,
    congestion_profiles,
    db,
    get_current_plan,
    handle_day_event,
    load_default_official_data,
    plan_history,
    visit_end_overrides,
)

router = APIRouter(prefix="/api/v1")


def _format_ru_date(iso_date: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_date[:10])
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return iso_date


def _next_workday_date(iso_date: str) -> str:
    dt = datetime.fromisoformat(iso_date[:10]) + timedelta(days=1)
    while dt.weekday() >= 5:
        dt += timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def _nearest_sc_id(order: Order, ref: Optional[ReferenceData]) -> str:
    sc_id = address_district(order)
    if sc_id:
        return sc_id
    if ref and ref.service_centers:
        nearest = min(
            ref.service_centers,
            key=lambda sc: (order.lat - sc.lat) ** 2 + (order.lon - sc.lon) ** 2,
        )
        return nearest.service_center_id
    return "SC-01"


def _top_backlog_service_centers(plan: Plan, day_data, ref: ReferenceData, count: int = 2) -> List[str]:
    orders_map = {o.order_id: o for o in day_data.orders}
    sc_backlog: Dict[str, int] = {sc.service_center_id: 0 for sc in ref.service_centers}
    for item in plan.unassigned:
        order = orders_map.get(item.order_id)
        if order:
            sc_id = _nearest_sc_id(order, ref)
            sc_backlog[sc_id] = sc_backlog.get(sc_id, 0) + 1
    ranked = sorted(sc_backlog.items(), key=lambda pair: -pair[1])
    sc_ids = [sc_id for sc_id, _ in ranked if sc_id]
    if not sc_ids and ref.service_centers:
        sc_ids = [ref.service_centers[0].service_center_id]
    while len(sc_ids) < count:
        sc_ids.append(sc_ids[0])
    return sc_ids[:count]


def _create_reserve_crews(plan: Plan, day_data, ref: ReferenceData, count: int = 2) -> List[Staff]:
    existing_ids = {s.staff_id for s in ref.staff}
    target_scs = _top_backlog_service_centers(plan, day_data, ref, count=count)

    all_profiles = sorted({p for s in ref.staff for p in s.profiles}) or ["UNIVERSAL"]
    all_techs = sorted({t for s in ref.staff for t in s.technologies}) or ["PON", "FTTB"]
    max_cap: Dict[str, int] = {}
    for s in ref.staff:
        for k, v in s.equipment_capacity.items():
            max_cap[k] = max(max_cap.get(k, 0), v)
    if not max_cap:
        max_cap = {"ONT": 6, "ROUTER": 6, "STB": 4}

    created: List[Staff] = []
    suffix = 1
    for idx in range(count):
        while f"ST-RES-{suffix}" in existing_ids:
            suffix += 1
        staff_id = f"ST-RES-{suffix}"
        existing_ids.add(staff_id)
        sc_id = target_scs[idx]
        template = next((s for s in ref.staff if s.service_center_id == sc_id), ref.staff[0] if ref.staff else None)
        shift_obj = (
            Shift(
                start=template.shift.start,
                end="21:00" if template.shift.end < "21:00" else template.shift.end,
                break_duration_min=template.shift.break_duration_min,
                break_window=BreakWindow(
                    from_=template.shift.break_window.from_,
                    to=template.shift.break_window.to,
                ),
            )
            if template
            else Shift(
                start="10:00",
                end="21:00",
                break_duration_min=60,
                break_window=BreakWindow(from_="14:00", to="16:00"),
            )
        )
        created.append(
            Staff(
                staff_id=staff_id,
                display_name=f"Дежурная бригада №{suffix} ({sc_id})",
                service_center_id=sc_id,
                profiles=list(all_profiles),
                technologies=list(all_techs),
                height_permit=True,
                shift=shift_obj,
                equipment_start=dict(max_cap),
                equipment_capacity=dict(max_cap),
                experience_years=5.0,
            )
        )
    return created


def _make_event_result(
    event_id: str,
    plan: Plan,
    changed_staff_ids: List[str],
    processing_ms: int,
    message: str,
    new_alerts=None,
    reassigned_orders=None,
    undo_token: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    body: Dict[str, Any] = {
        "event_id": event_id,
        "accepted": True,
        "duplicate": False,
        "status": "APPLIED",
        "plan_version": plan.plan_version,
        "as_of": plan.as_of,
        "processing_ms": processing_ms,
        "changed_staff_ids": changed_staff_ids,
        "reassigned_orders": reassigned_orders or [],
        "new_unassigned": [u.order_id for u in plan.unassigned],
        "new_alerts": [a.model_dump() for a in (new_alerts or [])],
        "message": message,
    }
    if undo_token:
        body["undo_token"] = undo_token
    if extra:
        body.update(extra)
    return JSONResponse(content=body)


@router.get("/context")
def get_context():
    active_date = db.get("active_date", "2026-10-26")
    ref = db.get("reference")
    day_data = db["days"].get(active_date)

    staff_dict = {}
    sc_dict = {}
    wt_dict = {}
    if ref:
        for s in ref.staff:
            staff_dict[s.staff_id] = {
                "staff_id": s.staff_id,
                "display_name": s.display_name,
                "service_center_id": s.service_center_id,
                "profiles": s.profiles,
                "technologies": s.technologies,
                "height_permit": s.height_permit,
                "shift": s.shift.model_dump(),
                "equipment_start": s.equipment_start,
                "equipment_capacity": s.equipment_capacity,
            }
        for sc in ref.service_centers:
            sc_dict[sc.service_center_id] = {
                "service_center_id": sc.service_center_id,
                "name": sc.name,
                "lat": sc.lat,
                "lon": sc.lon,
            }
        for wt in ref.work_types:
            wt_dict[wt.code] = {
                "code": wt.code,
                "name": wt.name,
                "duration_min": wt.duration_min,
                "profiles": wt.profiles,
            }

    orders_dict = {}
    if day_data:
        for o in day_data.orders:
            orders_dict[o.order_id] = {
                "order_id": o.order_id,
                "service_center_id": _nearest_sc_id(o, ref),
                "work_type_code": o.work_type_code,
                "technology": o.technology,
                "address": o.address,
                "lat": o.lat,
                "lon": o.lon,
                "building_type": o.building_type,
                "requires_height": o.requires_height,
                "slot": o.slot.model_dump(),
                "latest_start": o.latest_start,
                "required_equipment": o.required_equipment,
                "is_repeat": o.is_repeat,
                "client_name": o.client_name,
                "client_phone": o.client_phone,
                "entrance": o.entrance,
                "floor": o.floor,
                "district_id": o.district_id,
            }

    next_workday = _next_workday_date(active_date)
    return {
        "active_date": active_date,
        "next_workday": next_workday,
        "next_workday_ru": _format_ru_date(next_workday),
        "staff": staff_dict,
        "service_centers": sc_dict,
        "work_types": wt_dict,
        "orders": orders_dict,
    }


@router.post("/scenario/simulate")
def simulate_scenario(payload: Dict[str, Any] = Body(...)):
    active_date = db.get("active_date", "2026-10-26")
    base_plan = get_current_plan()
    day_data = db["days"].get(active_date)
    ref = db.get("reference")

    if not day_data or not ref:
        raise HTTPException(status_code=400, detail=RU_TEXT_0006)

    scenario_type = payload.get("scenario_type", "staff_minus_3")
    apply_to_shift = bool(payload.get("apply_to_shift", False))

    scenario_staff = copy.deepcopy(ref.staff)
    scenario_absent = list(day_data.absent_staff_ids or [])
    excluded_ids: List[str] = []

    if scenario_type == "staff_minus_3":
        requested_ids = payload.get("staff_ids")
        if isinstance(requested_ids, list) and len(requested_ids) > 0:
            valid_active = {r.staff_id for r in base_plan.routes if r.status == "ACTIVE"}
            excluded_ids = [sid for sid in requested_ids if sid in valid_active][:3]
        if not excluded_ids:
            active_routes = sorted(
                (r for r in base_plan.routes if r.status == "ACTIVE"),
                key=lambda r: (-(r.totals.visits_count if r.totals else 0), r.staff_id),
            )
            excluded_ids = [r.staff_id for r in active_routes[:3]]
        scenario_absent = list(dict.fromkeys(scenario_absent + excluded_ids))
        scenario_staff = [s for s in scenario_staff if s.staff_id not in set(excluded_ids)]
    elif scenario_type == "backpack_opt":
        target_staff_id = payload.get("staff_id")
        eq_override = payload.get("equipment") if isinstance(payload.get("equipment"), dict) else None
        for s in scenario_staff:
            if target_staff_id and s.staff_id != target_staff_id:
                continue
            if eq_override:
                for k, v in eq_override.items():
                    val = max(0, int(v))
                    s.equipment_start[k] = val
                    s.equipment_capacity[k] = max(s.equipment_capacity.get(k, 0), val)
            else:
                s.equipment_start["ONT"] = s.equipment_start.get("ONT", 0) + 3
                s.equipment_start["ROUTER"] = s.equipment_start.get("ROUTER", 0) + 3
                s.equipment_capacity["ONT"] = max(s.equipment_capacity.get("ONT", 0), s.equipment_start["ONT"])
                s.equipment_capacity["ROUTER"] = max(s.equipment_capacity.get("ROUTER", 0), s.equipment_start["ROUTER"])
    elif scenario_type == "duty_plus_2":
        reserve_crews = _create_reserve_crews(base_plan, day_data, ref, count=2)
        scenario_staff.extend(reserve_crews)

    sc_dict = {sc.service_center_id: sc for sc in ref.service_centers}
    wt_dict = {wt.code: wt for wt in ref.work_types}

    dispatchable_orders, pgp_groups = pgp_detector.detect_and_filter(
        orders=day_data.orders,
        as_of=day_data.now,
        work_types=ref.work_types,
    )

    sim_plan = official_shift_planner.plan_day(
        date_str=active_date,
        now_iso=day_data.now,
        staff_list=scenario_staff,
        orders=dispatchable_orders,
        service_centers=sc_dict,
        work_types=wt_dict,
        absent_staff_ids=scenario_absent,
        pgp_groups=pgp_groups,
        plan_version=base_plan.plan_version + 1,
        congestion_factors=congestion_profiles.get(active_date),
    )

    def _stats(pl: Plan):
        planned = pl.summary.visits_planned if pl.summary else sum(
            r.totals.visits_count for r in pl.routes if r.totals
        )
        unassigned = pl.summary.unassigned if pl.summary else len(pl.unassigned)
        travel_min = round((pl.summary.travel_sec_total if pl.summary else 0) / 60)
        restocks = sum(1 for r in pl.routes for st in r.stops if st.type == "RESTOCK" and st.status != "CANCELLED")
        pgp_cnt = pl.summary.pgp_orders if pl.summary else 0
        denom = max(1, len(day_data.orders) - pgp_cnt)
        sla = round((planned / denom) * 100, 1) if day_data.orders else 0.0
        return planned, unassigned, travel_min, restocks, sla

    base_planned, base_unassigned, base_travel_min, base_restocks, base_sla = _stats(base_plan)
    sim_planned, sim_unassigned, sim_travel_min, sim_restocks, sim_sla = _stats(sim_plan)

    delta_planned = sim_planned - base_planned
    delta_unassigned = sim_unassigned - base_unassigned
    delta_sla = round(sim_sla - base_sla, 1)
    delta_restocks = sim_restocks - base_restocks
    delta_travel = sim_travel_min - base_travel_min

    if scenario_type == "staff_minus_3":
        insight_text = (
            f"Невыход {len(excluded_ids)} специалистов ({', '.join(excluded_ids)}): "
            f"очередь переноса меняется на {delta_unassigned:+d} нарядов, суточный SLA на {delta_sla:+.1f}% "
            f"(выполнено {sim_planned} из {len(day_data.orders)} заявок)."
        )
    elif scenario_type == "backpack_opt":
        insight_text = (
            f"Оптимизация стартового рюкзака: дозаездов на склад {delta_restocks:+d} "
            f"(было {base_restocks}, станет {sim_restocks}), время в пути {delta_travel:+d} мин, "
            f"выполнено {delta_planned:+d} нарядов (SLA {delta_sla:+.1f}%)."
        )
    elif scenario_type == "duty_plus_2":
        insight_text = (
            f"Вывод 2 дежурных бригад ({', '.join(s.staff_id for s in scenario_staff[-2:])}): "
            f"дополнительно распределено {delta_planned:+d} нарядов, "
            f"SLA изменяется на {delta_sla:+.1f}% (с {base_sla}% до {sim_sla}%)."
        )
    else:
        insight_text = f"Сценарный расчёт завершён: изменение SLA {delta_sla:+.1f}%."

    undo_token = None
    if apply_to_shift:
        new_ref = ref.model_copy(update={"staff": scenario_staff})
        new_day = day_data.model_copy(update={"absent_staff_ids": scenario_absent})
        undo_res = save_reversible_change(
            base_plan,
            (sim_plan, new_day, {"type": "UI_SCENARIO", "event_id": uuid4().hex, "timestamp": base_plan.as_of}),
            reference=new_ref,
        )
        undo_token = undo_res["undo_token"]

    active_base_cnt = sum(1 for r in base_plan.routes if r.status == "ACTIVE")
    active_sim_cnt = sum(1 for r in sim_plan.routes if r.status == "ACTIVE")

    return {
        "status": "success",
        "scenario_type": scenario_type,
        "applied": apply_to_shift,
        "undo_token": undo_token,
        "excluded_staff_ids": excluded_ids,
        "insight": insight_text,
        "summary": insight_text,
        "comparison": {
            "staff_count": {"base": active_base_cnt, "scenario": active_sim_cnt, "delta": active_sim_cnt - active_base_cnt},
            "planned_orders": {"base": base_planned, "scenario": sim_planned, "delta": delta_planned},
            "unassigned_orders": {"base": base_unassigned, "scenario": sim_unassigned, "delta": delta_unassigned},
            "sla_percent": {"base": base_sla, "scenario": sim_sla, "delta": delta_sla},
            "restocks_count": {"base": base_restocks, "scenario": sim_restocks, "delta": delta_restocks},
            "travel_min_total": {"base": base_travel_min, "scenario": sim_travel_min, "delta": delta_travel},
        },
    }


@router.post("/events")
def handle_current_event(event_payload: Dict[str, Any] = Body(...)):
    active_date = db.get("active_date", "2026-10-26")
    plan = get_current_plan()
    day = db["days"].get(active_date)
    ref = db.get("reference")
    if not day or not ref:
        raise HTTPException(status_code=400, detail=RU_TEXT_0006)

    ev_type = event_payload.get("type")
    ev_id = event_payload.get("event_id") or uuid4().hex
    raw_ts = event_payload.get("timestamp") or plan.as_of
    ts = raw_ts
    try:
        if datetime.fromisoformat(raw_ts) < datetime.fromisoformat(plan.as_of):
            ts = plan.as_of
    except Exception:
        ts = plan.as_of
    event_payload = {**event_payload, "event_id": ev_id, "timestamp": ts}
    started_at = time.perf_counter()

    if ev_type == "ADD_DUTY_CREW":
        created_crews = _create_reserve_crews(plan, day, ref, count=2)
        created_ids = [c.staff_id for c in created_crews]
        updated_ref = ref.model_copy(update={"staff": [*ref.staff, *created_crews]})
        ev = {"event_id": ev_id, "type": "ADD_DUTY_CREW", "timestamp": ts}
        updated, updated_day, _ = replan_event(
            plan,
            day,
            updated_ref,
            ev,
            congestion_factors=congestion_profiles.get(plan.date),
            preserve_assignments=True,
            search_seconds=0,
        )
        before_planned = plan.summary.visits_planned if plan.summary else 0
        after_planned = updated.summary.visits_planned if updated.summary else 0
        assigned_delta = max(0, after_planned - before_planned)
        undo_res = save_reversible_change(plan, (updated, updated_day, ev), reference=updated_ref)
        proc_ms = max(1, round((time.perf_counter() - started_at) * 1000))
        msg = (
            f"Выведены 2 дежурные бригады ({', '.join(created_ids)}): "
            f"распределено +{assigned_delta} нарядов (в очереди осталось: {len(updated.unassigned)})"
        )
        return _make_event_result(
            ev_id,
            updated,
            created_ids,
            proc_ms,
            msg,
            undo_token=undo_res["undo_token"],
            extra={"assigned_count": assigned_delta, "created_staff_ids": created_ids},
        )

    if ev_type == "RESCHEDULE_TO_NEXT_DAY":
        order_id = event_payload.get("order_id")
        if not order_id:
            raise HTTPException(status_code=422, detail=RU_TEXT_0013)
        order = next((o for o in day.orders if o.order_id == order_id), None)
        if not order:
            raise HTTPException(status_code=404, detail=f"Наряд {order_id} не найден")

        proposed = next_workday_slot(order)
        next_date_iso = (proposed["start"][:10] if proposed else _next_workday_date(plan.date))
        next_date_ru = _format_ru_date(next_date_iso)
        deferred = list(dict.fromkeys([*day.deferred_order_ids, order_id]))
        updated_day = day.model_copy(update={"deferred_order_ids": deferred})
        ev = {"event_id": ev_id, "type": "RESCHEDULE_TO_NEXT_DAY", "timestamp": ts, "order_id": order_id}
        existing_route = find_route_for_order(plan, order_id)
        if existing_route is None:
            updated = plan.model_copy(deep=True)
            updated.plan_version += 1
            updated.based_on_event_id = ev_id
            found_unassigned = False
            for u in updated.unassigned:
                if u.order_id == order_id:
                    u.suggested_action = "CONFIRMED_FOR_TOMORROW"
                    if proposed:
                        u.proposed_slot = proposed
                    found_unassigned = True
                    break
            if not found_unassigned:
                from ..models.official_spec import UnassignedOrder
                updated.unassigned.append(UnassignedOrder(
                    order_id=order_id,
                    reason_code="OTHER",
                    reason_text=RU_TEXT_0021,
                    suggested_action="CONFIRMED_FOR_TOMORROW",
                    proposed_slot=proposed,
                ))
                if updated.summary:
                    updated.summary.unassigned = len(updated.unassigned)
        else:
            prev_item = next((u for u in plan.unassigned if u.order_id == order_id), None)
            replan_ev = {"event_id": ev_id, "type": "ORDER_DEFERRED", "timestamp": ts, "order_id": order_id}
            updated, updated_day, _ = replan_event(
                plan,
                updated_day,
                ref,
                replan_ev,
                congestion_factors=congestion_profiles.get(plan.date),
                search_seconds=0,
            )
            for u in updated.unassigned:
                if u.order_id == order_id:
                    u.reason_text = prev_item.reason_text if prev_item else RU_TEXT_0021
                    u.reason_code = prev_item.reason_code if prev_item else "OTHER"
                    u.suggested_action = "CONFIRMED_FOR_TOMORROW"
                    if proposed:
                        u.proposed_slot = proposed
        undo_res = save_reversible_change(plan, (updated, updated_day, ev))
        proc_ms = max(1, round((time.perf_counter() - started_at) * 1000))
        return _make_event_result(
            ev_id,
            updated,
            [],
            proc_ms,
            f"Наряд {order_id} перенесён на следующий рабочий день ({next_date_ru})",
            undo_token=undo_res["undo_token"],
            extra={"next_workday": next_date_iso, "next_workday_ru": next_date_ru},
        )

    if ev_type == "RESCHEDULE_ALL_BACKLOG":
        backlog_ids = [u.order_id for u in plan.unassigned]
        next_date_iso = _next_workday_date(plan.date)
        next_date_ru = _format_ru_date(next_date_iso)
        if not backlog_ids:
            return _make_event_result(ev_id, plan, [], 1, RU_TEXT_0007)
        orders_by_id = {o.order_id: o for o in day.orders}
        updated = plan.model_copy(deep=True)
        updated.plan_version += 1
        updated.based_on_event_id = ev_id
        for u in updated.unassigned:
            o = orders_by_id.get(u.order_id)
            proposed = next_workday_slot(o) if o else None
            if proposed:
                next_date_iso = proposed["start"][:10]
                next_date_ru = _format_ru_date(next_date_iso)
                u.proposed_slot = proposed
            u.suggested_action = "CONFIRMED_FOR_TOMORROW"
        deferred = list(dict.fromkeys([*day.deferred_order_ids, *backlog_ids]))
        updated_day = day.model_copy(update={"deferred_order_ids": deferred})
        ev = {"event_id": ev_id, "type": "BULK_RESCHEDULE", "timestamp": ts}
        undo_res = save_reversible_change(plan, (updated, updated_day, ev))
        proc_ms = max(1, round((time.perf_counter() - started_at) * 1000))
        return _make_event_result(
            ev_id,
            updated,
            [],
            proc_ms,
            f"Согласован перенос {len(backlog_ids)} нарядов на {next_date_ru}",
            undo_token=undo_res["undo_token"],
            extra={"next_workday": next_date_iso, "next_workday_ru": next_date_ru, "rescheduled_count": len(backlog_ids)},
        )

    if ev_type == "RESOLVE_ALERT":
        alert_id = event_payload.get("alert_id")
        updated = plan.model_copy(deep=True)
        for alert in updated.alerts:
            if alert.alert_id == alert_id:
                alert.status = "RESOLVED"
                alert.resolved_at = ts
                alert.applied = True
        updated.plan_version += 1
        db["plans"][plan.date] = updated
        return _make_event_result(ev_id, updated, [], 1, f"Предупреждение {alert_id} отмечено как обработанное.")

    if ev_type == "ORDER_TRANSFERRED" and "to_staff_id" in event_payload:
        order_id = event_payload.get("order_id")
        to_staff_id = event_payload.get("to_staff_id")
        if not order_id or not to_staff_id:
            raise HTTPException(status_code=422, detail=RU_TEXT_0014)

        existing_unassigned = next((u for u in plan.unassigned if u.order_id == order_id), None)
        if existing_unassigned and existing_unassigned.suggested_action == "CONFIRMED_FOR_TOMORROW":
            raise HTTPException(status_code=409, detail=RU_TEXT_0023)

        was_unassigned = find_route_for_order(plan, order_id) is None
        prev_unassigned_ids = {u.order_id for u in plan.unassigned}
        cleaned_deferred = [x for x in day.deferred_order_ids if x != order_id]
        base_day = day.model_copy(update={"deferred_order_ids": cleaned_deferred}) if cleaned_deferred != day.deferred_order_ids else day

        def _try_assign(target_day: Any, target_staff: str) -> Tuple[Optional[Plan], Any]:
            attempt_ev = {"event_id": ev_id, "type": "UI_REASSIGN", "timestamp": ts, "staff_id": target_staff, "order_id": order_id}
            cand_plan, cand_day, _ = replan_event(
                plan,
                target_day,
                ref,
                attempt_ev,
                assignments={order_id: target_staff},
                congestion_factors=congestion_profiles.get(plan.date),
                search_seconds=0,
            )
            rt = find_route_for_order(cand_plan, order_id)
            if rt is None or rt.staff_id != target_staff:
                return None, None
            if was_unassigned:
                bumped = {u.order_id for u in cand_plan.unassigned} - prev_unassigned_ids
                if bumped:
                    return None, None
            return cand_plan, cand_day

        updated, updated_day = _try_assign(base_day, to_staff_id)
        chosen_staff_id = to_staff_id

        if updated is None and was_unassigned:
            target_order = next((o for o in base_day.orders if o.order_id == order_id), None)
            if target_order is not None:
                tz_suffix = plan.as_of[19:] if len(plan.as_of) > 19 else "+05:00"
                widened_slot = target_order.slot.model_copy(update={
                    "start": f"{plan.date}T08:00:00{tz_suffix}",
                    "end": f"{plan.date}T22:00:00{tz_suffix}",
                })
                widened_order = target_order.model_copy(update={
                    "slot": widened_slot,
                    "latest_start": f"{plan.date}T21:30:00{tz_suffix}",
                    "deadline": f"{plan.date}T23:00:00{tz_suffix}",
                })
                widened_day = base_day.model_copy(
                    update={"orders": [widened_order if o.order_id == order_id else o for o in base_day.orders]}
                )
                updated, updated_day = _try_assign(widened_day, to_staff_id)

                if updated is None and event_payload.get("auto_fallback"):
                    sc_dict = {sc.service_center_id: sc for sc in ref.service_centers}
                    wt_dict = {w.code: w for w in ref.work_types}
                    orders_map = {o.order_id: o for o in widened_day.orders}
                    absent_ids = set(widened_day.absent_staff_ids or [])
                    order_sc_id = _nearest_sc_id(widened_order, ref)
                    wt_obj = wt_dict.get(widened_order.work_type_code)
                    active_staff = [
                        s for s in ref.staff
                        if s.staff_id != to_staff_id
                        and s.staff_id not in absent_ids
                        and not (wt_obj and wt_obj.profiles and s.profiles and not set(s.profiles).intersection(wt_obj.profiles))
                        and not (widened_order.requires_height and not s.height_permit)
                        and not (widened_order.technology and s.technologies and widened_order.technology not in s.technologies)
                        and all((s.equipment_capacity or {"ONT": 4, "ROUTER": 5, "STB": 4}).get(k, 0) >= q for k, q in (widened_order.required_equipment or {}).items())
                    ]
                    route_counts = {rt.staff_id: sum(1 for st in rt.stops if st.type == "VISIT" and st.status != "CANCELLED") for rt in plan.routes if rt.status != "UNAVAILABLE"}
                    active_staff.sort(key=lambda s: (0 if s.service_center_id == order_sc_id else 1, route_counts.get(s.staff_id, 99)))
                    for s in active_staff:
                        rt_existing = next((rt for rt in plan.routes if rt.staff_id == s.staff_id), None)
                        if rt_existing and rt_existing.status == "UNAVAILABLE":
                            continue
                        feasible, _, _ = _check_single_staff_fit(
                            plan, widened_day, s, widened_order, orders_map, sc_dict, wt_dict, 0.0, {},
                        )
                        if not feasible:
                            continue
                        updated, updated_day = _try_assign(widened_day, s.staff_id)
                        if updated is not None:
                            chosen_staff_id = s.staff_id
                            break

        if updated is None:
            reason = next((u.reason_text for u in plan.unassigned if u.order_id == order_id), None)
            raise HTTPException(
                status_code=409,
                detail=reason or RU_TEXT_0022,
            )

        ev = {"event_id": ev_id, "type": "UI_REASSIGN", "timestamp": ts, "staff_id": chosen_staff_id, "order_id": order_id}
        undo_res = save_reversible_change(plan, (updated, updated_day, ev))
        proc_ms = max(1, round((time.perf_counter() - started_at) * 1000))
        return _make_event_result(
            ev_id,
            updated,
            [chosen_staff_id],
            proc_ms,
            f"Наряд {order_id} переназначен на {chosen_staff_id}",
            reassigned_orders=[order_id],
            undo_token=undo_res["undo_token"],
        )

    if ev_type == "STAFF_UNAVAILABLE" and event_payload.get("reassign_mode") in ("auto", "reschedule_tomorrow", "backlog"):
        staff_id = event_payload.get("staff_id")
        mode = event_payload.get("reassign_mode")
        reason_text = event_payload.get("reason")
        comment_text = str(event_payload.get("comment") or "").strip() or "-"
        target_route = next((r for r in plan.routes if r.staff_id == staff_id), None)
        if target_route is None:
            raise HTTPException(status_code=404, detail=f"Сотрудник {staff_id} не найден в плане")
        now_dt = datetime.fromisoformat(ts)
        future_order_ids = [
            st.order_id
            for st in target_route.stops
            if st.type == "VISIT" and st.order_id and st.status == "PLANNED" and datetime.fromisoformat(st.start) >= now_dt
        ]
        next_date_iso = _next_workday_date(plan.date)
        for o in day.orders:
            if o.order_id in future_order_ids and mode == "reschedule_tomorrow":
                proposed = next_workday_slot(o)
                if proposed:
                    next_date_iso = proposed["start"][:10]
                    break
        deferred = (
            list(dict.fromkeys([*day.deferred_order_ids, *future_order_ids]))
            if mode in ("reschedule_tomorrow", "backlog")
            else list(day.deferred_order_ids)
        )
        absent = list(dict.fromkeys([*(day.absent_staff_ids or []), staff_id]))
        updated_day = day.model_copy(update={"deferred_order_ids": deferred, "absent_staff_ids": absent})
        ev = {
            "event_id": ev_id,
            "type": "STAFF_UNAVAILABLE",
            "timestamp": ts,
            "staff_id": staff_id,
            "reason": reason_text,
            "comment": comment_text,
            "reassign_mode": mode,
        }
        replan_kwargs = {"congestion_factors": congestion_profiles.get(plan.date)}
        if mode in ("reschedule_tomorrow", "backlog"):
            replan_kwargs["search_seconds"] = 0
        updated, updated_day, replan_res = replan_event(
            plan,
            updated_day,
            ref,
            ev,
            **replan_kwargs,
        )
        future_set = set(future_order_ids)
        if mode in ("reschedule_tomorrow", "backlog"):
            for u in updated.unassigned:
                if u.order_id in future_set:
                    u.reason_text = f"{RU_TEXT_0020}: {staff_id}"
                    u.suggested_action = "CONFIRMED_FOR_TOMORROW" if mode == "reschedule_tomorrow" else "MANUAL_REVIEW"
                    if mode == "backlog":
                        u.proposed_slot = None
        undo_res = save_reversible_change(plan, (updated, updated_day, ev))
        proc_ms = max(1, round((time.perf_counter() - started_at) * 1000))
        if mode == "auto":
            unassigned_count = sum(1 for u in updated.unassigned if u.order_id in future_set)
            reassigned_count = max(0, len(future_order_ids) - unassigned_count)
            summary_msg = f"Сотрудник {staff_id} снят со смены; перераспределено {reassigned_count} из {len(future_order_ids)} визитов"
        else:
            label = _format_ru_date(next_date_iso) if mode == "reschedule_tomorrow" else RU_TEXT_0005
            summary_msg = f"Сотрудник {staff_id} снят со смены; {len(future_order_ids)} визитов переведены в {label}"
        return _make_event_result(
            ev_id,
            updated,
            replan_res.changed_staff_ids or [staff_id],
            proc_ms,
            summary_msg,
            new_alerts=replan_res.new_alerts,
            undo_token=undo_res["undo_token"],
        )

    return handle_day_event(active_date, event_payload)


@router.post("/custom-order")
def add_custom_order(order_payload: Dict[str, Any] = Body(...)):
    active_date = db.get("active_date", "2026-10-26")
    if active_date not in db["days"]:
        raise HTTPException(status_code=400, detail=RU_TEXT_0008)

    payload = dict(order_payload)
    assigned_staff = payload.pop("assigned_staff_id", None)
    new_order = prepare_custom_order(Order(**payload))

    if not assigned_staff:
        return orchestrator_add_custom_order(new_order)

    plan, day, ref = get_active_dispatch_context()
    if any(o.order_id == new_order.order_id for o in day.orders):
        raise HTTPException(status_code=409, detail=RU_TEXT_0009)
    staff_obj = next((s for s in ref.staff if s.staff_id == assigned_staff), None)
    if staff_obj is None:
        raise HTTPException(status_code=404, detail=f"Инженер {assigned_staff} не найден")
    absent_ids = set(day.absent_staff_ids or [])
    route_obj = next((rt for rt in plan.routes if rt.staff_id == assigned_staff), None)
    if assigned_staff in absent_ids or (route_obj and route_obj.status == "UNAVAILABLE"):
        raise HTTPException(status_code=409, detail=f"Мастер {assigned_staff} снят со смены (форс-мажор)")
    if new_order.requires_height and not staff_obj.height_permit:
        raise HTTPException(status_code=409, detail=f"У мастера {assigned_staff} отсутствует допуск к работам на высоте (ВЛС)")
    if new_order.technology and staff_obj.technologies and new_order.technology not in staff_obj.technologies:
        raise HTTPException(status_code=409, detail=f"Мастер {assigned_staff} не обучен технологии {new_order.technology}")
    wt_obj = next((w for w in ref.work_types if w.code == new_order.work_type_code), None)
    if wt_obj and wt_obj.profiles and staff_obj.profiles and not set(staff_obj.profiles).intersection(wt_obj.profiles):
        raise HTTPException(status_code=409, detail=f"Профиль мастера {assigned_staff} не соответствует виду работ {wt_obj.name}")
    updated_day = day.model_copy(update={"orders": [new_order, *day.orders]})
    ev = {"event_id": f"manual-{new_order.order_id}", "type": "NEW_ORDER", "timestamp": plan.as_of, "order": new_order.model_dump()}
    updated, updated_day, _ = replan_event(
        plan,
        updated_day,
        ref,
        ev,
        assignments={new_order.order_id: assigned_staff},
        congestion_factors=congestion_profiles.get(plan.date),
        search_seconds=0,
    )
    assigned_route = find_route_for_order(updated, new_order.order_id)
    if assigned_route is None or assigned_route.staff_id != assigned_staff:
        raise HTTPException(
            status_code=409,
            detail=f"Мастер {assigned_staff} не может выполнить наряд {new_order.order_id} в указанный слот без нарушения H1–H10",
        )
    undo_res = save_reversible_change(plan, (updated, updated_day, ev))
    return {
        "status": "added",
        "order_id": new_order.order_id,
        "assigned_staff_id": assigned_staff,
        "plan_version": updated.plan_version,
        "undo_token": undo_res["undo_token"],
        "message": f"Наряд {new_order.order_id} добавлен и назначен мастеру {assigned_staff}",
    }


def _check_single_staff_fit(
    plan: Plan,
    day_data: Any,
    staff_obj: Staff,
    target_order: Order,
    orders_map: Dict[str, Order],
    sc_dict: Dict[str, Any],
    wt_dict: Dict[str, Any],
    base_cv: float,
    route_work_secs: Dict[str, int],
) -> Tuple[bool, Optional[int], Optional[float]]:
    r = next((rt for rt in plan.routes if rt.staff_id == staff_obj.staff_id), None)
    now_dt = datetime.fromisoformat(plan.as_of)
    frozen_prefix = []
    if r:
        for st in r.stops:
            if st.status != "CANCELLED" and (
                st.status in {"IN_PROGRESS", "DONE"}
                or datetime.fromisoformat(st.start) < now_dt
                or (st.type == "RESTOCK" and st.explanation == MANUAL_RESTOCK_EXPLANATION)
            ):
                frozen_prefix.append(st.model_copy(deep=True))
    frozen_order_ids = {st.order_id for st in frozen_prefix if st.order_id}
    future_orders = [
        orders_map[st.order_id]
        for st in (r.stops if r else [])
        if st.type == "VISIT"
        and st.status != "CANCELLED"
        and st.order_id
        and st.order_id != target_order.order_id
        and st.order_id not in frozen_order_ids
        and st.order_id in orders_map
    ]
    candidate_orders = [*future_orders, target_order]
    single_plan = official_shift_planner.plan_day(
        date_str=plan.date,
        now_iso=plan.as_of,
        staff_list=[staff_obj],
        orders=candidate_orders,
        service_centers=sc_dict,
        work_types=wt_dict,
        plan_version=plan.plan_version + 1,
        frozen_routes={staff_obj.staff_id: r.model_copy(update={"stops": frozen_prefix})} if (r and frozen_prefix) else None,
        known_orders=day_data.orders,
        assignments={o.order_id: staff_obj.staff_id for o in candidate_orders},
        preferred_assignments={o.order_id: staff_obj.staff_id for o in candidate_orders},
        congestion_factors=congestion_profiles.get(plan.date),
    )
    prop_route = next((rt for rt in single_plan.routes if rt.staff_id == staff_obj.staff_id), None)
    placed_ids = (
        {st.order_id for st in prop_route.stops if st.type == "VISIT" and st.status != "CANCELLED"}
        if prop_route
        else set()
    )
    expected_ids = {o.order_id for o in candidate_orders}
    if not prop_route or not (expected_ids <= placed_ids):
        return False, None, None
    prev_travel = r.totals.travel_sec if (r and r.totals) else 0
    new_travel = prop_route.totals.travel_sec if prop_route.totals else 0
    extra_travel_sec = new_travel - prev_travel
    cv_delta = 0.0
    if route_work_secs:
        test_work = dict(route_work_secs)
        if prop_route.totals and prop_route.totals.work_sec > 0:
            test_work[staff_obj.staff_id] = prop_route.totals.work_sec
        vals = [v for v in test_work.values() if v > 0]
        if len(vals) > 1:
            mean_v = sum(vals) / len(vals)
            var_v = sum((v - mean_v) ** 2 for v in vals) / len(vals)
            prop_cv = round((var_v ** 0.5) / mean_v, 3) if mean_v > 0 else 0.0
            cv_delta = round(prop_cv - base_cv, 3)
    return True, extra_travel_sec, cv_delta


@router.get("/orders/{order_id}/eligible-engineers")
def get_eligible_engineers(order_id: str):
    active_date = db.get("active_date", "2026-10-26")
    plan = get_current_plan()
    ref = db.get("reference")
    day_data = db["days"].get(active_date)

    if not ref or not ref.staff or not day_data:
        raise HTTPException(status_code=400, detail=RU_TEXT_0006)

    target_order = next((o for o in day_data.orders if o.order_id == order_id), None)
    if not target_order:
        raise HTTPException(status_code=404, detail=f"Наряд {order_id} не найден")

    order_sc_id = _nearest_sc_id(target_order, ref)
    orders_map = {o.order_id: o for o in day_data.orders}
    absent_ids = set(day_data.absent_staff_ids or [])
    base_cv = plan.summary.utilization_cv if plan.summary else 0.0
    sc_dict = {sc.service_center_id: sc for sc in ref.service_centers}
    wt_dict = {w.code: w for w in ref.work_types}
    route_work_secs = {
        rt.staff_id: rt.totals.work_sec
        for rt in plan.routes
        if rt.status != "UNAVAILABLE" and rt.totals and rt.totals.work_sec > 0
    }

    candidates = []
    for s in ref.staff:
        r = next((route for route in plan.routes if route.staff_id == s.staff_id), None)
        visits = [st for st in r.stops if st.type == "VISIT" and st.status != "CANCELLED"] if r else []
        visits_count = len(visits)

        wt_obj = wt_dict.get(target_order.work_type_code)
        district_match = s.service_center_id == order_sc_id
        profile_ok = not (wt_obj and wt_obj.profiles and s.profiles and not set(s.profiles).intersection(wt_obj.profiles))
        height_ok = not (target_order.requires_height and not s.height_permit)
        tech_ok = not (target_order.technology and s.technologies and target_order.technology not in s.technologies)

        cap_stock = dict(s.equipment_capacity) if s.equipment_capacity else {"ONT": 4, "ROUTER": 5, "STB": 4}
        start_stock = dict(s.equipment_start) if s.equipment_start else dict(cap_stock)
        curr_stock = dict(start_stock)

        if r:
            for st in r.stops:
                if st.status == "CANCELLED":
                    continue
                if st.type == "RESTOCK":
                    curr_stock = dict(cap_stock)
                elif st.type == "VISIT" and st.order_id != order_id:
                    v_o = orders_map.get(st.order_id)
                    if v_o and v_o.required_equipment:
                        for eq_k, needed_qty in v_o.required_equipment.items():
                            curr_stock[eq_k] = max(0, curr_stock.get(eq_k, 0) - needed_qty)

        req_eq = target_order.required_equipment or {}
        needs_restock = False
        missing_equipment = {}
        for eq_k, needed_qty in req_eq.items():
            if curr_stock.get(eq_k, 0) < needed_qty:
                needs_restock = True
                missing_equipment[eq_k] = needed_qty - curr_stock.get(eq_k, 0)

        restock_feasible = all(cap_stock.get(eq_k, 0) >= needed_qty for eq_k, needed_qty in req_eq.items())

        if visits_count <= 4:
            workload_status = "LOW"
        elif visits_count in (5, 6):
            workload_status = "NORMAL"
        elif visits_count == 7:
            workload_status = "HIGH"
        else:
            workload_status = "OVERLOAD"

        reasons = []
        eligible = True
        tier = 1
        status_code = "OPTIMAL"

        if s.staff_id in absent_ids or (r and r.status == "UNAVAILABLE"):
            eligible = False
            tier = 5
            status_code = "NOT_ELIGIBLE"
            reasons.append(RU_TEXT_0010)
        elif not profile_ok:
            eligible = False
            tier = 5
            status_code = "NOT_ELIGIBLE"
            reasons.append("Профиль сотрудника не соответствует виду работ")
        elif not height_ok:
            eligible = False
            tier = 5
            status_code = "NOT_ELIGIBLE"
            reasons.append(RU_TEXT_0015)
        elif not tech_ok:
            eligible = False
            tier = 5
            status_code = "NOT_ELIGIBLE"
            reasons.append(f"Не сертифицирован по технологии {target_order.technology}")
        elif not restock_feasible:
            eligible = False
            tier = 5
            status_code = "NOT_ELIGIBLE"
            reasons.append(RU_TEXT_0016)
        elif visits_count >= 8:
            eligible = True
            tier = 4
            status_code = "OVERLOAD"
            reasons.append(f"Превышение нормы: уже назначено {visits_count} нарядов")
        elif not district_match:
            eligible = True
            tier = 3
            status_code = "CROSS_DISTRICT"
            reasons.append(f"Другой район ({s.service_center_id}), потребуется межрайонный переезд")
        elif needs_restock:
            eligible = True
            tier = 2
            status_code = "RESTOCK_NEEDED"
            missing_str = ", ".join([f"{v} шт {k}" for k, v in missing_equipment.items()])
            reasons.append(f"В рюкзаке не хватает: {missing_str}. Требуется дозаезд на склад СЦ ({s.service_center_id})")
        else:
            eligible = True
            tier = 1
            status_code = "OPTIMAL"
            reasons.append(RU_TEXT_0017)

        if district_match and RU_TEXT_0011 not in reasons and eligible:
            reasons.insert(0, RU_TEXT_0011)
        if height_ok and target_order.requires_height:
            reasons.append(RU_TEXT_0012)

        extra_travel_sec: Optional[int] = None
        cv_delta: Optional[float] = None
        solver_feasible = False
        if eligible:
            solver_feasible, extra_travel_sec, cv_delta = _check_single_staff_fit(
                plan,
                day_data,
                s,
                target_order,
                orders_map,
                sc_dict,
                wt_dict,
                base_cv,
                route_work_secs,
            )

        candidates.append(
            {
                "staff_id": s.staff_id,
                "display_name": s.display_name,
                "service_center_id": s.service_center_id,
                "eligible": eligible,
                "solver_feasible": solver_feasible,
                "tier": tier,
                "status_code": status_code,
                "district_match": district_match,
                "profile_ok": profile_ok,
                "height_ok": height_ok,
                "tech_ok": tech_ok,
                "needs_restock": needs_restock,
                "missing_equipment": missing_equipment,
                "backpack_stock": dict(curr_stock),
                "visits_count": visits_count,
                "workload_status": workload_status,
                "extra_travel_sec": extra_travel_sec,
                "cv_delta": cv_delta,
                "reasons": reasons,
            }
        )

    candidates.sort(
        key=lambda c: (
            c["tier"],
            0 if c["solver_feasible"] else 1,
            c["extra_travel_sec"] if c["extra_travel_sec"] is not None else 10**9,
            c["visits_count"],
        )
    )

    return {
        "order_id": order_id,
        "district_id": order_sc_id,
        "requires_height": target_order.requires_height,
        "technology": target_order.technology,
        "required_equipment": target_order.required_equipment,
        "candidates": candidates,
    }


@router.get("/routes/{staff_id}/sheet")
def get_route_sheet(staff_id: str):
    active_date = db.get("active_date", "2026-10-26")
    plan = get_current_plan()
    target_route = next((r for r in plan.routes if r.staff_id == staff_id), None)
    if not target_route:
        raise HTTPException(status_code=404, detail=f"Маршрут для сотрудника {staff_id} не найден")

    ref = db.get("reference")
    day_data = db["days"].get(active_date)
    if not ref or not day_data:
        raise HTTPException(status_code=400, detail=RU_TEXT_0006)

    base_sheet = build_route_sheet(target_route, day_data, ref)
    staff_meta = next((s for s in ref.staff if s.staff_id == staff_id), None)
    sc_meta = next((sc for sc in ref.service_centers if sc.service_center_id == target_route.service_center_id), None)
    orders_map = {o.order_id: o for o in day_data.orders}
    wt_map = {w.code: w for w in ref.work_types}

    active_raw_stops = [s for s in target_route.stops if s.status != "CANCELLED"]
    enriched_stops = []
    for idx, (raw_stop, sheet_stop) in enumerate(zip(active_raw_stops, base_sheet["stops"]), 1):
        ord_info = orders_map.get(raw_stop.order_id) if raw_stop.order_id else None
        wt_info = wt_map.get(ord_info.work_type_code) if (ord_info and ord_info.work_type_code) else None

        work_type_label = sheet_stop["work"] or raw_stop.type
        if raw_stop.type == "VISIT" and wt_info:
            work_type_label = wt_info.name

        enriched_stops.append(
            {
                **sheet_stop,
                "seq": idx,
                "seq_label": str(idx),
                "type": raw_stop.type,
                "status": raw_stop.status,
                "order_id": raw_stop.order_id,
                "work_type": work_type_label,
                "slot": (
                    f"{ord_info.slot.start.split('T')[1][:5]}-{ord_info.slot.end.split('T')[1][:5]}"
                    if ord_info
                    else "-"
                ),
                "technology": ord_info.technology if ord_info else "-",
                "required_equipment": ord_info.required_equipment if ord_info else {},
                "called": bool(raw_stop.order_id and (plan.date, raw_stop.order_id) in recorded_calls),
                "actual_start": raw_stop.actual_start,
                "explanation": raw_stop.explanation or "",
            }
        )

    return {
        **base_sheet,
        "staff_id": staff_id,
        "display_name": staff_meta.display_name if staff_meta else staff_id,
        "service_center_id": target_route.service_center_id,
        "service_center_name": sc_meta.name if sc_meta else target_route.service_center_id,
        "vehicle": RU_TEXT_0003,
        "warehouse_manifest": {
            "initial_load": base_sheet["initial_load"],
            "capacity": base_sheet["capacity"],
            "day_consumption": base_sheet["consumption"],
            "remaining": base_sheet["remaining"],
        },
        "totals": target_route.totals.model_dump() if target_route.totals else {},
        "stops": enriched_stops,
    }


@router.post("/orders/{order_id}/toggle-complete")
def toggle_order_complete(order_id: str):
    plan, day_data, _ = get_active_dispatch_context()
    updated = plan.model_copy(deep=True)

    target_stop = None
    target_route = None
    for r in updated.routes:
        for s in r.stops:
            if s.order_id == order_id and s.type == "VISIT" and s.status != "CANCELLED":
                target_stop = s
                target_route = r
                break
        if target_stop:
            break

    if not target_stop or not target_route:
        raise HTTPException(status_code=404, detail=f"Наряд {order_id} не найден в маршрутах")

    is_now_done = target_stop.status not in ("DONE", "COMPLETED")
    target_stop.status = "DONE" if is_now_done else "PLANNED"
    target_stop.actual_end = (target_stop.end or updated.as_of) if is_now_done else None
    updated.plan_version += 1

    if is_now_done:
        target_stop.explanation = f"Наряд выполнен мастером {target_route.staff_id}"
        msg = f"Наряд {order_id} успешно отмечен как выполненный"
    else:
        target_stop.explanation = RU_TEXT_0004
        msg = f"Наряд {order_id} возвращён в работу"

    ev = {
        "type": "VISIT_COMPLETED" if is_now_done else "UI_REOPEN",
        "event_id": uuid4().hex,
        "timestamp": updated.as_of,
        "staff_id": target_route.staff_id,
        "order_id": order_id,
        "actual_end": target_stop.actual_end or updated.as_of,
    }
    undo_res = save_reversible_change(plan, (updated, day_data, ev))
    return {
        "status": "success",
        "order_id": order_id,
        "staff_id": target_route.staff_id,
        "stop_status": target_stop.status,
        "completed": is_now_done,
        "undo_token": undo_res["undo_token"],
        "message": msg,
    }


@router.get("/routing/track/{staff_id}")
def get_staff_route_track(staff_id: str):
    active_date = db.get("active_date", "2026-10-26")
    plan = get_current_plan()
    target_route = next((r for r in plan.routes if r.staff_id == staff_id), None)
    if not target_route:
        raise HTTPException(status_code=404, detail=f"Маршрут для {staff_id} не найден")

    ref = db.get("reference")
    sc_meta = next((sc for sc in ref.service_centers if sc.service_center_id == target_route.service_center_id), None) if ref else None
    day_data = db["days"].get(active_date)
    orders_map = {o.order_id: o for o in day_data.orders} if day_data else {}

    default_sc_lat = sc_meta.lat if sc_meta else 56.8450
    default_sc_lon = sc_meta.lon if sc_meta else 60.6050

    points: List[Tuple[float, float]] = []
    last_pt = (default_sc_lat, default_sc_lon)
    for s in target_route.stops:
        if s.status == "CANCELLED":
            continue
        if s.type in ("DEPOT_START", "DEPOT_END", "RESTOCK"):
            pt = (default_sc_lat, default_sc_lon)
        elif s.type == "BREAK":
            pt = (last_pt[0] + 0.0003, last_pt[1] + 0.0003)
        elif s.order_id and s.order_id in orders_map:
            ord_obj = orders_map[s.order_id]
            pt = (ord_obj.lat, ord_obj.lon)
        else:
            pt = last_pt
        points.append(pt)
        last_pt = pt

    track_result = osrm_client.get_route_geometry(points)
    return {
        "staff_id": staff_id,
        "date": active_date,
        "service_center_id": target_route.service_center_id,
        "provider": track_result.get("provider", "OSRM"),
        "coordinates": track_result.get("coordinates", points),
        "distance_m": track_result.get("distance_m", 0),
        "duration_sec": track_result.get("duration_sec", 0),
        "waypoints_count": len(points),
    }


@router.post("/routing/track")
def get_custom_route_track(payload: Dict[str, Any] = Body(...)):
    raw_points = payload.get("points", [])
    if not raw_points or len(raw_points) < 2:
        return {"coordinates": raw_points, "distance_m": 0, "duration_sec": 0, "provider": "empty"}
    points = [(float(p[0]), float(p[1])) for p in raw_points]
    return osrm_client.get_route_geometry(points)


@router.get("/traffic/profile")
def get_traffic_profile():
    return distance_engine.get_traffic_profile()


@router.post("/traffic/profile")
def update_traffic_profile(payload: Dict[str, Any] = Body(...)):
    if "traffic_enabled" in payload:
        distance_engine.traffic_enabled = bool(payload["traffic_enabled"])
    if "disabled_hours" in payload:
        distance_engine.disabled_hours = {int(h) for h in payload["disabled_hours"]}
    if "bridge_penalty_enabled" in payload:
        distance_engine.bridge_penalty_enabled = bool(payload["bridge_penalty_enabled"])
    if "bridge_peak_factor" in payload:
        distance_engine.bridge_peak_factor = float(payload["bridge_peak_factor"])
    if "hourly_factors" in payload and isinstance(payload["hourly_factors"], dict):
        for h, factor in payload["hourly_factors"].items():
            distance_engine.hourly_factors[int(h)] = float(factor)
    return distance_engine.get_traffic_profile()
