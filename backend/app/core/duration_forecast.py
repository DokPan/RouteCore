from statistics import median


def forecast_duration(order, work_type, history):
    matching_durations = [
        sample.actual_duration_min for sample in history
        if sample.work_type_code == order.work_type_code
        and sample.technology == order.technology
    ]
    if matching_durations:
        return {
            "minutes": round(median(matching_durations)),
            "sample_count": len(matching_durations),
            "source": "history",
        }
    return {"minutes": work_type.duration_min, "sample_count": 0, "source": "standard"}
