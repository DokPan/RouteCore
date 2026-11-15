import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, HTTPException, Path as FPath, Body

from ..config import BASE_DIR
from ..models.official_spec import (
    Health, ReferenceData, TravelMatrix, LoadResult, DayInput,
    PlanRequest, Plan, EventResult, PgpGroup, Order
)
from ..core.distance import distance_engine
from ..core.roads import OSRM_URL, RoadServiceError, road_geometry
from ..core.pgp_detector import pgp_detector
from ..core.solver import official_shift_planner
from ..core.reactive import REPLAN_EVENTS, advance_model_time, replan_event
from ..core.plan_improvement import SEARCH_SECONDS, improve_plan
from ..core.plan_validation import validate_plan
from time import perf_counter

INVALID_MATRIX = "Некорректная матрица проезда."
INVALID_DAY = "Проверьте дату и уникальность номеров нарядов."
MISSING_DAY = "Данные для дня {date} не найдены. Сначала выполните PUT /api/v1/days/{date}"
MISSING_REFERENCE = "Справочники не загружены. Сначала выполните PUT /api/v1/reference"
MISSING_PLAN = "План для даты {date} не найден"
INVALID_EVENT_TYPE = "Укажите номер и поддерживаемый тип события."
INVALID_EVENT_FIELDS = "Проверьте время и обязательные поля события."
STALE_EVENT = "Время события раньше текущего времени плана."
INVALID_EVENT_TARGET = "Событие ссылается на неизвестный наряд или сотрудника либо дублирует номер наряда."
INVALID_VISIT_EVENT = "Событие не соответствует назначению или состоянию визита."
INITIAL_PLAN_ALREADY_USED = "Для этой даты уже принято событие. Получите актуальный план через GET."
INVALID_PLAN_TIME_LIMIT = "Лимит расчёта должен быть от 1 до 300 секунд."
MISSING_ROUTE = "Маршрут не найден"
MISSING_ACTIVE_DAY = "Нет активного дня"
DUPLICATE_ORDER = "Наряд с таким номером уже существует."
DUPLICATE_ORDER_IDS = "Номера нарядов должны быть уникальными."
DEMO_CLIENT_NAME = "Демо-абонент №{number}"
DISPATCH_TIMEZONE = timezone(timedelta(hours=5))

router = APIRouter(prefix="/api/v1", tags=["Rostelecom Official Orchestrator API"])

db: Dict[str, Any] = {
    "reference": None,
    "travel_matrix": None,
    "active_date": "2026-10-26",
    "days": {},
    "plans": {},
    "pgp_groups": {},
    "events_log": {},
    "initial_kpis": {},
}
plan_history = {}
MANUAL_REPLAN_EVENT = "MANUAL_REPLAN"
visit_end_overrides = {}
congestion_profiles = {}
ui_reset_hooks = []


def get_validation_matrix():
    matrix = db["travel_matrix"]
    if OSRM_URL and matrix and matrix.matrix_id == "ekb-metro-2026-v1":
        return None
    return matrix


def dispatch_datetime(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError()
    return moment.astimezone(DISPATCH_TIMEZONE)


def normalize_order_times(order: Order) -> Order:
    slot = order.slot.model_copy(update={
        "start": dispatch_datetime(order.slot.start).isoformat(),
        "end": dispatch_datetime(order.slot.end).isoformat(),
    })
    return order.model_copy(update={
        "slot": slot,
        "registered_at": dispatch_datetime(order.registered_at).isoformat(),
        "latest_start": dispatch_datetime(order.latest_start).isoformat(),
        "deadline": dispatch_datetime(order.deadline).isoformat(),
    })

def load_default_official_data():
    dataset_dir = BASE_DIR / "data" / "official_dataset"
    if not dataset_dir.exists():
        dataset_dir = BASE_DIR / "api" / "examples"
    if not dataset_dir.exists():
        dataset_dir = BASE_DIR / "data" / "official_examples"

    if not dataset_dir.exists():
        return
    db["events_log"].clear()
    db["plans"].clear()
    db["pgp_groups"].clear()
    db["initial_kpis"].clear()
    plan_history.clear()
    visit_end_overrides.clear()
    congestion_profiles.clear()
    distance_engine.traffic_enabled = True


    ref_file = dataset_dir / "ReferenceData.json"
    if not ref_file.exists():
        ref_file = dataset_dir / "ReferenceDataExample.json"
    if ref_file.exists():
        with open(ref_file, encoding="utf-8") as f:
            ref_json = json.load(f)
            db["reference"] = ReferenceData(**ref_json)


    mat_file = dataset_dir / "TravelMatrix.json"
    if not mat_file.exists():
        mat_file = dataset_dir / "TravelMatrixExample.json"
    if mat_file.exists():
        with open(mat_file, encoding="utf-8") as f:
            mat_json = json.load(f)
            matrix = TravelMatrix(**mat_json)
            db["travel_matrix"] = matrix
            distance_engine.set_matrix(matrix)


    day_file = dataset_dir / "DayInput.json"
    if not day_file.exists():
        day_file = dataset_dir / "DayInputExample.json"
    if day_file.exists():
        with open(day_file, encoding="utf-8") as f:
            day_json = json.load(f)
            for number, order in enumerate(day_json["orders"], start=1):
                order.setdefault("client_name", DEMO_CLIENT_NAME.format(number=number))
                if order.get("building_type") == "MKD":
                    order.setdefault("entrance", str(1 + number % 4))
                    order.setdefault("floor", 1 + number % 9)
            day_input = DayInput(**day_json)
            db["active_date"] = day_input.date
            db["days"][day_input.date] = day_input

load_default_official_data()


@router.get("/health", response_model=Health)
def health():
    return Health(status="ok", team="RTC Crew Smart Dispatcher", version="1.0.0")

@router.put("/reference", response_model=LoadResult)
def load_reference(data: ReferenceData):
    db["reference"] = data
    for key in ("days", "plans", "pgp_groups", "events_log", "initial_kpis"):
        db[key].clear()
    plan_history.clear()
    visit_end_overrides.clear()
    congestion_profiles.clear()
    sc_cnt = len(data.service_centers)
    staff_cnt = len(data.staff)
    nodes_cnt = len(data.access_nodes)
    wt_cnt = len(data.work_types)
    bld_cnt = sum(len(n.buildings) for n in data.access_nodes)

    return LoadResult(
        status="loaded",
        counts={
            "service_centers": sc_cnt,
            "staff": staff_cnt,
            "access_nodes": nodes_cnt,
            "buildings": bld_cnt,
            "work_types": wt_cnt
        }
    )

@router.put("/reference/travel-matrix", response_model=LoadResult)
def load_travel_matrix(matrix: TravelMatrix):
    size = len(matrix.points)
    if len(set(matrix.points)) != size or any(
        len(table) != size or any(len(row) != size or any(value < 0 for value in row) for row in table)
        for table in (matrix.durations_sec, matrix.distances_m)
    ):
        raise HTTPException(422, INVALID_MATRIX)
    db["travel_matrix"] = matrix
    distance_engine.set_matrix(matrix)
    return LoadResult(
        status="loaded",
        counts={"points": len(matrix.points)}
    )

@router.put("/days/{date}", response_model=LoadResult)
def load_day(date: str = FPath(...), day_data: DayInput = Body(...)):
    if not db["reference"]:
        raise HTTPException(409, MISSING_REFERENCE)
    if date != day_data.date or len({order.order_id for order in day_data.orders}) != len(day_data.orders):
        raise HTTPException(422, INVALID_DAY)
    try:
        day_data = day_data.model_copy(update={
            "now": dispatch_datetime(day_data.now).isoformat(),
            "orders": [normalize_order_times(order) for order in day_data.orders],
        })
    except ValueError as error:
        raise HTTPException(422, INVALID_DAY) from error
    db["active_date"] = date
    db["days"][date] = day_data
    db["plans"].pop(date, None)
    db["initial_kpis"].pop(date, None)
    plan_history.pop(date, None)
    visit_end_overrides.pop(date, None)
    congestion_profiles.pop(date, None)
    db["events_log"].pop(date, None)
    return LoadResult(
        status="loaded",
        counts={"orders": len(day_data.orders)}
    )

@router.post("/days/{date}/plan", response_model=Plan)
def create_initial_plan(date: str = FPath(...), req: Optional[PlanRequest] = None):
    if not db["reference"]:
        raise HTTPException(409, MISSING_REFERENCE)
    if db["events_log"].get(date):
        raise HTTPException(409, INITIAL_PLAN_ALREADY_USED)
    if req and (req.time_limit_sec is None or not 1 <= req.time_limit_sec <= 300):
        raise HTTPException(422, INVALID_PLAN_TIME_LIMIT)
    return build_plan(date, req, rebuild_initial=True)


def build_plan(date: str, req: Optional[PlanRequest] = None, *, rebuild_initial: bool = False,
               store_initial: bool = True):
    if date not in db["days"]:
        raise HTTPException(status_code=404, detail=MISSING_DAY.format(date=date))

    if not db["reference"]:
        raise HTTPException(status_code=409, detail=MISSING_REFERENCE)

    day_data: DayInput = db["days"][date]
    ref: ReferenceData = db["reference"]

    previous_plan = db["plans"].get(date)
    if previous_plan is not None and not rebuild_initial:
        event = {
            "type": MANUAL_REPLAN_EVENT,
            "event_id": f"manual-replan:{date}:{previous_plan.plan_version + 1}",
            "timestamp": previous_plan.as_of,
        }
        plan, updated_day, _ = replan_event(
            previous_plan, day_data, ref, event,
            search_seconds=req.time_limit_sec if req and req.time_limit_sec is not None else SEARCH_SECONDS,
        )
        db["plans"][date] = plan
        db["days"][date] = updated_day
        db["pgp_groups"][date] = plan.pgp_groups
        plan_history[date] = previous_plan, event
        return plan

    sc_dict = {s.service_center_id: s for s in ref.service_centers}
    wt_dict = {w.code: w for w in ref.work_types}


    dispatchable_orders, pgp_groups = pgp_detector.detect_and_filter(
        orders=day_data.orders,
        as_of=day_data.now,
        work_types=ref.work_types,
    )


    started_at = perf_counter()
    options = dict(
        date_str=date,
        now_iso=day_data.now,
        staff_list=ref.staff,
        orders=dispatchable_orders,
        service_centers=sc_dict,
        work_types=wt_dict,
        absent_staff_ids=day_data.absent_staff_ids,
        pgp_groups=pgp_groups,
        plan_version=1,
    )
    plan = official_shift_planner.plan_day(**options)
    requested_seconds = req.time_limit_sec if req and req.time_limit_sec is not None else SEARCH_SECONDS
    plan = improve_plan(official_shift_planner, plan, options, requested_seconds - (perf_counter() - started_at))
    plan.computation_ms = round((perf_counter() - started_at) * 1000)

    if store_initial:
        save_initial_plan(plan, day_data, ref)
    else:
        db["plans"][date] = plan
        db["pgp_groups"][date] = pgp_groups
        plan_history.pop(date, None)
    return plan


def save_initial_plan(plan: Plan, day: DayInput, reference: ReferenceData):
    order_centers = {order.order_id: address_district(order) for order in day.orders}
    report = validate_plan(
        plan, day, reference, matrix=get_validation_matrix(),
        order_service_centers=order_centers,
    )
    db["initial_kpis"][plan.date] = {
        key: report[key] for key in ("weighted_on_time", "weighted_total", "travel_sec", "utilization_cv")
    }
    db["plans"][plan.date] = plan
    db["pgp_groups"][plan.date] = plan.pgp_groups
    plan_history.pop(plan.date, None)

@router.get("/days/{date}/plan", response_model=Plan)
def get_day_plan(date: str = FPath(...)):
    if date not in db["plans"]:
        raise HTTPException(status_code=404, detail=MISSING_PLAN.format(date=date))
    return db["plans"][date]

@router.post("/days/{date}/events", response_model=EventResult)
def handle_day_event(date: str = FPath(...), event_payload: Dict[str, Any] = Body(...)):
    started_at = perf_counter()
    event_id = event_payload.get("event_id")
    event_type = event_payload.get("type")
    required_fields = {
        "NEW_ORDER": ("order",), "ORDER_CANCELLED": ("order_id",), "ORDER_DEFERRED": ("order_id",),
        "STAFF_UNAVAILABLE": ("staff_id",), "VISIT_DELAYED": ("staff_id", "order_id", "expected_end"),
        "ORDER_TRANSFERRED": ("staff_id", "order_id", "actual_end", "transferred_to"), "TIME_TICK": (),
        "VISIT_COMPLETED": ("staff_id", "order_id", "actual_end"),
    }
    if not isinstance(event_id, str) or not event_id or not isinstance(event_type, str) or event_type not in required_fields:
        raise HTTPException(422, INVALID_EVENT_TYPE)
    event_payload = dict(event_payload)
    try:
        timestamp = dispatch_datetime(event_payload["timestamp"])
        event_payload["timestamp"] = timestamp.isoformat()
        if event_type == "STAFF_UNAVAILABLE" and event_payload.get("until") is not None:
            available_at = dispatch_datetime(event_payload["until"])
            if available_at <= timestamp or available_at.date() != timestamp.date():
                raise ValueError()
            event_payload["until"] = available_at.isoformat()
        for field in required_fields[event_type]:
            value = event_payload[field]
            if field == "order":
                event_payload[field] = normalize_order_times(Order.model_validate(value)).model_dump()
            elif not isinstance(value, str) or not value:
                raise ValueError()
            elif field in {"expected_end", "actual_end"}:
                event_payload[field] = dispatch_datetime(value).isoformat()
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(422, INVALID_EVENT_FIELDS) from error
    previous_result = db["events_log"].get(date, {}).get(event_id)
    if previous_result:
        return previous_result.model_copy(update={"duplicate": True, "processing_ms": 0})
    if date not in db["days"]:
        raise HTTPException(404, MISSING_DAY.format(date=date))
    if not db["reference"]:
        raise HTTPException(409, MISSING_REFERENCE)
    day = db["days"][date]
    reference = db["reference"]
    staff_id = event_payload.get("staff_id")
    order_id = event_payload.get("order_id")
    order_ids = {order.order_id for order in day.orders}
    if ((staff_id and reference and all(person.staff_id != staff_id for person in reference.staff))
            or (order_id and order_id not in order_ids)
            or (event_type == "NEW_ORDER" and event_payload["order"]["order_id"] in order_ids)):
        raise HTTPException(422, INVALID_EVENT_TARGET)
    current_plan = db["plans"].get(date)
    if timestamp < datetime.fromisoformat(current_plan.as_of if current_plan else day.now):
        raise HTTPException(409, STALE_EVENT)
    new_initial_plan = current_plan is None
    if new_initial_plan:
        current_plan = build_plan(date, PlanRequest(time_limit_sec=0), store_initial=False)

    if event_type in {"VISIT_DELAYED", "ORDER_TRANSFERRED", "VISIT_COMPLETED"}:
        visit = next((
            stop for route in current_plan.routes if route.staff_id == staff_id
            for stop in route.stops if stop.type == "VISIT" and stop.order_id == order_id
        ), None)
        revised_end = datetime.fromisoformat(event_payload.get("expected_end") or event_payload["actual_end"])
        if (not visit or visit.status in {"CANCELLED", "DONE"}
                or timestamp < datetime.fromisoformat(visit.start)
                or revised_end < datetime.fromisoformat(visit.start)):
            raise HTTPException(422, INVALID_VISIT_EVENT)
    if event_payload.get("type") in REPLAN_EVENTS:
        plan, updated_day, result = replan_event(
            current_plan, day, reference, event_payload,
            congestion_factors=congestion_profiles.get(date),
        )
        if new_initial_plan:
            save_initial_plan(current_plan, day, reference)
        db["days"][date] = updated_day
        db["plans"][date] = plan
        plan_history[date] = current_plan, event_payload
        if event_type in {"VISIT_DELAYED", "ORDER_TRANSFERRED", "VISIT_COMPLETED"}:
            staff_id = event_payload["staff_id"]
            order_id = event_payload["order_id"]
            expected_end = event_payload.get("expected_end") or event_payload.get("actual_end")
            if any(stop.order_id == order_id and stop.end == expected_end
                   for route in plan.routes if route.staff_id == staff_id for stop in route.stops):
                visit_end_overrides.setdefault(date, {})[staff_id, order_id] = expected_end
        db["pgp_groups"][date] = plan.pgp_groups
        result.processing_ms = round((perf_counter() - started_at) * 1000)
        db["events_log"].setdefault(date, {})[event_id] = result
        return result
    updated_plan, result = advance_model_time(current_plan, db["days"][date], event_payload)
    if new_initial_plan:
        save_initial_plan(current_plan, day, reference)
    db["plans"][date] = updated_plan
    plan_history[date] = current_plan, event_payload
    result.processing_ms = round((perf_counter() - started_at) * 1000)
    db["events_log"].setdefault(date, {})[event_id] = result
    return result


@router.get("/plan", response_model=Plan)
def get_current_plan():
    active_date = db.get("active_date", "2026-10-26")
    if active_date in db["days"] and active_date not in db["plans"]:
        return build_plan(active_date, PlanRequest(time_limit_sec=0))
    return get_day_plan(active_date)

@router.post("/plan", response_model=Plan)
def create_current_plan():
    active_date = db.get("active_date", "2026-10-26")
    return build_plan(active_date)


@router.get("/plan/map/{staff_id}")
def get_map_route(staff_id: str):
    plan = get_current_plan()
    route = next((route for route in plan.routes if route.staff_id == staff_id), None)
    if route is None:
        raise HTTPException(status_code=404, detail=MISSING_ROUTE)

    reference = db["reference"]
    centers = {center.service_center_id: center for center in reference.service_centers}
    day = db["days"][db["active_date"]]
    orders = {order.order_id: order for order in day.orders}
    stops = {}
    addresses = {}
    segments = []
    current = []
    for stop in route.stops:
        if stop.type == "BREAK" or stop.status == "CANCELLED":
            continue
        location = orders.get(stop.order_id) if stop.type == "VISIT" else centers.get(stop.location_id)
        if location is None:
            segments.append(current)
            current = []
            continue
        point = (location.lat, location.lon)
        stops[stop.seq] = point
        addresses[stop.seq] = location.address
        if not current or point != current[-1]:
            current.append(point)
    segments.append(current)

    lines = []
    snapped = {}
    if OSRM_URL:
        for points in segments:
            if not points:
                continue
            for offset in range(0, max(1, len(points) - 1), 49):
                chunk = tuple(points[offset:offset + 50])
                try:
                    line, waypoints = road_geometry(chunk if len(chunk) > 1 else chunk * 2)
                except RoadServiceError:
                    continue
                snapped.update(zip(chunk, waypoints))
                if len(chunk) > 1 and line:
                    lines.append(line)
        stops = {seq: snapped[point] for seq, point in stops.items() if point in snapped}
    expected_lines = sum((len(points) + 47) // 49 for points in segments if len(points) > 1)
    return {"stops": stops, "addresses": addresses, "lines": lines, "complete": all(
        stop.type == "BREAK" or stop.status == "CANCELLED" or stop.seq in stops for stop in route.stops
    ) and len(lines) == expected_lines}


@router.get("/staff-directory")
def get_staff_directory():
    reference = db["reference"]
    return {staff.staff_id: staff.display_name for staff in reference.staff} if reference else {}


@router.get("/address-points")
def list_address_points():
    day = db["days"].get(db["active_date"])
    if day is None:
        return []
    points = {
        order.building_id: {
            "id": order.building_id, "address": order.address,
            "lat": order.lat, "lng": order.lon, "district": address_district(order),
        }
        for order in day.orders
    }
    return sorted(points.values(), key=lambda point: point["address"])


def address_district(order):
    centers = db["reference"].service_centers if db["reference"] else []
    center_ids = {center.service_center_id for center in centers}
    if order.district_id in center_ids:
        return order.district_id
    if order.access_node_id and order.access_node_id.startswith("AN-"):
        demo_center = "SC-" + order.access_node_id[3:5]
        if demo_center in center_ids:
            return demo_center
    return None


@router.get("/service-centers")
def get_service_centers():
    reference = db["reference"]
    return {center.service_center_id: center.name for center in reference.service_centers} if reference else {}

@router.get("/pgp")
def get_current_pgp():
    active_date = db.get("active_date", "2026-10-26")
    return db["pgp_groups"].get(active_date, [])

def handle_current_event(event_payload: Dict[str, Any] = Body(...)):
    active_date = db.get("active_date", "2026-10-26")
    return handle_day_event(active_date, event_payload)

def add_custom_order(new_order: Order):
    active_date = db.get("active_date", "2026-10-26")
    if active_date not in db["days"]:
        raise HTTPException(status_code=400, detail=MISSING_ACTIVE_DAY)

    day_data = db["days"][active_date]
    if any(order.order_id == new_order.order_id for order in day_data.orders):
        raise HTTPException(409, DUPLICATE_ORDER)
    current = get_current_plan()
    result = handle_day_event(active_date, {
        "event_id": "manual-" + new_order.order_id, "type": "NEW_ORDER",
        "timestamp": current.as_of, "order": new_order.model_dump(),
    })
    return {"status": "added", "order_id": new_order.order_id, "plan_version": result.plan_version}

@router.post("/upload-day")
def upload_day_json(day_data: DayInput):
    if len({order.order_id for order in day_data.orders}) != len(day_data.orders):
        raise HTTPException(422, DUPLICATE_ORDER_IDS)
    previous_day = db["days"].get(day_data.date)
    previous_plan = db["plans"].pop(day_data.date, None)
    db["days"][day_data.date] = day_data
    try:
        plan = build_plan(day_data.date, PlanRequest(time_limit_sec=2), rebuild_initial=True, store_initial=True)
    except Exception:
        if previous_day is None:
            db["days"].pop(day_data.date, None)
        else:
            db["days"][day_data.date] = previous_day
        if previous_plan is not None:
            db["plans"][day_data.date] = previous_plan
        raise
    db["active_date"] = day_data.date
    plan_history.pop(day_data.date, None)
    db["events_log"].pop(day_data.date, None)
    visit_end_overrides.pop(day_data.date, None)
    congestion_profiles.pop(day_data.date, None)
    return {"status": "success", "orders_count": len(day_data.orders), "plan_version": plan.plan_version}

@router.post("/reset-demo")
def reset_demo():
    load_default_official_data()
    for hook in ui_reset_hooks:
        hook()
    active_date = db.get("active_date", "2026-10-26")
    plan = build_plan(active_date, PlanRequest(time_limit_sec=2), rebuild_initial=True, store_initial=True)
    return {
        "status": "success",
        "date": active_date,
        "orders": len(db["days"][active_date].orders),
        "plan_version": plan.plan_version,
    }
