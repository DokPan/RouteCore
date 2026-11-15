from typing import List, Dict, Optional, Any, Union
from pydantic import BaseModel, Field, ConfigDict
from enum import Enum

class Health(BaseModel):
    status: str = "ok"
    team: str = "RTC Crew Smart Dispatcher"
    version: str = "1.0.0"

class OpeningHours(BaseModel):
    open: str
    close: str

class ServiceCenter(BaseModel):
    service_center_id: str
    name: str
    address: str
    lat: float
    lon: float
    opening_hours: OpeningHours
    restock_duration_min: int = 15

class BreakWindow(BaseModel):
    from_: str = Field(alias="from")
    to: str

    model_config = ConfigDict(populate_by_name=True)

class Shift(BaseModel):
    start: str
    end: str
    break_duration_min: int = 60
    break_window: BreakWindow

class Staff(BaseModel):
    staff_id: str
    display_name: str
    service_center_id: str
    profiles: List[str]
    technologies: List[str]
    height_permit: bool = False
    shift: Shift
    equipment_start: Dict[str, int] = Field(default_factory=dict)
    equipment_capacity: Dict[str, int] = Field(default_factory=dict)
    experience_years: Optional[float] = None

class Building(BaseModel):
    building_id: str
    address: str
    lat: float
    lon: float
    building_type: str = "MKD"

class AccessNode(BaseModel):
    access_node_id: str
    name: str
    type: str = "OLT"
    lat: float
    lon: float
    buildings: List[Building] = Field(default_factory=list)

class WorkType(BaseModel):
    code: str
    name: str
    profiles: List[str]
    duration_min: int
    pgp_relevant: Optional[bool] = None
    requires_height: bool = False
    required_equipment: Dict[str, int] = Field(default_factory=dict)
    sla_hours: Optional[float] = None

class ReferenceData(BaseModel):
    service_centers: List[ServiceCenter]
    staff: List[Staff]
    access_nodes: List[AccessNode]
    work_types: List[WorkType]

class HourlyFactor(BaseModel):
    hour: int
    factor: float

class TravelMatrix(BaseModel):
    matrix_id: str
    points: List[str]
    durations_sec: List[List[int]]
    distances_m: List[List[int]]
    hourly_factors: Optional[List[HourlyFactor]] = None

class LoadResult(BaseModel):
    status: str = "loaded"
    counts: Dict[str, int] = Field(default_factory=dict)

class TimeSlot(BaseModel):
    start: str
    end: str

class Order(BaseModel):
    client_name: Optional[str] = None
    client_phone: Optional[str] = None
    entrance: Optional[str] = None
    floor: Optional[int] = None
    order_id: str
    work_type_code: str
    technology: str
    building_id: str
    address: str
    lat: float
    lon: float
    building_type: str = "MKD"
    requires_height: bool = False
    access_node_id: Optional[str] = None
    district_id: Optional[str] = None
    registered_at: str
    slot: TimeSlot
    latest_start: str
    deadline: str
    required_equipment: Dict[str, int] = Field(default_factory=dict)
    is_repeat: bool = False
    previous_order_id: Optional[str] = None

class DayInput(BaseModel):
    date: str
    now: str
    orders: List[Order]
    absent_staff_ids: Optional[List[str]] = Field(default_factory=list)

    deferred_order_ids: List[str] = Field(default_factory=list)

class PlanRequest(BaseModel):
    time_limit_sec: Optional[int] = 300

class Stop(BaseModel):
    seq: int
    type: str
    location_id: str
    order_id: Optional[str] = None
    arrival: str
    start: str
    end: str
    travel_sec: int = 0
    distance_m: int = 0
    status: str = "PLANNED"
    equipment_after: Optional[Dict[str, int]] = None
    explanation: Optional[str] = None
    planned_duration_min: Optional[int] = None
    actual_start: Optional[str] = None
    actual_end: Optional[str] = None

class RouteTotals(BaseModel):
    visits_count: int = 0
    travel_sec: int = 0
    work_sec: int = 0
    distance_m: int = 0

class Route(BaseModel):
    staff_id: str
    service_center_id: str
    status: str = "ACTIVE"
    unavailable_from: Optional[str] = None
    unavailable_until: Optional[str] = None
    unavailable_reason: Optional[str] = None
    unavailable_comment: Optional[str] = None
    stops: List[Stop] = Field(default_factory=list)
    totals: Optional[RouteTotals] = None

class UnassignedOrder(BaseModel):
    order_id: str
    reason_code: str
    reason_text: str
    suggested_action: str = "RESCHEDULE_TO_NEXT_DAY"
    proposed_slot: Optional[Dict[str, str]] = None
    since: Optional[str] = None

class PgpGroup(BaseModel):
    group_id: str
    access_node_id: str
    order_ids: List[str]
    detected_at: str
    action: str = "FORWARD_TO_STATION"
    comment: Optional[str] = None

class Alert(BaseModel):
    alert_id: str
    created_at: str
    type: str
    status: str = "ACTIVE"
    resolved_at: Optional[str] = None
    order_id: Optional[str] = None
    group_id: Optional[str] = None
    staff_id: Optional[str] = None
    forecast_start: Optional[str] = None
    lateness_min: Optional[int] = None
    suggested_action: str = "REASSIGN"
    suggested_staff_id: Optional[str] = None
    extra_travel_sec: Optional[int] = None
    proposed_slot: Optional[Dict[str, str]] = None
    applied: bool = False
    message: str

class PlanSummary(BaseModel):
    orders_total: int = 0
    visits_planned: int = 0
    unassigned: int = 0
    pgp_orders: int = 0
    cancelled: int = 0
    travel_sec_total: int = 0
    utilization_cv: float = 0.0

class Plan(BaseModel):
    date: str
    plan_version: int = 1
    based_on_event_id: Optional[str] = None
    as_of: str
    computation_ms: int = 0
    routes: List[Route] = Field(default_factory=list)
    unassigned: List[UnassignedOrder] = Field(default_factory=list)
    pgp_groups: List[PgpGroup] = Field(default_factory=list)
    cancelled: List[str] = Field(default_factory=list)
    alerts: List[Alert] = Field(default_factory=list)
    summary: Optional[PlanSummary] = None


class EventBase(BaseModel):
    event_id: str
    type: str
    timestamp: str

class NewOrderEvent(EventBase):
    type: str = "NEW_ORDER"
    order: Order

class OrderCancelledEvent(EventBase):
    type: str = "ORDER_CANCELLED"
    order_id: str
    reason: str = "NOT_CONFIRMED"

class VisitDelayedEvent(EventBase):
    type: str = "VISIT_DELAYED"
    staff_id: str
    order_id: str
    expected_end: str

class OrderTransferredEvent(EventBase):
    type: str = "ORDER_TRANSFERRED"
    order_id: str
    from_staff_id: str
    to_staff_id: str
    reason: Optional[str] = None

class StaffUnavailableEvent(EventBase):
    type: str = "STAFF_UNAVAILABLE"
    staff_id: str
    until: Optional[str] = None
    reason: str = "VEHICLE_BREAKDOWN"

class TimeTickEvent(EventBase):
    type: str = "TIME_TICK"

class EventResult(BaseModel):
    event_id: str
    accepted: bool = True
    duplicate: bool = False
    plan_version: int
    as_of: str
    processing_ms: int = 0
    changed_staff_ids: List[str] = Field(default_factory=list)
    new_alerts: List[Alert] = Field(default_factory=list)
    message: str = ""
