from datetime import datetime, timedelta

INSTALLATION_ARRIVAL_LIMIT_MINUTES = 10


def latest_allowed_start(work_type_code: str, slot_start: datetime, latest_start: datetime) -> datetime:
    if work_type_code.startswith("INST"):
        return min(latest_start, slot_start + timedelta(minutes=INSTALLATION_ARRIVAL_LIMIT_MINUTES))
    return latest_start
