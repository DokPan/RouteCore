from datetime import datetime, timedelta


def next_workday_slot(order):
    start = datetime.fromisoformat(order.slot.start)
    end = datetime.fromisoformat(order.slot.end)
    day_offset = timedelta(days=1)
    while (start + day_offset).weekday() >= 5:
        day_offset += timedelta(days=1)
    proposed_start, proposed_end = start + day_offset, end + day_offset
    if proposed_end > datetime.fromisoformat(order.deadline):
        return None
    return {"start": proposed_start.isoformat(), "end": proposed_end.isoformat()}
