from io import BytesIO

try:
    from xlsxwriter import Workbook
except ImportError:
    Workbook = None
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter


ROUTES = "Маршруты"
EQUIPMENT = "Оборудование"
ROUTE_HEADERS = ("Дата", "Мастер", "СЦ", "Начало", "Конец", "Наряд", "Работа", "Адрес", EQUIPMENT)
STOCK_HEADERS = ("Дата", "Мастер", EQUIPMENT, "На начало", "Вместимость", "Расход", "На конец")


def _collect_rows(sheets):
    route_rows = []
    stock_rows = []
    for sheet in sheets:
        identity = (sheet["date"], sheet["name"])
        for stop in sheet["stops"]:
            equipment = ", ".join(f"{code}: {qty}" for code, qty in stop["equipment"].items() if qty)
            route_rows.append((*identity, sheet["center"], stop["start"], stop["end"], stop["order_id"],
                               stop["work"], stop["address"], equipment))
        for code in sorted(sheet["capacity"].keys() | sheet["consumption"].keys()):
            stock_rows.append((*identity, code, sheet["initial_load"].get(code, 0),
                               sheet["capacity"].get(code, 0), sheet["consumption"].get(code, 0),
                               sheet["remaining"].get(code, 0)))
    return route_rows, stock_rows


def _build_excel_openpyxl(route_rows, stock_rows):
    output = BytesIO()
    book = openpyxl.Workbook()
    routes = book.active
    routes.title = ROUTES
    stock = book.create_sheet(EQUIPMENT)
    header_fill = PatternFill(start_color="5626A3", end_color="5626A3", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")

    for worksheet, headers, rows, widths in (
        (routes, ROUTE_HEADERS, route_rows, (13, 30, 28, 10, 10, 23, 38, 43, 24)),
        (stock, STOCK_HEADERS, stock_rows, (13, 30, 18, 14, 15, 12, 12)),
    ):
        worksheet.append(list(headers))
        for col_idx in range(1, len(headers) + 1):
            cell = worksheet.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
        for row in rows:
            worksheet.append(list(row))
        for index, width in enumerate(widths):
            worksheet.column_dimensions[get_column_letter(index + 1)].width = width
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"

    book.save(output)
    return output.getvalue()


def build_excel(sheets):
    route_rows, stock_rows = _collect_rows(sheets)
    if Workbook is None:
        return _build_excel_openpyxl(route_rows, stock_rows)
    output = BytesIO()
    with Workbook(output, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False}) as book:
        header = book.add_format({"bold": True, "bg_color": "#5626A3", "font_color": "#FFFFFF"})
        routes = book.add_worksheet(ROUTES)
        stock = book.add_worksheet(EQUIPMENT)
        for worksheet, headers, rows, widths in (
            (routes, ROUTE_HEADERS, route_rows, (13, 30, 28, 10, 10, 23, 38, 43, 24)),
            (stock, STOCK_HEADERS, stock_rows, (13, 30, 18, 14, 15, 12, 12)),
        ):
            worksheet.write_row(0, 0, headers, header)
            for index, row in enumerate(rows, 1):
                worksheet.write_row(index, 0, row)
            for index, width in enumerate(widths):
                worksheet.set_column(index, index, width)
            worksheet.freeze_panes(1, 0)
            worksheet.autofilter(0, 0, len(rows), len(headers) - 1)
    return output.getvalue()
