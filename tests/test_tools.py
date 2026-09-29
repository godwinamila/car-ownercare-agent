import json
import os
import sys
from pathlib import Path

os.environ["DEMO_TODAY"] = "2026-09-28"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import tools  # noqa: E402
from tools import ToolError, execute_tool  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch):
    monkeypatch.setattr(tools, "store", tools.OwnerCareStore())


def test_identify_owner_by_each_identifier():
    for ident in ["ZK-OWN-1001", "emma.devries@example.com", "+31 6 5550 1001", "LZKZ00XDEMO000002", "zk-001-nl"]:
        assert tools.identify_owner(ident)["owner_id"] == "ZK-OWN-1001"
    with pytest.raises(ToolError):
        tools.identify_owner("nobody@example.com")


def test_cannot_access_another_owners_vehicle():
    result, is_error = execute_tool("get_vehicle_status", {"owner_id": "ZK-OWN-1002", "vin": "LZKZ001DEMO000001"})
    assert is_error and "not registered" in json.loads(result)["error"]


def test_maintenance_flags_overdue_service_and_12v_battery():
    items = {i["item"]: i for i in tools.get_maintenance_recommendations("ZK-OWN-1005", "LZKZ00XDEMO000006")["recommendations"]}
    assert items["Scheduled service"]["status"] == "Overdue"
    assert items["12V battery"]["service_code"] == "BATTERY_12V"


def test_maintenance_flags_low_tyre_pressure():
    items = {i["item"] for i in tools.get_maintenance_recommendations("ZK-OWN-1004", "LZKZ7XBDEMO000005")["recommendations"]}
    assert "Tyre pressure" in items


def test_recalls_and_ota():
    open_ids = {i["campaign_id"] for i in tools.check_recalls_and_updates("ZK-OWN-1006", "LZKZ007DEMO000007")["open_items"]}
    assert open_ids == {"SC-2026-007A", "OTA-2026-09"}
    # Already on the latest software, no campaigns.
    assert tools.check_recalls_and_updates("ZK-OWN-1001", "LZKZ00XDEMO000002")["open_items"] == []


def test_warranty_active():
    w = tools.get_warranty_status("ZK-OWN-1001", "LZKZ001DEMO000001")
    assert w["region"] == "Europe"
    assert w["vehicle_warranty"]["active"] and w["vehicle_warranty"]["expires_on"] == "2029-03-15"


def test_book_reschedule_cancel_flow():
    slots = tools.get_available_slots("SC-AMS-01", days=10)["availability"]
    first, second = slots[0], slots[1]
    result, is_error = execute_tool(
        "book_service_appointment",
        {
            "owner_id": "ZK-OWN-1001",
            "vin": "LZKZ001DEMO000001",
            "center_id": "SC-AMS-01",
            "date": first["date"],
            "time": first["times"][0],
            "service_codes": ["ANNUAL", "TYRES"],
            "loaner_requested": True,
        },
    )
    assert not is_error, result
    apt = json.loads(result)
    assert apt["estimated_duration_hours"] == 3.5

    # The booked slot is no longer offered.
    again = tools.get_available_slots("SC-AMS-01", start_date=first["date"], days=1)["availability"]
    assert not again or first["times"][0] not in again[0]["times"]

    moved = tools.reschedule_appointment("ZK-OWN-1001", apt["appointment_id"], second["date"], second["times"][0])
    assert moved["date"] == second["date"]
    assert tools.cancel_appointment("ZK-OWN-1001", apt["appointment_id"])["status"] == "Cancelled"


def test_booking_rejects_unavailable_slot():
    result, is_error = execute_tool(
        "book_service_appointment",
        {
            "owner_id": "ZK-OWN-1001",
            "vin": "LZKZ001DEMO000001",
            "center_id": "SC-AMS-01",
            "date": "2026-10-04",  # Sunday
            "time": "09:00",
            "service_codes": ["ANNUAL"],
        },
    )
    assert is_error


def test_dubai_open_on_sunday_closed_friday():
    days = {d["weekday"] for d in tools.get_available_slots("SC-DXB-01", days=14)["availability"]}
    assert "Friday" not in days and "Sunday" in days


def test_roadside_and_support_case():
    rsa = tools.request_roadside_assistance("ZK-OWN-1004", "LZKZ7XBDEMO000005", "M4 westbound near Parramatta", "flat_tyre")
    assert rsa["covered"] and rsa["status"] == "Dispatched"
    case = tools.create_support_case("ZK-OWN-1004", "Elyra app", "App login fails", "Can't log in since update.")
    assert case["case_id"] in {c["case_id"] for c in tools.get_support_cases("ZK-OWN-1004")["cases"]}


def test_knowledge_base_search():
    articles = tools.search_knowledge_base("how do I share my digital key")["articles"]
    assert articles[0]["id"] == "KB-08"


def test_every_schema_has_a_function():
    assert {t["name"] for t in tools.TOOL_SCHEMAS} == set(tools._TOOL_FUNCTIONS)
