import csv
from io import StringIO

from fastapi import APIRouter, HTTPException, Response

from .dispatch import get_active_dispatch_context
from .route_sheet_excel import build_excel
from ..core.route_sheet import build_route_sheet


router = APIRouter(prefix="/api/v1")
MISSING_ROUTE = "Маршрут мастера не найден."
STOP_COLUMNS = ("Дата", "Мастер", "Начало", "Конец", "Наряд", "Работа", "Адрес", "Оборудование")
EQUIPMENT_COLUMNS = ("Оборудование", "На начало смены", "Вместимость", "На все визиты")


def get_route_sheet(staff_id: str):
    plan, day, reference = get_active_dispatch_context()
    route = next((route for route in plan.routes if route.staff_id == staff_id), None)
    if route is None:
        raise HTTPException(404, MISSING_ROUTE)
    return build_route_sheet(route, day, reference)


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_sheets(sheets, filename, include_equipment=False):
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    if include_equipment:
        sheet = sheets[0]
        writer.writerow(EQUIPMENT_COLUMNS)
        for code in sorted(sheet["capacity"].keys() | sheet["consumption"].keys()):
            writer.writerow([csv_cell(code), sheet["initial_load"].get(code, 0),
                             sheet["capacity"].get(code, 0), sheet["consumption"].get(code, 0)])
        writer.writerow([])
    writer.writerow(STOP_COLUMNS)
    for sheet in sheets:
        for stop in sheet["stops"]:
            equipment = ", ".join(f"{key}: {value}" for key, value in stop["equipment"].items() if value)
            writer.writerow(map(csv_cell, (
                sheet["date"], sheet["name"], stop["start"], stop["end"],
                stop["order_id"], stop["work"], stop["address"], equipment,
            )))
    return Response(output.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/route/{staff_id}.csv")
def export_route_csv(staff_id: str):
    return export_sheets([get_route_sheet(staff_id)], "route.csv", include_equipment=True)


@router.get("/export/routes.csv")
def export_all_routes_csv():
    plan, day, reference = get_active_dispatch_context()
    return export_sheets([build_route_sheet(route, day, reference) for route in plan.routes], "routes.csv")


def excel_response(sheets, filename):
    return Response(build_excel(sheets), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/route/{staff_id}.xlsx")
def export_route_excel(staff_id: str):
    return excel_response([get_route_sheet(staff_id)], "route.xlsx")


@router.get("/export/routes.xlsx")
def export_all_routes_excel():
    plan, day, reference = get_active_dispatch_context()
    return excel_response([build_route_sheet(route, day, reference) for route in plan.routes], "routes.xlsx")
