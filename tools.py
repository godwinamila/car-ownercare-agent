"""Owner Care tools backed by the static demo data in data/owner_care_data.json.

Each tool is a plain Python function that returns a JSON-serialisable dict.
TOOL_SCHEMAS describes them to the model; execute_tool() dispatches a tool call.
Data is loaded into memory at start-up, so bookings and cases created during a
demo last until the process restarts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DATA_FILE = Path(os.getenv("OWNER_CARE_DATA_FILE", Path(__file__).parent / "data" / "owner_care_data.json"))

SLOT_TIMES_WEEKDAY = ["09:00", "10:30", "13:00", "14:30", "16:00"]
SLOT_TIMES_SATURDAY = ["09:00", "10:30"]
TYRE_ROTATION_KM = 10000
CABIN_FILTER_MONTHS = 12


def today() -> date:
    """Current date; set DEMO_TODAY=YYYY-MM-DD to pin it for a repeatable demo."""
    override = os.getenv("DEMO_TODAY")
    return date.fromisoformat(override) if override else date.today()


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) - (1 if end.day < start.day else 0)


def _version_tuple(version: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", version))


class OwnerCareStore:
    """In-memory copy of the demo data with a lock around writes."""

    def __init__(self, path: Path = DATA_FILE):
        self._lock = threading.Lock()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self.data = copy.deepcopy(raw)
        self._next_id = {"APT": 50100, "CASE": 70100, "RSA": 90100}
        # Seeded appointments use days_from_today so the demo never goes stale.
        for apt in self.data["appointments"]:
            if "days_from_today" in apt:
                apt["date"] = (today() + timedelta(days=apt.pop("days_from_today"))).isoformat()
        self.roadside_requests: List[Dict[str, Any]] = []

    def new_id(self, prefix: str) -> str:
        with self._lock:
            self._next_id[prefix] += 1
            return f"{prefix}-{self._next_id[prefix]}"

    # Lookups ---------------------------------------------------------------

    def owner(self, owner_id: str) -> Optional[Dict[str, Any]]:
        return next((o for o in self.data["owners"] if o["owner_id"].lower() == owner_id.strip().lower()), None)

    def vehicle(self, vin: str) -> Optional[Dict[str, Any]]:
        return next((v for v in self.data["vehicles"] if v["vin"].lower() == vin.strip().lower()), None)

    def center(self, center_id: str) -> Optional[Dict[str, Any]]:
        return next((c for c in self.data["service_centers"] if c["center_id"].lower() == center_id.strip().lower()), None)

    def service_codes(self) -> Dict[str, Dict[str, Any]]:
        return {s["code"]: s for s in self.data["service_catalog"]}


store = OwnerCareStore()


class ToolError(Exception):
    """Raised for bad input; returned to the model as an error tool result."""


def _owned_vehicle(owner_id: str, vin: str) -> Dict[str, Any]:
    owner = store.owner(owner_id)
    if not owner:
        raise ToolError(f"No owner found with ID {owner_id}.")
    vehicle = store.vehicle(vin)
    if not vehicle or vehicle["owner_id"] != owner["owner_id"]:
        raise ToolError(f"Vehicle {vin} is not registered to owner {owner['owner_id']}.")
    return vehicle


def _vehicle_summary(v: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "vin": v["vin"],
        "model": v["model"],
        "trim": v["trim"],
        "model_year": v["model_year"],
        "color": v["color"],
        "license_plate": v["license_plate"],
    }


# Tool implementations ------------------------------------------------------


def identify_owner(identifier: str) -> Dict[str, Any]:
    ident = identifier.strip().lower()
    digits = re.sub(r"\D", "", ident)
    for owner in store.data["owners"]:
        owner_vehicles = [store.vehicle(vin) for vin in owner["vehicles"]]
        matches = (
            ident in (owner["owner_id"].lower(), owner["email"].lower())
            or (len(digits) >= 7 and digits == re.sub(r"\D", "", owner["phone"]))
            or any(ident in (v["vin"].lower(), v["license_plate"].lower()) for v in owner_vehicles)
        )
        if matches:
            return {
                "owner_id": owner["owner_id"],
                "name": owner["name"],
                "email": owner["email"],
                "phone": owner["phone"],
                "city": owner["city"],
                "country": owner["country"],
                "membership_tier": owner["membership_tier"],
                "preferred_service_center": owner["preferred_service_center"],
                "vehicles": [_vehicle_summary(v) for v in owner_vehicles],
            }
    raise ToolError("No owner matches that owner ID, email, phone number, VIN or licence plate.")


def get_vehicle_status(owner_id: str, vin: str) -> Dict[str, Any]:
    vehicle = _owned_vehicle(owner_id, vin)
    return {**_vehicle_summary(vehicle), "battery_kwh": vehicle["battery_kwh"], **vehicle["telemetry"]}


def get_service_history(owner_id: str, vin: str) -> Dict[str, Any]:
    vehicle = _owned_vehicle(owner_id, vin)
    records = sorted(
        (r for r in store.data["service_history"] if r["vin"] == vehicle["vin"]),
        key=lambda r: r["date"],
        reverse=True,
    )
    return {"vin": vehicle["vin"], "model": vehicle["model"], "records": records}


def get_maintenance_recommendations(owner_id: str, vin: str) -> Dict[str, Any]:
    vehicle = _owned_vehicle(owner_id, vin)
    model = store.data["models"][vehicle["model"]]
    odometer = vehicle["telemetry"]["odometer_km"]
    last = vehicle["last_service"] or {"date": vehicle["delivery_date"], "odometer_km": 0}
    last_date = date.fromisoformat(last["date"])
    months_since = _months_between(last_date, today())
    km_since = odometer - last["odometer_km"]

    items = []

    def add(item: str, status: str, reason: str, service_code: str) -> None:
        items.append({"item": item, "status": status, "reason": reason, "service_code": service_code})

    km_left = model["service_interval_km"] - km_since
    months_left = model["service_interval_months"] - months_since
    if km_left <= 0 or months_left <= 0:
        add("Scheduled service", "Overdue", f"{km_since} km / {months_since} months since last service "
            f"(interval {model['service_interval_km']} km or {model['service_interval_months']} months).", "ANNUAL")
    elif km_left <= 3000 or months_left <= 3:
        add("Scheduled service", "Due soon", f"{km_left} km or {months_left} months remaining.", "ANNUAL")
    else:
        add("Scheduled service", "OK", f"Next due in {km_left} km or {months_left} months.", "ANNUAL")

    if km_since >= TYRE_ROTATION_KM:
        add("Tyre rotation", "Due", f"{km_since} km since last rotation (recommended every {TYRE_ROTATION_KM} km).", "TYRES")
    if months_since >= CABIN_FILTER_MONTHS:
        add("Cabin air filter", "Due", f"{months_since} months since last replacement.", "ANNUAL")
    if vehicle["telemetry"]["twelve_volt_battery"].lower() != "good":
        add("12V battery", "Action needed", vehicle["telemetry"]["twelve_volt_battery"], "BATTERY_12V")
    pressures = vehicle["telemetry"]["tire_pressure_bar"]
    low = {pos: p for pos, p in pressures.items() if p < 2.5}
    if low:
        add("Tyre pressure", "Action needed", f"Low pressure: {low}. Inflate or have the tyre checked for punctures.", "TYRES")

    return {
        "vin": vehicle["vin"],
        "model": vehicle["model"],
        "odometer_km": odometer,
        "last_service": vehicle["last_service"],
        "recommendations": items,
    }


def get_warranty_status(owner_id: str, vin: str) -> Dict[str, Any]:
    vehicle = _owned_vehicle(owner_id, vin)
    owner = store.owner(owner_id)
    region = store.data["country_region"].get(owner["country"], "Europe")
    policy = store.data["warranty_policies"][region]
    delivered = date.fromisoformat(vehicle["delivery_date"])
    odometer = vehicle["telemetry"]["odometer_km"]

    def coverage(years: int, km: Optional[int]) -> Dict[str, Any]:
        expires = _add_years(delivered, years)
        active = today() <= expires and (km is None or odometer <= km)
        result = {"expires_on": expires.isoformat(), "active": active}
        if km is not None:
            result["km_limit"] = km
            result["km_remaining"] = max(km - odometer, 0)
        return result

    battery = coverage(policy["battery_years"], policy["battery_km"])
    battery["min_guaranteed_health_pct"] = policy["battery_min_health_pct"]
    battery["current_health_pct"] = vehicle["telemetry"]["battery_health_pct"]
    return {
        "vin": vehicle["vin"],
        "model": vehicle["model"],
        "region": region,
        "delivery_date": vehicle["delivery_date"],
        "odometer_km": odometer,
        "vehicle_warranty": coverage(policy["vehicle_years"], policy["vehicle_km"]),
        "battery_warranty": battery,
        "roadside_assistance": coverage(policy["roadside_assistance_years"], None),
    }


def check_recalls_and_updates(owner_id: str, vin: str) -> Dict[str, Any]:
    vehicle = _owned_vehicle(owner_id, vin)
    current = vehicle["telemetry"]["software_version"]
    open_items = []
    for c in store.data["recalls_and_campaigns"]:
        if c["type"] == "Over-the-air update":
            applies = (
                vehicle["model"] in c["models"]
                and _version_tuple(current) < _version_tuple(c["target_version"])
            )
        else:
            applies = vehicle["vin"] in c["affected_vins"]
        if applies:
            open_items.append({k: v for k, v in c.items() if k not in ("affected_vins", "models", "model_years")})
    return {"vin": vehicle["vin"], "current_software": current, "open_items": open_items}


def find_service_centers(city: str = "", country: str = "", service: str = "") -> Dict[str, Any]:
    results = []
    for c in store.data["service_centers"]:
        if city and city.lower() not in c["city"].lower():
            continue
        if country and country.lower() not in c["country"].lower():
            continue
        if service and not any(service.lower() in s.lower() for s in c["services"]):
            continue
        results.append(c)
    return {"count": len(results), "service_centers": results}


def get_service_catalog() -> Dict[str, Any]:
    return {"services": store.data["service_catalog"]}


def _open_weekdays(center: Dict[str, Any]) -> List[int]:
    hours = center["hours"]
    if "Sat-Thu" in hours:
        return [5, 6, 0, 1, 2, 3]
    return [0, 1, 2, 3, 4] + ([5] if "Sat" in hours else [])


def _slots_for(center: Dict[str, Any], day: date) -> List[str]:
    if day.weekday() not in _open_weekdays(center) or day <= today():
        return []
    times = SLOT_TIMES_SATURDAY if day.weekday() == 5 and "Sat-Thu" not in center["hours"] else SLOT_TIMES_WEEKDAY
    booked = {
        a["time"]
        for a in store.data["appointments"]
        if a["service_center_id"] == center["center_id"] and a["date"] == day.isoformat() and a["status"] != "Cancelled"
    }
    free = []
    for t in times:
        # Deterministically mark about a third of slots as already taken by other customers.
        h = hashlib.sha256(f"{center['center_id']}{day}{t}".encode()).digest()[0]
        if h % 3 != 0 and t not in booked:
            free.append(t)
    return free


def get_available_slots(center_id: str, start_date: str = "", days: int = 7) -> Dict[str, Any]:
    center = store.center(center_id)
    if not center:
        raise ToolError(f"Unknown service center {center_id}.")
    start = date.fromisoformat(start_date) if start_date else today() + timedelta(days=1)
    days = max(1, min(int(days), 21))
    availability = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        slots = _slots_for(center, day)
        if slots:
            availability.append({"date": day.isoformat(), "weekday": day.strftime("%A"), "times": slots})
    return {"center_id": center["center_id"], "center_name": center["name"], "availability": availability}


def book_service_appointment(
    owner_id: str,
    vin: str,
    center_id: str,
    date_str: str,
    time: str,
    service_codes: List[str],
    notes: str = "",
    loaner_requested: bool = False,
) -> Dict[str, Any]:
    vehicle = _owned_vehicle(owner_id, vin)
    center = store.center(center_id)
    if not center:
        raise ToolError(f"Unknown service center {center_id}.")
    catalog = store.service_codes()
    unknown = [c for c in service_codes if c not in catalog]
    if unknown or not service_codes:
        raise ToolError(f"Unknown or missing service codes {unknown}. Valid codes: {sorted(catalog)}.")
    day = date.fromisoformat(date_str)
    if time not in _slots_for(center, day):
        raise ToolError(f"{date_str} {time} is not available at {center['name']}. Check get_available_slots first.")
    if loaner_requested and not center["loaner_cars"]:
        raise ToolError(f"{center['name']} does not offer loaner cars.")

    appointment = {
        "appointment_id": store.new_id("APT"),
        "owner_id": vehicle["owner_id"],
        "vin": vehicle["vin"],
        "service_center_id": center["center_id"],
        "date": day.isoformat(),
        "time": time,
        "service_codes": service_codes,
        "notes": notes,
        "status": "Confirmed",
        "loaner_requested": loaner_requested,
    }
    store.data["appointments"].append(appointment)
    return {
        **appointment,
        "center_name": center["name"],
        "center_address": center["address"],
        "estimated_duration_hours": sum(catalog[c]["duration_hours"] for c in service_codes),
        "confirmation": "A confirmation has been sent to the owner's email and the ZEEKR app.",
    }


def list_appointments(owner_id: str) -> Dict[str, Any]:
    if not store.owner(owner_id):
        raise ToolError(f"No owner found with ID {owner_id}.")
    apts = [a for a in store.data["appointments"] if a["owner_id"] == store.owner(owner_id)["owner_id"]]
    return {"appointments": sorted(apts, key=lambda a: (a["date"], a["time"]))}


def _owned_appointment(owner_id: str, appointment_id: str) -> Dict[str, Any]:
    owner = store.owner(owner_id)
    apt = next((a for a in store.data["appointments"] if a["appointment_id"].lower() == appointment_id.lower()), None)
    if not owner or not apt or apt["owner_id"] != owner["owner_id"]:
        raise ToolError(f"Appointment {appointment_id} was not found for owner {owner_id}.")
    if apt["status"] == "Cancelled":
        raise ToolError(f"Appointment {appointment_id} is already cancelled.")
    return apt


def reschedule_appointment(owner_id: str, appointment_id: str, new_date: str, new_time: str) -> Dict[str, Any]:
    apt = _owned_appointment(owner_id, appointment_id)
    center = store.center(apt["service_center_id"])
    if new_time not in _slots_for(center, date.fromisoformat(new_date)):
        raise ToolError(f"{new_date} {new_time} is not available at {center['name']}.")
    apt["date"], apt["time"] = new_date, new_time
    return {**apt, "center_name": center["name"]}


def cancel_appointment(owner_id: str, appointment_id: str) -> Dict[str, Any]:
    apt = _owned_appointment(owner_id, appointment_id)
    apt["status"] = "Cancelled"
    return apt


def request_roadside_assistance(
    owner_id: str, vin: str, location: str, issue_type: str, description: str = ""
) -> Dict[str, Any]:
    vehicle = _owned_vehicle(owner_id, vin)
    warranty = get_warranty_status(owner_id, vin)
    owner = store.owner(owner_id)
    request = {
        "request_id": store.new_id("RSA"),
        "vin": vehicle["vin"],
        "license_plate": vehicle["license_plate"],
        "location": location,
        "issue_type": issue_type,
        "description": description,
        "status": "Dispatched",
        "estimated_arrival_minutes": 35 if issue_type != "towing" else 50,
        "covered": warranty["roadside_assistance"]["active"],
        "callback_number": owner["phone"],
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "safety_advice": "Switch on hazard lights, stay in a safe place away from traffic, and keep your phone on.",
    }
    store.roadside_requests.append(request)
    return request


def create_support_case(owner_id: str, category: str, subject: str, description: str, vin: str = "") -> Dict[str, Any]:
    owner = store.owner(owner_id)
    if not owner:
        raise ToolError(f"No owner found with ID {owner_id}.")
    if vin:
        _owned_vehicle(owner_id, vin)
    case = {
        "case_id": store.new_id("CASE"),
        "owner_id": owner["owner_id"],
        "vin": vin or None,
        "category": category,
        "subject": subject,
        "description": description,
        "status": "Open",
        "opened": today().isoformat(),
        "last_update": "Case created. An Owner Care specialist will respond within 1 business day.",
    }
    store.data["support_cases"].append(case)
    return case


def get_support_cases(owner_id: str) -> Dict[str, Any]:
    owner = store.owner(owner_id)
    if not owner:
        raise ToolError(f"No owner found with ID {owner_id}.")
    return {"cases": [c for c in store.data["support_cases"] if c["owner_id"] == owner["owner_id"]]}


def search_knowledge_base(query: str) -> Dict[str, Any]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    scored = []
    for article in store.data["knowledge_base"]:
        text = f"{article['topic']} {article['question']} {article['answer']}".lower()
        score = sum(text.count(w) for w in words)
        if score:
            scored.append((score, article))
    scored.sort(key=lambda s: s[0], reverse=True)
    return {"articles": [a for _, a in scored[:3]]}


# Tool definitions for the model -----------------------------------------------

_OWNER_ID = {"type": "string", "description": "Owner ID returned by identify_owner, e.g. ZK-OWN-1001."}
_VIN = {"type": "string", "description": "17-character VIN of one of the owner's vehicles."}


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "identify_owner",
        "description": "Look up a ZEEKR owner by owner ID, email, phone number, VIN or licence plate. Returns the profile "
        "and registered vehicles. Call this before any other owner-specific tool.",
        "input_schema": _schema({"identifier": {"type": "string", "description": "Owner ID, email, phone, VIN or plate."}}, ["identifier"]),
    },
    {
        "name": "get_vehicle_status",
        "description": "Live connected-car status: odometer, charge level, range, battery health, charging state, tyre "
        "pressures, 12V battery, software version and active alerts.",
        "input_schema": _schema({"owner_id": _OWNER_ID, "vin": _VIN}, ["owner_id", "vin"]),
    },
    {
        "name": "get_service_history",
        "description": "Past service and repair records for a vehicle, newest first.",
        "input_schema": _schema({"owner_id": _OWNER_ID, "vin": _VIN}, ["owner_id", "vin"]),
    },
    {
        "name": "get_maintenance_recommendations",
        "description": "Maintenance items that are due, overdue or need action, based on mileage, time since last "
        "service and live telemetry. Each item includes the service_code to use when booking.",
        "input_schema": _schema({"owner_id": _OWNER_ID, "vin": _VIN}, ["owner_id", "vin"]),
    },
    {
        "name": "get_warranty_status",
        "description": "Vehicle, high-voltage battery and roadside-assistance warranty coverage and expiry for a vehicle.",
        "input_schema": _schema({"owner_id": _OWNER_ID, "vin": _VIN}, ["owner_id", "vin"]),
    },
    {
        "name": "check_recalls_and_updates",
        "description": "Open recalls, service campaigns and available over-the-air software updates for a vehicle.",
        "input_schema": _schema({"owner_id": _OWNER_ID, "vin": _VIN}, ["owner_id", "vin"]),
    },
    {
        "name": "find_service_centers",
        "description": "Search ZEEKR service centers by city, country and/or offered service. All filters are optional.",
        "input_schema": _schema(
            {
                "city": {"type": "string", "description": "City name, e.g. Amsterdam."},
                "country": {"type": "string", "description": "Country name, e.g. Sweden."},
                "service": {"type": "string", "description": "Service keyword, e.g. Tyres or battery."},
            },
            [],
        ),
    },
    {
        "name": "get_service_catalog",
        "description": "List bookable service types with their codes, durations and descriptions.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_available_slots",
        "description": "Free appointment slots at a service center for a range of days.",
        "input_schema": _schema(
            {
                "center_id": {"type": "string", "description": "Service center ID, e.g. SC-AMS-01."},
                "start_date": {"type": "string", "description": "First date to check (YYYY-MM-DD). Defaults to tomorrow."},
                "days": {"type": "integer", "description": "Number of days to check (1-21). Defaults to 7."},
            },
            ["center_id"],
        ),
    },
    {
        "name": "book_service_appointment",
        "description": "Book a service appointment. Only call after the owner has confirmed the center, date, time and "
        "services. The slot must come from get_available_slots.",
        "input_schema": _schema(
            {
                "owner_id": _OWNER_ID,
                "vin": _VIN,
                "center_id": {"type": "string", "description": "Service center ID."},
                "date": {"type": "string", "description": "Appointment date (YYYY-MM-DD)."},
                "time": {"type": "string", "description": "Appointment time (HH:MM) from get_available_slots."},
                "service_codes": {"type": "array", "items": {"type": "string"}, "description": "Service codes from the catalog, e.g. [\"ANNUAL\"]."},
                "notes": {"type": "string", "description": "Notes for the technician."},
                "loaner_requested": {"type": "boolean", "description": "Whether the owner wants a loaner car."},
            },
            ["owner_id", "vin", "center_id", "date", "time", "service_codes"],
        ),
    },
    {
        "name": "list_appointments",
        "description": "All service appointments for the owner, including cancelled ones.",
        "input_schema": _schema({"owner_id": _OWNER_ID}, ["owner_id"]),
    },
    {
        "name": "reschedule_appointment",
        "description": "Move an existing appointment to a new free slot at the same service center. Confirm with the owner first.",
        "input_schema": _schema(
            {
                "owner_id": _OWNER_ID,
                "appointment_id": {"type": "string", "description": "Appointment ID, e.g. APT-50001."},
                "new_date": {"type": "string", "description": "New date (YYYY-MM-DD)."},
                "new_time": {"type": "string", "description": "New time (HH:MM)."},
            },
            ["owner_id", "appointment_id", "new_date", "new_time"],
        ),
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel an existing appointment. Confirm with the owner first.",
        "input_schema": _schema(
            {"owner_id": _OWNER_ID, "appointment_id": {"type": "string", "description": "Appointment ID."}},
            ["owner_id", "appointment_id"],
        ),
    },
    {
        "name": "request_roadside_assistance",
        "description": "Dispatch 24/7 roadside assistance for a breakdown, flat tyre, flat 12V battery, lockout, "
        "running out of charge, accident or towing need.",
        "input_schema": _schema(
            {
                "owner_id": _OWNER_ID,
                "vin": _VIN,
                "location": {"type": "string", "description": "Where the car is: address, road and direction, or landmark."},
                "issue_type": {
                    "type": "string",
                    "enum": ["flat_tyre", "flat_12v_battery", "out_of_charge", "lockout", "breakdown", "accident", "towing"],
                },
                "description": {"type": "string", "description": "What happened, warning lights, whether anyone is hurt."},
            },
            ["owner_id", "vin", "location", "issue_type"],
        ),
    },
    {
        "name": "create_support_case",
        "description": "Open an Owner Care case for issues that need a specialist: complaints, faults that need "
        "investigation, billing, app or infotainment problems, feedback.",
        "input_schema": _schema(
            {
                "owner_id": _OWNER_ID,
                "vin": {"type": "string", "description": "VIN, if the case relates to a specific vehicle."},
                "category": {
                    "type": "string",
                    "enum": ["Vehicle fault", "Infotainment", "ZEEKR app", "Charging", "Billing", "Complaint", "Feedback", "Other"],
                },
                "subject": {"type": "string", "description": "One-line summary."},
                "description": {"type": "string", "description": "Full details in the owner's words."},
            },
            ["owner_id", "category", "subject", "description"],
        ),
    },
    {
        "name": "get_support_cases",
        "description": "Existing Owner Care cases for the owner with their current status.",
        "input_schema": _schema({"owner_id": _OWNER_ID}, ["owner_id"]),
    },
    {
        "name": "search_knowledge_base",
        "description": "Search ZEEKR owner help articles on charging, range, software updates, warranty, roadside "
        "assistance, tyres, digital key and maintenance. Use for general how-to questions.",
        "input_schema": _schema({"query": {"type": "string", "description": "The owner's question or keywords."}}, ["query"]),
    },
]


_TOOL_FUNCTIONS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "identify_owner": identify_owner,
    "get_vehicle_status": get_vehicle_status,
    "get_service_history": get_service_history,
    "get_maintenance_recommendations": get_maintenance_recommendations,
    "get_warranty_status": get_warranty_status,
    "check_recalls_and_updates": check_recalls_and_updates,
    "find_service_centers": find_service_centers,
    "get_service_catalog": get_service_catalog,
    "get_available_slots": get_available_slots,
    "book_service_appointment": lambda date, **kw: book_service_appointment(date_str=date, **kw),
    "list_appointments": list_appointments,
    "reschedule_appointment": reschedule_appointment,
    "cancel_appointment": cancel_appointment,
    "request_roadside_assistance": request_roadside_assistance,
    "create_support_case": create_support_case,
    "get_support_cases": get_support_cases,
    "search_knowledge_base": search_knowledge_base,
}


def execute_tool(name: str, tool_input: Dict[str, Any]) -> tuple:
    """Run a tool. Returns (result_json, is_error)."""
    func = _TOOL_FUNCTIONS.get(name)
    if func is None:
        return json.dumps({"error": f"Unknown tool {name}"}), True
    try:
        return json.dumps(func(**tool_input), default=str), False
    except ToolError as e:
        return json.dumps({"error": str(e)}), True
    except (TypeError, ValueError) as e:
        return json.dumps({"error": f"Invalid input for {name}: {e}"}), True
