from fastapi import APIRouter, HTTPException

from .orchestrator import db
from ..core.duration_history import CompletedWork, load_duration_history, read_duration_history

DAY_UNAVAILABLE = "Загрузите наряды и справочники для расчёта прогноза."
from ..core.duration_forecast import forecast_duration
from ..core.pgp_detector import pgp_detector
from ..core.solver import official_shift_planner


router = APIRouter(prefix="/api/v1/ui")
ORDER_NOT_FOUND = "Наряд не найден."
WORK_TYPE_NOT_FOUND = "Норматив для этого типа работ не найден."


@router.put("/duration-history")
def upload_duration_history(samples: list[CompletedWork]):
    return load_duration_history(samples)


@router.get("/orders/{order_id}/duration-forecast")
def get_order_duration_forecast(order_id: str):
    day = db["days"].get(db["active_date"])
    order = next((item for item in day.orders if item.order_id == order_id), None) if day else None
    if order is None:
        raise HTTPException(404, ORDER_NOT_FOUND)
    reference = db["reference"]
    work_type = next((item for item in reference.work_types if item.code == order.work_type_code), None) if reference else None
    if work_type is None:
        raise HTTPException(404, WORK_TYPE_NOT_FOUND)
    return forecast_duration(order, work_type, read_duration_history())


@router.post("/whatif/duration")
def preview_duration_plan():
    day = db["days"].get(db["active_date"])
    reference = db["reference"]
    if not day or not reference:
        raise HTTPException(409, DAY_UNAVAILABLE)
    history = read_duration_history()
    work_types = {work.code: work for work in reference.work_types}
    orders, pgp_groups = pgp_detector.detect_and_filter(
        orders=day.orders, as_of=day.now, work_types=reference.work_types,
    )
    forecasts = {
        order.order_id: forecast_duration(order, work_types[order.work_type_code], history)
        for order in orders if order.work_type_code in work_types
    }
    preview = official_shift_planner.plan_day(
        date_str=day.date, now_iso=day.now, staff_list=reference.staff, orders=orders,
        service_centers={center.service_center_id: center for center in reference.service_centers},
        work_types=work_types, absent_staff_ids=day.absent_staff_ids, pgp_groups=pgp_groups,
        duration_forecasts={key: forecast["minutes"] for key, forecast in forecasts.items()},
    )
    return {"plan": preview, "forecasts": forecasts}
