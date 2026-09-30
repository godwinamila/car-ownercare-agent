import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402
from tools import execute_tool  # noqa: E402


def call(name, **kwargs):
    result, is_error = execute_tool(name, kwargs)
    return json.loads(result), is_error


def test_company_info():
    info, err = call("get_company_info")
    assert not err and info["name"] == "Elyra" and "Netherlands" in info["markets"]


def test_list_models():
    data, _ = call("list_models")
    assert [m["model"] for m in data["models"]] == ["Elyra 001", "Elyra 007", "Elyra 7X", "Elyra X", "Elyra 009"]


def test_model_lookup_accepts_short_names():
    for name in ["Elyra 7X", "7x", "7X", "elyra-7x"]:
        details, err = call("get_model_details", model=name)
        assert not err and details["model"] == "Elyra 7X"
    details, _ = call("get_model_details", model="007")
    assert details["model"] == "Elyra 007"


def test_unknown_model_is_an_error():
    data, err = call("get_model_details", model="Elyra 500")
    assert err and "Unknown model" in data["error"]


def test_compare_models():
    data, err = call("compare_models", models=["007", "7X"])
    assert not err
    assert {r["model"] for r in data["comparison"]} == {"Elyra 007", "Elyra 7X"}
    _, err = call("compare_models", models=["007"])
    assert err


def test_warranty_by_country_and_all_regions():
    data, _ = call("get_warranty_policy", country="Sweden")
    assert data["region"] == "Europe" and data["policy"]["battery_years"] == 8
    data, _ = call("get_warranty_policy")
    assert set(data["policies_by_region"]) == {"Europe", "Middle East", "Asia Pacific"}
    _, err = call("get_warranty_policy", country="Mars")
    assert err


def test_service_centers_filters():
    data, _ = call("find_service_centers", country="Australia")
    assert data["count"] == 2
    data, _ = call("find_service_centers", city="Stockholm", service="tyre hotel")
    assert data["count"] == 1


def test_contact_channels():
    data, _ = call("get_contact_channels", country="Singapore")
    assert data["contact_channels"][0]["roadside_phone"]
    _, err = call("get_contact_channels", country="Atlantis")
    assert err


def test_software_updates_and_campaigns():
    data, _ = call("get_software_updates")
    assert data["latest"]["version"] == "Elyra OS 6.3.0"
    data, _ = call("get_service_campaigns", model="007")
    assert [c["campaign_id"] for c in data["campaigns"]] == ["SC-2026-007A"]
    data, _ = call("get_service_campaigns", model="7X")
    assert data["campaigns"] == []


def test_knowledge_base_search():
    data, _ = call("search_knowledge_base", query="how do I book a test drive")
    assert data["articles"][0]["id"] == "KB-12"


def test_no_customer_specific_tools():
    names = {t["name"] for t in tools.TOOL_SCHEMAS}
    assert not any(word in n for n in names for word in ("owner", "book_", "cancel", "vehicle_status", "case"))


def test_every_schema_has_a_function():
    assert {t["name"] for t in tools.TOOL_SCHEMAS} == set(tools._TOOL_FUNCTIONS)
