from asyncio import Lock
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.dispatch import router as dispatch_router
from .api.duration_forecasts import router as duration_forecast_router
from .api.joint_visits import router as joint_visit_router
from .api.mobile_visits import router as mobile_visits_router
from .api.orchestrator import router as orchestrator_router
from .api.plan_comparisons import router as plan_comparison_router
from .api.route_sheets import router as route_sheets_router
from .api.routes import router as ui_routes_router
from .api.staff_scenarios import router as staff_scenarios_router
from .core.roads import RoadServiceError

APP_TITLE = "Умная диспетчеризация выездных бригад: ПАО Ростелеком"
APP_DESCRIPTION = "Интеллектуальная система планирования маршрутов и реактивного перераспределения нарядов"
ROAD_SERVICE_UNAVAILABLE = "Дорожный граф недоступен. Проверьте запуск OSRM."

app = FastAPI(
    title=APP_TITLE,
    description=APP_DESCRIPTION,
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(orchestrator_router)
app.include_router(dispatch_router)
app.include_router(route_sheets_router)
app.include_router(mobile_visits_router)
app.include_router(staff_scenarios_router)
app.include_router(duration_forecast_router)
app.include_router(joint_visit_router)
app.include_router(plan_comparison_router)
app.include_router(ui_routes_router)

api_write_lock = Lock()


@app.middleware("http")
async def serialize_api_changes(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        async with api_write_lock:
            return await call_next(request)
    response = await call_next(request)
    content_type = response.headers.get("content-type", "").split(";")[0]
    if content_type in {"text/html", "text/javascript", "application/javascript", "text/css"}:
        response.headers["Cache-Control"] = "no-cache"
    return response


OFFICIAL_API_ROUTES = {
    "health",
    "load_reference",
    "load_travel_matrix",
    "load_day",
    "create_initial_plan",
    "get_day_plan",
    "handle_day_event",
}

API_ERROR_CODES = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "UNPROCESSABLE_ENTITY",
    503: "SERVICE_UNAVAILABLE",
}


def is_official_api_request(request: Request) -> bool:
    route = request.scope.get("route")
    return getattr(route, "name", None) in OFFICIAL_API_ROUTES


def official_api_error(status_code: int, message: str, details=None, headers=None, code: str | None = None):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code or API_ERROR_CODES.get(status_code, f"HTTP_{status_code}"),
                "message": message,
                "details": details or [],
            },
        },
        headers=headers,
    )


@app.exception_handler(RoadServiceError)
async def road_service_error(request: Request, _error: RoadServiceError):
    if is_official_api_request(request):
        return official_api_error(503, ROAD_SERVICE_UNAVAILABLE)
    return JSONResponse(
        status_code=503,
        content={
            "detail": ROAD_SERVICE_UNAVAILABLE,
            "error": {"code": "ROAD_SERVICE_UNAVAILABLE", "message": ROAD_SERVICE_UNAVAILABLE, "details": []},
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if is_official_api_request(request):
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            return official_api_error(
                exc.status_code,
                str(detail["message"]),
                details=detail.get("details") or [],
                headers=getattr(exc, "headers", None),
                code=str(detail["code"]),
            )
        message = detail if isinstance(detail, str) else str(detail)
        return official_api_error(exc.status_code, message, headers=getattr(exc, "headers", None))
    message = detail if isinstance(detail, str) else str(detail)
    code = API_ERROR_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": detail,
            "error": {"code": code, "message": message, "details": []},
        },
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError):
    if not is_official_api_request(request):
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
    details = [
        {"field": ".".join(str(part) for part in err.get("loc", ())), "message": str(err.get("msg", ""))}
        for err in exc.errors()
    ]
    if any(err.get("type") == "json_invalid" for err in exc.errors()):
        return official_api_error(400, "Invalid JSON request body", details)
    return official_api_error(422, "Request validation failed", details)


static_dir = Path(__file__).resolve().parents[2] / "frontend"


@app.get("/classic", include_in_schema=False)
def classic_dispatcher():
    return FileResponse(static_dir / "index.html")


@app.get("/m", include_in_schema=False)
def mobile():
    return FileResponse(static_dir / "mobile.html")


@app.get("/api/v1/health_check", include_in_schema=False)
def health_check():
    return {"status": "ok"}


if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=8085, reload=True)
