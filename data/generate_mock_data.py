"""
Генератор реалистичных тестовых данных для кейса "Умная диспетчеризация выездных бригад" (Екатеринбург).
Соответствует спецификации из Кейс №05 Ростелеком (про).pdf.
"""
import json
import csv
import random
from pathlib import Path

# Координатные границы Екатеринбурга
LAT_MIN, LAT_MAX = 56.780, 56.910
LON_MIN, LON_MAX = 60.520, 60.720

def generate_all():
    mock_dir = Path("data/mock")
    mock_dir.mkdir(parents=True, exist_ok=True)
    random.seed(42)

    # 1. Справочник типов работ (work_types.csv)
    work_types = [
        {
            "id": "inst_mdf_pon",
            "name": "Инсталляция ШПД/IPTV, МКД, PON",
            "profile": "installation",
            "norm_min": 90,
            "sla": "Визит в слот; подключение <= 3 раб. дней с заявки",
            "technologies": "PON",
            "requires_height": "false",
            "eq_ont": 1,
            "eq_router": 1,
            "eq_tv_box": 1
        },
        {
            "id": "inst_mdf_fttb",
            "name": "Инсталляция ШПД/IPTV, МКД, FTTB",
            "profile": "installation",
            "norm_min": 60,
            "sla": "Визит в слот; подключение <= 3 раб. дней с заявки",
            "technologies": "FTTB",
            "requires_height": "false",
            "eq_ont": 0,
            "eq_router": 1,
            "eq_tv_box": 1
        },
        {
            "id": "inst_private_vls",
            "name": "Инсталляция, частный сектор, с ВЛС",
            "profile": "installation",
            "norm_min": 180,
            "sla": "Визит в слот; подключение <= 5 раб. дней с заявки",
            "technologies": "PON",
            "requires_height": "true",
            "eq_ont": 1,
            "eq_router": 1,
            "eq_tv_box": 0
        },
        {
            "id": "add_service",
            "name": "Доподключение услуги (IPTV, Wi-Fi-роутер)",
            "profile": "installation",
            "norm_min": 40,
            "sla": "Визит в слот",
            "technologies": "PON;FTTB",
            "requires_height": "false",
            "eq_ont": 0,
            "eq_router": 1,
            "eq_tv_box": 1
        },
        {
            "id": "survey_private",
            "name": "Обследование (частный сектор)",
            "profile": "both",
            "norm_min": 45,
            "sla": "<= 2 раб. дней",
            "technologies": "PON;xDSL",
            "requires_height": "false",
            "eq_ont": 0,
            "eq_router": 0,
            "eq_tv_box": 0
        },
        {
            "id": "troubleshoot_user",
            "name": "Устранение неисправности (обращение абонента)",
            "profile": "3ltp",
            "norm_min": 60,
            "sla": "<= 24 ч с регистрации; повторное <= 12 ч",
            "technologies": "PON;FTTB;xDSL;DOCSIS",
            "requires_height": "false",
            "eq_ont": 0,
            "eq_router": 1,
            "eq_tv_box": 0
        },
        {
            "id": "troubleshoot_degrade",
            "name": "Устранение деградации, выявленной системой мониторинга качества",
            "profile": "3ltp",
            "norm_min": 45,
            "sla": "<= 48 ч с регистрации",
            "technologies": "PON;FTTB",
            "requires_height": "false",
            "eq_ont": 0,
            "eq_router": 0,
            "eq_tv_box": 0
        },
        {
            "id": "dismantle_ao",
            "name": "Демонтаж АО",
            "profile": "3ltp",
            "norm_min": 30,
            "sla": "<= 5 раб. дней",
            "technologies": "PON;FTTB;xDSL;DOCSIS",
            "requires_height": "false",
            "eq_ont": 0,
            "eq_router": 0,
            "eq_tv_box": 0
        }
    ]

    with open(mock_dir / "work_types.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(work_types[0].keys()))
        writer.writeheader()
        writer.writerows(work_types)

    # 2. Сервисные центры (service_centers.json) - 5 СЦ в Екатеринбурге
    service_centers = [
        {"id": "SC_1", "name": "СЦ Центр (ул. Малышева, 46)", "lat": 56.8335, "lon": 60.6010, "work_hours": "08:00-22:00"},
        {"id": "SC_2", "name": "СЦ ВИЗ (ул. Халтурина, 55)", "lat": 56.8520, "lon": 60.5390, "work_hours": "08:00-22:00"},
        {"id": "SC_3", "name": "СЦ Уралмаш (ул. Баумана, 2)", "lat": 56.8890, "lon": 60.6120, "work_hours": "08:00-22:00"},
        {"id": "SC_4", "name": "СЦ Пионерский (ул. Сулимова, 50)", "lat": 56.8580, "lon": 60.6480, "work_hours": "08:00-22:00"},
        {"id": "SC_5", "name": "СЦ Ботаника (ул. 8 Марта, 194)", "lat": 56.7990, "lon": 60.6150, "work_hours": "08:00-22:00"}
    ]
    with open(mock_dir / "service_centers.json", "w", encoding="utf-8") as f:
        json.dump(service_centers, f, ensure_ascii=False, indent=2)

    # 3. Узлы доступа (access_nodes.json) - ОЛТ и коммутаторы
    access_nodes = []
    streets = ["Ленина", "Малышева", "Космонавтов", "8 Марта", "Белинского", "Крауля", "Луначарского", "Шевченко", "Бажова", "Мира"]
    for i in range(1, 26):
        node_id = f"AN_{i:03d}"
        street = random.choice(streets)
        node_type = "OLT" if i % 4 == 0 else "Switch"
        lat = round(random.uniform(LAT_MIN, LAT_MAX), 6)
        lon = round(random.uniform(LON_MIN, LON_MAX), 6)
        buildings = [f"ул. {street}, д. {j}" for j in range(1 + (i*3)%15, 6 + (i*3)%15)]
        access_nodes.append({
            "id": node_id,
            "type": node_type,
            "address": f"ул. {street}, д. {i * 2}",
            "lat": lat,
            "lon": lon,
            "served_buildings": buildings
        })
    with open(mock_dir / "access_nodes.json", "w", encoding="utf-8") as f:
        json.dump(access_nodes, f, ensure_ascii=False, indent=2)

    # 4. Выездные сотрудники (staff.json) - 40 человек (28 3ЛТП, 12 инсталляторов)
    staff = []
    all_techs = ["PON", "FTTB", "xDSL", "DOCSIS"]
    first_names = ["Александр", "Дмитрий", "Максим", "Сергей", "Андрей", "Алексей", "Иван", "Роман", "Евгений", "Михаил", "Денис", "Павел"]
    last_names = ["Иванов", "Смирнов", "Кузнецов", "Попов", "Васильев", "Петров", "Соколов", "Михайлов", "Новиков", "Федоров", "Морозов", "Волков"]

    for idx in range(1, 41):
        is_installer = (idx <= 12)
        profile = "installation" if is_installer else "3ltp"
        sc = service_centers[(idx - 1) % len(service_centers)]
        shift_type = "morning" if idx % 2 == 1 else "evening"
        
        shift_start = "09:00" if shift_type == "morning" else "12:00"
        shift_end = "18:00" if shift_type == "morning" else "21:00"
        lunch_start = "13:00" if shift_type == "morning" else "16:00"
        lunch_end = "14:00" if shift_type == "morning" else "17:00"

        # Навыки и допуски
        if is_installer:
            techs = ["PON", "FTTB"] if idx % 3 != 0 else ["PON", "FTTB", "xDSL"]
            has_height = (idx % 2 == 1)  # половина инсталляторов имеют допуск на высоту
            stock = {"ONT": random.randint(3, 5), "router": random.randint(3, 5), "tv_box": random.randint(2, 4)}
        else:
            techs = all_techs if idx % 2 == 0 else ["PON", "FTTB", "xDSL"]
            has_height = (idx % 4 == 0)
            stock = {"ONT": random.randint(1, 2), "router": random.randint(2, 3), "tv_box": random.randint(1, 2)}

        name = f"{random.choice(last_names)} {random.choice(first_names)}"
        staff.append({
            "id": f"EMP_{idx:03d}",
            "name": name,
            "profile": profile,
            "assigned_sc_id": sc["id"],
            "shift_start": shift_start,
            "shift_end": shift_end,
            "lunch_start": lunch_start,
            "lunch_end": lunch_end,
            "technologies": techs,
            "has_height_permit": has_height,
            "equipment_stock": stock
        })
    with open(mock_dir / "staff.json", "w", encoding="utf-8") as f:
        json.dump(staff, f, ensure_ascii=False, indent=2)

    # 5. Наряды на смену (orders_train.json) - 60 репрезентативных нарядов на 1 день
    # Включает кластер для подозрения на групповую проблему (ПГП)!
    orders = []
    slots = ["09:00-11:00", "11:00-13:00", "13:00-15:00", "15:00-17:00", "17:00-19:00", "19:00-21:00"]
    
    # Создаем подозрение на ПГП: узел AN_004 имеет 4 аварийных заявки в короткий промежуток!
    pgp_node = access_nodes[3]  # AN_004
    for p_i in range(1, 5):
        orders.append({
            "id": f"ORD_PGP_{p_i:02d}",
            "order_type": "troubleshoot_user",
            "technology": "FTTB",
            "address": f"{pgp_node['served_buildings'][p_i % len(pgp_node['served_buildings'])]}",
            "lat": round(pgp_node["lat"] + random.uniform(-0.001, 0.001), 6),
            "lon": round(pgp_node["lon"] + random.uniform(-0.001, 0.001), 6),
            "registration_time": f"2026-11-13T09:{10 + p_i * 3:02d}:00",
            "time_slot": "11:00-13:00",
            "required_equipment": {"ONT": 0, "router": 1, "tv_box": 0},
            "is_repeat": False,
            "access_node_id": pgp_node["id"],
            "requires_height": False
        })

    # Обычные заявки (инсталляции и ремонты)
    for ord_idx in range(1, 56):
        node = random.choice(access_nodes)
        is_inst = (ord_idx % 2 == 1)
        if is_inst:
            wt = random.choice([w for w in work_types if w["profile"] == "installation"])
        else:
            wt = random.choice([w for w in work_types if w["profile"] in ["3ltp", "both"]])
        
        tech = random.choice(wt["technologies"].split(";"))
        slot = random.choice(slots)
        is_repeat = (ord_idx % 8 == 0)
        
        orders.append({
            "id": f"ORD_{ord_idx:03d}",
            "order_type": wt["id"],
            "technology": tech,
            "address": f"ул. {random.choice(streets)}, д. {random.randint(1, 120)}",
            "lat": round(node["lat"] + random.uniform(-0.015, 0.015), 6),
            "lon": round(node["lon"] + random.uniform(-0.015, 0.015), 6),
            "registration_time": f"2026-11-13T08:{random.randint(0, 59):02d}:00",
            "time_slot": slot,
            "required_equipment": {
                "ONT": wt["eq_ont"],
                "router": wt["eq_router"],
                "tv_box": wt["eq_tv_box"]
            },
            "is_repeat": is_repeat,
            "access_node_id": node["id"],
            "requires_height": (wt["requires_height"] == "true")
        })

    with open(mock_dir / "orders_train.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)

    # 6. События в течение смены (events_example.json)
    events = [
        {
            "id": "EVT_001",
            "event_type": "cancel_visit",
            "timestamp": "2026-11-13T10:15:00",
            "order_id": "ORD_003",
            "reason": "Абонент не подтвердил визит при звонке за 45 минут",
            "parameters": {}
        },
        {
            "id": "EVT_002",
            "event_type": "tech_delay",
            "timestamp": "2026-11-13T10:30:00",
            "staff_id": "EMP_001",
            "delay_minutes": 35,
            "reason": "Задержка на объекте: сложный монтаж кабельного ввода",
            "parameters": {"extra_minutes": 35}
        },
        {
            "id": "EVT_003",
            "event_type": "new_order",
            "timestamp": "2026-11-13T11:00:00",
            "order": {
                "id": "ORD_URGENT_999",
                "order_type": "troubleshoot_user",
                "technology": "PON",
                "address": "ул. Малышева, д. 51",
                "lat": 56.8350,
                "lon": 60.6120,
                "registration_time": "2026-11-13T10:55:00",
                "time_slot": "13:00-15:00",
                "required_equipment": {"ONT": 0, "router": 1, "tv_box": 0},
                "is_repeat": True,
                "access_node_id": "AN_001",
                "requires_height": False
            }
        },
        {
            "id": "EVT_004",
            "event_type": "tech_unavailable",
            "timestamp": "2026-11-13T12:30:00",
            "staff_id": "EMP_005",
            "reason": "Поломка служебного автомобиля на перекрестке",
            "parameters": {}
        }
    ]
    with open(mock_dir / "events_example.json", "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

    print("Все тестовые данные сгенерированы успешно в data/mock/!")

if __name__ == "__main__":
    generate_all()

