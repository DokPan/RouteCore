from datetime import datetime, timedelta
from math import ceil

from ..models.official_spec import Alert
from .visit_slots import latest_allowed_start


RESCHEDULE_NOTICE_MINUTES = 30
RISK_MESSAGE = "Есть риск опоздания по наряду {order_id}. Проверьте предложенное действие."


def forecast_visit_warnings(previous, updated, orders, event):
    timestamp = event["timestamp"]
    now = datetime.fromisoformat(timestamp)
    earliest_rebooking = now + timedelta(minutes=RESCHEDULE_NOTICE_MINUTES)
    updated_visits = {
        stop.order_id: (route.staff_id, stop) for route in updated.routes
        for stop in route.stops if stop.type == "VISIT" and stop.status != "CANCELLED"
    }
    unavailable = {route.staff_id: route for route in updated.routes if route.unavailable_from}
    previous_travel = {route.staff_id: route.totals.travel_sec for route in previous.routes if route.totals}
    updated_travel = {route.staff_id: route.totals.travel_sec for route in updated.routes if route.totals}
    warnings = []
    for route in previous.routes:
        earliest_departure = None
        for stop in route.stops:
            if stop.status == "CANCELLED":
                continue
            start = datetime.fromisoformat(stop.start)
            end = datetime.fromisoformat(stop.end)
            if start < now or stop.status in {"IN_PROGRESS", "DONE"}:
                if (route.staff_id == event.get("staff_id") and stop.order_id
                        and stop.order_id == event.get("order_id")):
                    revised_end = event.get("expected_end") or event.get("actual_end")
                    if revised_end:
                        end = datetime.fromisoformat(revised_end)
                earliest_departure = end
                continue
            forecast = max(start, (earliest_departure or now) + timedelta(seconds=stop.travel_sec))
            absence = unavailable.get(route.staff_id)
            indefinitely_absent = absence and not absence.unavailable_until
            if absence and absence.unavailable_until:
                forecast = max(forecast, datetime.fromisoformat(absence.unavailable_until) + timedelta(seconds=stop.travel_sec))
            earliest_departure = forecast + (end - start)
            order = orders.get(stop.order_id)
            if stop.type != "VISIT" or not order or stop.order_id in updated.cancelled:
                continue
            slot_start = datetime.fromisoformat(order.slot.start)
            latest = latest_allowed_start(order.work_type_code, slot_start, datetime.fromisoformat(order.latest_start))
            if forecast <= latest and not indefinitely_absent:
                continue
            replacement = updated_visits.get(stop.order_id)
            suggested_staff = replacement[0] if replacement and replacement[0] != route.staff_id else None
            proposed_slot = None
            proposed_start = max(forecast, earliest_rebooking)
            proposed_end = proposed_start + (end - start)
            if (not suggested_staff and not indefinitely_absent
                    and earliest_rebooking <= slot_start
                    and proposed_end.date() == now.date()
                    and proposed_end <= datetime.fromisoformat(order.deadline)):
                proposed_slot = {"start": proposed_start.isoformat(), "end": proposed_end.isoformat()}
            warnings.append(Alert(
                alert_id=f"{event['event_id']}:forecast:{stop.order_id}", created_at=timestamp,
                type="LATE_RISK", order_id=stop.order_id, staff_id=route.staff_id,
                forecast_start=None if indefinitely_absent else forecast.isoformat(),
                lateness_min=None if indefinitely_absent else ceil((forecast - latest).total_seconds() / 60),
                suggested_action="REASSIGN" if suggested_staff else "RESCHEDULE" if proposed_slot else "NONE",
                suggested_staff_id=suggested_staff, proposed_slot=proposed_slot,
                extra_travel_sec=(updated_travel[suggested_staff] - previous_travel.get(suggested_staff, 0)
                                  if suggested_staff in updated_travel else None),
                applied=bool(suggested_staff), message=RISK_MESSAGE.format(order_id=stop.order_id),
            ))
    return warnings
