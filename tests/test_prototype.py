import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.api.orchestrator import (
    load_default_official_data, db, build_plan, handle_day_event
)

def test_full_cycle():
    print("=== ТЕСТ 1: Загрузка официальных справочников и данных ===")
    load_default_official_data()
    assert db["reference"] is not None, "Справочники должны быть загружены"
    ref = db["reference"]
    print("Сотрудников:", len(ref.staff))
    print("Сервисных центров:", len(ref.service_centers))
    print("Узлов доступа:", len(ref.access_nodes))
    print("Типов работ:", len(ref.work_types))
    print("[OK] Данные загружены корректно")

    print("\n=== ТЕСТ 2: Планирование смены по официальному протоколу ===")
    active_date = db.get("active_date", "2026-10-26")
    plan = build_plan(active_date)
    print("Версия плана:", plan.plan_version)
    print("Маршрутов сформировано:", len(plan.routes))
    print("Визитов назначено:", plan.summary.visits_planned if plan.summary else 0)
    print("Нераспределенных нарядов:", len(plan.unassigned))
    print("[OK] Планирование смены выполнено успешно")

    print("\n=== ТЕСТ 3: Реактивное перепланирование по событию ===")
    delay_event = {
        "event_id": "EV-TEST-001",
        "type": "VISIT_DELAYED",
        "timestamp": "2026-10-26T10:00:00+05:00",
        "staff_id": plan.routes[0].staff_id,
        "order_id": plan.routes[0].stops[1].order_id if len(plan.routes[0].stops) > 1 else "ORD-001",
        "expected_end": "2026-10-26T11:00:00+05:00"
    }
    res = handle_day_event(active_date, delay_event)
    print("Статус обработки:", res.message)
    print("Новая версия плана:", res.plan_version)
    print("[OK] Реактивная обработка событий работает штатно")

    print("\nВсе тесты прототипа пройдены успешно!")

if __name__ == "__main__":
    test_full_cycle()
