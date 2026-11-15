"""
Генератор масштабного реалистичного набора данных для хакатона Ростелеком.
Соответствует официальной схеме api_spec.yaml (Екатеринбург, 40 сотрудников, 5 районов, ~360 заявок).
"""
import json
import random
import math
from pathlib import Path

random.seed(42)

DISTRICTS = [
    {
        "id": "SC-01",
        "name": "СЦ «Север» (Уралмаш / Эльмаш)",
        "address": "г. Екатеринбург, ул. Баумана, 2",
        "lat": 56.8890,
        "lon": 60.6120,
        "streets": ["Космонавтов", "Баумана", "Победы", "Ильича", "Орджоникидзе", "Машиностроителей", "Кузнецова", "Кировградская"]
    },
    {
        "id": "SC-02",
        "name": "СЦ «Центр» (Ленинский / Кировский)",
        "address": "г. Екатеринбург, ул. Малышева, 46",
        "lat": 56.8335,
        "lon": 60.6010,
        "streets": ["Ленина", "Малышева", "8 Марта", "Луначарского", "Радищева", "Куйбышева", "Горького", "Карла Либкнехта"]
    },
    {
        "id": "SC-03",
        "name": "СЦ «Запад» (ВИЗ / Заречный)",
        "address": "г. Екатеринбург, ул. Халтурина, 55",
        "lat": 56.8520,
        "lon": 60.5390,
        "streets": ["Халтурина", "Крауля", "Татищева", "Готвальда", "Опалихинская", "Бебеля", "Кирова", "Викулова"]
    },
    {
        "id": "SC-04",
        "name": "СЦ «Восток» (Пионерский / ЖБИ)",
        "address": "г. Екатеринбург, ул. Сулимова, 50",
        "lat": 56.8580,
        "lon": 60.6480,
        "streets": ["Сулимова", "Уральская", "Учителей", "Сиреневый бульвар", "40-летия Комсомола", "Сыромолотова", "Блюхера", "Советская"]
    },
    {
        "id": "SC-05",
        "name": "СЦ «Юг» (Ботаника / Чкаловский)",
        "address": "г. Екатеринбург, ул. 8 Марта, 194",
        "lat": 56.7990,
        "lon": 60.6150,
        "streets": ["8 Марта", "Белинского", "Шварца", "Родонитовая", "Академика Сахарова", "Титова", "Щербакова", "Крестинского"]
    }
]

LAST_NAMES = ["Морозов", "Иванов", "Петров", "Смирнов", "Соколов", "Кузнецов", "Попов", "Васильев", "Михайлов", "Новиков", "Федоров", "Волков", "Алексеев", "Лебедев", "Семенов", "Егоров", "Павлов", "Козлов", "Степанов", "Николаев"]
FIRST_NAMES = ["Роман", "Александр", "Сергей", "Дмитрий", "Андрей", "Алексей", "Максим", "Евгений", "Михаил", "Иван", "Денис", "Павел", "Артем", "Владимир", "Антон"]

WORK_TYPES = [
    {
        "code": "INST_PON_MKD",
        "name": "Инсталляция ШПД/IPTV, МКД, PON",
        "profiles": ["INSTALL"],
        "duration_min": 90,
        "requires_height": False,
        "required_equipment": {"ONT": 1, "ROUTER": 1, "STB": 1},
        "technologies": ["PON"]
    },
    {
        "code": "INST_FTTB_MKD",
        "name": "Инсталляция ШПД/IPTV, МКД, FTTB",
        "profiles": ["INSTALL"],
        "duration_min": 60,
        "requires_height": False,
        "required_equipment": {"ONT": 0, "ROUTER": 1, "STB": 1},
        "technologies": ["FTTB"]
    },
    {
        "code": "INST_PON_PRIVATE_VLS",
        "name": "Инсталляция, частный сектор, с ВЛС",
        "profiles": ["INSTALL"],
        "duration_min": 180,
        "requires_height": True,
        "required_equipment": {"ONT": 1, "ROUTER": 1, "STB": 0},
        "technologies": ["PON"]
    },
    {
        "code": "ADD_SERVICE",
        "name": "Доподключение услуги (IPTV, Wi-Fi)",
        "profiles": ["INSTALL"],
        "duration_min": 40,
        "requires_height": False,
        "required_equipment": {"ONT": 0, "ROUTER": 1, "STB": 1},
        "technologies": ["PON", "FTTB"]
    },
    {
        "code": "SURVEY_PRIVATE",
        "name": "Обследование (частный сектор)",
        "profiles": ["INSTALL", "LTP3"],
        "duration_min": 45,
        "requires_height": False,
        "required_equipment": {"ONT": 0, "ROUTER": 0, "STB": 0},
        "technologies": ["PON", "XDSL"]
    },
    {
        "code": "REPAIR",
        "name": "Устранение неисправности (обращение абонента)",
        "profiles": ["LTP3"],
        "duration_min": 60,
        "requires_height": False,
        "required_equipment": {"ONT": 0, "ROUTER": 1, "STB": 0},
        "technologies": ["PON", "FTTB", "XDSL", "DOCSIS"]
    },
    {
        "code": "DEGRADATION",
        "name": "Устранение деградации",
        "profiles": ["LTP3"],
        "duration_min": 45,
        "requires_height": False,
        "required_equipment": {"ONT": 0, "ROUTER": 0, "STB": 0},
        "technologies": ["PON", "FTTB"]
    },
    {
        "code": "DISMANTLE",
        "name": "Демонтаж АО",
        "profiles": ["LTP3"],
        "duration_min": 30,
        "requires_height": False,
        "required_equipment": {"ONT": 0, "ROUTER": 0, "STB": 0},
        "technologies": ["PON", "FTTB", "XDSL", "DOCSIS"]
    }
]

def generate():
    out_dir = Path("data/official_dataset")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Сервисные центры
    service_centers = []
    for d in DISTRICTS:
        service_centers.append({
            "service_center_id": d["id"],
            "name": d["name"],
            "address": d["address"],
            "lat": d["lat"],
            "lon": d["lon"],
            "opening_hours": {"open": "08:00", "close": "22:00"},
            "restock_duration_min": 15
        })

    # 2. Сотрудники (40 сотрудников: 15 инсталляторов, 25 3ЛТП)
    staff = []
    staff_idx = 1
    for d_idx, d in enumerate(DISTRICTS):
        # По 8 сотрудников на район: 3 инсталлятора, 5 3ЛТП
        for i in range(8):
            s_id = f"ST-{staff_idx:03d}"
            is_install = (i < 3)
            profiles = ["INSTALL"] if is_install else ["LTP3"]

            # Имя: первый сотрудник - Морозов Роман
            if staff_idx == 1:
                name = "Морозов Роман"
            else:
                name = f"{LAST_NAMES[(staff_idx + i) % len(LAST_NAMES)]} {FIRST_NAMES[(staff_idx * 2 + i) % len(FIRST_NAMES)]}"

            # Графики смен: половина с 09:00, половина с 12:00
            is_morning = (i % 2 == 0)
            shift_start = "09:00" if is_morning else "12:00"
            shift_end = "18:00" if is_morning else "21:00"
            break_from = "12:00" if is_morning else "15:00"
            break_to = "15:00" if is_morning else "18:00"

            techs = ["PON", "FTTB"] if is_install else ["PON", "FTTB", "XDSL", "DOCSIS"]
            height_permit = (is_install and (i == 0 or i == 2))

            cap = {"ONT": 6, "ROUTER": 6, "STB": 4} if is_install else {"ONT": 3, "ROUTER": 4, "STB": 2}
            start_eq = {"ONT": random.randint(3, 5), "ROUTER": random.randint(3, 5), "STB": random.randint(2, 3)} if is_install else {"ONT": 2, "ROUTER": 2, "STB": 1}

            staff.append({
                "staff_id": s_id,
                "display_name": f"{name} ({d['name'].split()[1]})",
                "service_center_id": d["id"],
                "profiles": profiles,
                "technologies": techs,
                "height_permit": height_permit,
                "shift": {
                    "start": shift_start,
                    "end": shift_end,
                    "break_duration_min": 60,
                    "break_window": {
                        "from": break_from,
                        "to": break_to
                    }
                },
                "equipment_start": start_eq,
                "equipment_capacity": cap,
                "experience_years": round(random.uniform(1.5, 9.0), 1)
            })
            staff_idx += 1

    # 3. Узлы доступа и здания (по 10 узлов на район, к каждому по 6-8 зданий)
    access_nodes = []
    all_buildings = []
    points = [sc["service_center_id"] for sc in service_centers]
    bld_counter = 1

    for d_idx, d in enumerate(DISTRICTS):
        for n_i in range(1, 11):
            an_id = f"AN-{d_idx+1:02d}{n_i:02d}"
            an_street = random.choice(d["streets"])
            an_lat = round(d["lat"] + random.uniform(-0.02, 0.02), 4)
            an_lon = round(d["lon"] + random.uniform(-0.025, 0.025), 4)

            bld_list = []
            for b_i in range(1, random.randint(6, 9)):
                b_id = f"B-{bld_counter:05d}"
                b_num = b_i * 3 + random.randint(1, 5)
                b_addr = f"г. Екатеринбург, ул. {an_street}, д. {b_num}"
                b_lat = round(an_lat + random.uniform(-0.005, 0.005), 4)
                b_lon = round(an_lon + random.uniform(-0.005, 0.005), 4)
                # Защита от попадания в Верх-Исетское водохранилище и Городской пруд
                if 56.820 <= b_lat <= 56.890 and b_lon < 60.548:
                    b_lon = round(60.5520 + random.uniform(0.002, 0.020), 4)
                elif 56.837 <= b_lat <= 56.848 and 60.596 <= b_lon <= 60.6065:
                    b_lon = round(60.6080 + random.uniform(0.002, 0.008), 4)
                b_type = "PRIVATE" if ("частный" in an_street.lower() or b_i == 7) else "MKD"

                bld_obj = {
                    "building_id": b_id,
                    "address": b_addr,
                    "lat": b_lat,
                    "lon": b_lon,
                    "building_type": b_type,
                    "district_id": d["id"],
                    "access_node_id": an_id
                }
                bld_list.append(bld_obj)
                all_buildings.append(bld_obj)
                points.append(b_id)
                bld_counter += 1

            access_nodes.append({
                "access_node_id": an_id,
                "name": f"Узел {an_id} ({an_street})",
                "type": "OLT" if n_i % 3 == 0 else "SWITCH",
                "lat": an_lat,
                "lon": an_lon,
                "buildings": [{"building_id": b["building_id"], "address": b["address"], "lat": b["lat"], "lon": b["lon"], "building_type": b["building_type"]} for b in bld_list]
            })

    # Справочные данные
    reference_data = {
        "service_centers": service_centers,
        "staff": staff,
        "access_nodes": access_nodes,
        "work_types": [
            {
                "code": wt["code"],
                "name": wt["name"],
                "profiles": wt["profiles"],
                "duration_min": wt["duration_min"],
                "requires_height": wt["requires_height"],
                "required_equipment": wt["required_equipment"],
                "sla_hours": 24 if "REPAIR" in wt["code"] else 72
            } for wt in WORK_TYPES
        ]
    }
    with open(out_dir / "ReferenceData.json", "w", encoding="utf-8") as f:
        json.dump(reference_data, f, ensure_ascii=False, indent=2)

    # 4. Наряды на день (350 заявок на 2026-10-26)
    date_str = "2026-10-26"
    slots = [
        ("09:00", "11:00"),
        ("11:00", "13:00"),
        ("13:00", "15:00"),
        ("15:00", "17:00"),
        ("17:00", "19:00"),
        ("19:00", "21:00")
    ]

    orders = []
    ord_id = 1

    # Заведомая групповая авария ПГП на Уралмаше (узел AN-0103)
    pgp_an_id = "AN-0103"
    pgp_buildings = [b for b in all_buildings if b["access_node_id"] == pgp_an_id][:5]
    for pb in pgp_buildings:
        orders.append({
            "order_id": f"ORD-{date_str.replace('-', '')}-{ord_id:04d}",
            "work_type_code": "REPAIR",
            "technology": "FTTB",
            "building_id": pb["building_id"],
            "address": pb["address"],
            "lat": pb["lat"],
            "lon": pb["lon"],
            "building_type": pb["building_type"],
            "requires_height": False,
            "access_node_id": pgp_an_id,
            "registered_at": f"{date_str}T08:15:00+05:00",
            "slot": {
                "start": f"{date_str}T09:00:00+05:00",
                "end": f"{date_str}T11:00:00+05:00"
            },
            "latest_start": f"{date_str}T11:00:00+05:00",
            "deadline": f"{date_str}T20:15:00+05:00",
            "required_equipment": {"ONT": 0, "ROUTER": 1, "STB": 0},
            "is_repeat": False,
            "previous_order_id": None
        })
        ord_id += 1

    # Основные наряды (распределены по всем зданиям и районам)
    shuffled_blds = list(all_buildings)
    random.shuffle(shuffled_blds)

    for i in range(355):
        b = shuffled_blds[i % len(shuffled_blds)]
        slot_s, slot_e = random.choice(slots)

        # Выбор типа работы
        is_inst = (random.random() < 0.45)
        if is_inst:
            candidates = [w for w in WORK_TYPES if "INSTALL" in w["profiles"]]
        else:
            candidates = [w for w in WORK_TYPES if "LTP3" in w["profiles"]]
        wt = random.choice(candidates)

        tech = random.choice(wt["technologies"])
        is_repeat = (not is_inst and random.random() < 0.12)  # повторные аварии с весом 2!

        # latest_start: для инсталляций строго +10 минут от начала слота!
        if is_inst:
            s_h, s_m = map(int, slot_s.split(":"))
            l_m = s_m + 10
            l_h = s_h + (l_m // 60)
            l_m = l_m % 60
            latest_start = f"{date_str}T{l_h:02d}:{l_m:02d}:00+05:00"
        else:
            latest_start = f"{date_str}T{slot_e}:00+05:00"

        req_eq = dict(wt["required_equipment"])

        orders.append({
            "order_id": f"ORD-{date_str.replace('-', '')}-{ord_id:04d}",
            "work_type_code": wt["code"],
            "technology": tech,
            "building_id": b["building_id"],
            "address": b["address"],
            "lat": b["lat"],
            "lon": b["lon"],
            "building_type": b["building_type"],
            "requires_height": wt["requires_height"],
            "access_node_id": b["access_node_id"],
            "registered_at": f"{date_str}T08:{random.randint(0, 59):02d}:00+05:00",
            "slot": {
                "start": f"{date_str}T{slot_s}:00+05:00",
                "end": f"{date_str}T{slot_e}:00+05:00"
            },
            "latest_start": latest_start,
            "deadline": f"2026-10-27T18:00:00+05:00",
            "required_equipment": req_eq,
            "is_repeat": is_repeat,
            "previous_order_id": f"ORD-20261021-{random.randint(100, 999)}" if is_repeat else None
        })
        ord_id += 1

    day_input = {
        "date": date_str,
        "now": f"{date_str}T08:30:00+05:00",
        "orders": orders,
        "absent_staff_ids": ["ST-012"]  # 1 сотрудник в отпуске/на больничном
    }
    with open(out_dir / "DayInput.json", "w", encoding="utf-8") as f:
        json.dump(day_input, f, ensure_ascii=False, indent=2)

    # 5. TravelMatrix для ключевых точек
    # Для быстродействия матрица генерируется для точек, а для остальных используется быстрый расчет
    point_subset = points[:80]
    n_pts = len(point_subset)
    durations = [[0]*n_pts for _ in range(n_pts)]
    distances = [[0]*n_pts for _ in range(n_pts)]

    # Координаты точек
    coords = {}
    for sc in service_centers:
        coords[sc["service_center_id"]] = (sc["lat"], sc["lon"])
    for b in all_buildings:
        coords[b["building_id"]] = (b["lat"], b["lon"])

    for i in range(n_pts):
        p1 = point_subset[i]
        c1 = coords.get(p1, (56.8389, 60.6057))
        for j in range(n_pts):
            if i == j:
                continue
            p2 = point_subset[j]
            c2 = coords.get(p2, (56.8389, 60.6057))
            d_km = math.sqrt((c1[0]-c2[0])**2 + ((c1[1]-c2[1])*0.55)**2) * 111.0 * 1.35
            sec = max(180, int(round((d_km / 30.0) * 3600)))
            durations[i][j] = sec
            distances[i][j] = int(round(d_km * 1000))

    travel_matrix = {
        "matrix_id": "ekb-metro-2026-v1",
        "points": point_subset,
        "durations_sec": durations,
        "distances_m": distances,
        "hourly_factors": [
            {"hour": 8, "factor": 1.35},
            {"hour": 9, "factor": 1.25},
            {"hour": 13, "factor": 1.10},
            {"hour": 17, "factor": 1.40},
            {"hour": 18, "factor": 1.35}
        ]
    }
    with open(out_dir / "TravelMatrix.json", "w", encoding="utf-8") as f:
        json.dump(travel_matrix, f, ensure_ascii=False, indent=2)

    print(f"Сгенерирован полный датасет: {len(staff)} сотрудников, {len(service_centers)} СЦ, {len(access_nodes)} узлов, {len(orders)} заявок!")

if __name__ == "__main__":
    generate()

