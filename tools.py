"""Elyra company information tools backed by the static demo data in data/company_data.json.

These tools answer general questions about Elyra: the company, models and specifications, warranty terms,
the service network, software updates, service campaigns, contact channels and help articles. None of them
need to know who the customer is.

Each tool is a plain Python function that returns a JSON-serialisable dict.
TOOL_SCHEMAS describes them to the model; execute_tool() dispatches a tool call.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DATA_FILE = Path(os.getenv("COMPANY_DATA_FILE", Path(__file__).parent / "data" / "company_data.json"))


def today() -> date:
    """Current date; set DEMO_TODAY=YYYY-MM-DD to pin it for a repeatable demo."""
    override = os.getenv("DEMO_TODAY")
    return date.fromisoformat(override) if override else date.today()


class CompanyData:
    def __init__(self, path: Path = DATA_FILE):
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))

    def model(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a model by full or partial name: "Elyra 7X", "7x", "007"."""
        wanted = re.sub(r"[^a-z0-9]", "", name.lower()).replace("elyra", "")
        for m in self.data["models"]:
            if re.sub(r"[^a-z0-9]", "", m["model"].lower()).replace("elyra", "") == wanted:
                return m
        return None

    def region(self, country: str) -> Optional[str]:
        for c, region in self.data["country_region"].items():
            if c.lower() == country.strip().lower():
                return region
        return None


store = CompanyData()


class ToolError(Exception):
    """Raised for bad input; returned to the model as an error tool result."""


def _require_model(name: str) -> Dict[str, Any]:
    m = store.model(name)
    if not m:
        raise ToolError(f"Unknown model {name!r}. Elyra models: {[x['model'] for x in store.data['models']]}.")
    return m


def _model_summary(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "model": m["model"],
        "body_type": m["body_type"],
        "seats": m["seats"],
        "max_range_wltp_km": max(m["range_wltp_km"].values()),
        "starting_price_eur": m["starting_price_eur"],
    }


# Tool implementations ------------------------------------------------------


def get_company_info() -> Dict[str, Any]:
    return store.data["company"]


def list_models() -> Dict[str, Any]:
    return {"models": [_model_summary(m) for m in store.data["models"]], "prices_note": "Indicative starting prices in EUR; local prices vary."}


def get_model_details(model: str) -> Dict[str, Any]:
    return {**_require_model(model), "prices_note": "Indicative starting price in EUR; local prices vary."}


def compare_models(models: List[str]) -> Dict[str, Any]:
    if len(models) < 2:
        raise ToolError("Give at least two models to compare.")
    rows = []
    for name in models:
        m = _require_model(name)
        rows.append({
            "model": m["model"],
            "body_type": m["body_type"],
            "seats": m["seats"],
            "battery_kwh": m["battery_kwh"],
            "max_range_wltp_km": max(m["range_wltp_km"].values()),
            "fastest_0_100_s": min(m["acceleration_0_100_s"].values()),
            "max_dc_charge_kw": m["max_dc_charge_kw"],
            "dc_10_80_minutes": m["dc_10_80_minutes"],
            "boot_litres": m["boot_litres"],
            "towing_kg": m["towing_kg"],
            "starting_price_eur": m["starting_price_eur"],
        })
    return {"comparison": rows}


def get_warranty_policy(country: str = "") -> Dict[str, Any]:
    policies = store.data["warranty_policies"]
    exclusions = store.data["warranty_exclusions"]
    if not country:
        return {"policies_by_region": policies, "country_regions": store.data["country_region"], "exclusions": exclusions}
    region = store.region(country)
    if not region:
        raise ToolError(f"Elyra doesn't sell in {country!r}. Markets: {list(store.data['country_region'])}.")
    return {"country": country, "region": region, "policy": policies[region], "exclusions": exclusions}


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
    return {"services": store.data["service_catalog"], "prices_note": "Indicative prices in EUR; local prices vary."}


def get_contact_channels(country: str = "") -> Dict[str, Any]:
    channels = store.data["contact_channels"]
    if country:
        channels = [c for c in channels if c["country"].lower() == country.strip().lower()]
        if not channels:
            raise ToolError(f"No Elyra contact channels for {country!r}. Markets: {list(store.data['country_region'])}.")
    return {
        "safety_first": "If there is smoke, a burning smell, fire, a high-voltage battery warning or anyone is hurt: pull "
        "over when safe, switch off the car, get everyone out and away from the car and traffic, and call local "
        "emergency services (112 in Europe, 999 in the UAE and Hong Kong, 000 in Australia, 995 in Singapore). "
        "Then call roadside assistance.",
        "contact_channels": channels,
        "app": "The Elyra app also has in-app chat and an SOS button for roadside help.",
    }


def get_software_updates(model: str = "") -> Dict[str, Any]:
    releases = store.data["software_releases"]
    if model:
        name = _require_model(model)["model"]
        releases = [r for r in releases if name in r["models"]]
    return {"latest": releases[0] if releases else None, "releases": releases}


def get_service_campaigns(model: str = "") -> Dict[str, Any]:
    campaigns = store.data["service_campaigns"]
    if model:
        name = _require_model(model)["model"]
        campaigns = [c for c in campaigns if name in c["models"]]
    return {
        "campaigns": campaigns,
        "note": "Campaigns apply only to some cars of the listed models and years. Affected owners are contacted "
        "directly, and a service centre can check a specific car by its VIN.",
    }


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


# Tool definitions for the model ---------------------------------------------

_MODEL = {"type": "string", "description": "Model name, e.g. \"Elyra 7X\" or just \"7X\"."}
_COUNTRY = {"type": "string", "description": "Country name, e.g. Netherlands."}


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "get_company_info",
        "description": "About Elyra: description, headquarters, markets, website, the Elyra app, sustainability and "
        "customer programmes (Elyra Club, Elyra Care, Elyra Charge).",
        "input_schema": _schema({}, []),
    },
    {
        "name": "list_models",
        "description": "All Elyra models with body type, seats, maximum range and starting price.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_model_details",
        "description": "Full specifications of one model: drive options, battery, range, power, acceleration, "
        "charging speeds, boot space, towing, price and highlights.",
        "input_schema": _schema({"model": _MODEL}, ["model"]),
    },
    {
        "name": "compare_models",
        "description": "Side-by-side comparison of two or more models on the key specifications and price.",
        "input_schema": _schema(
            {"models": {"type": "array", "items": {"type": "string"}, "description": "Model names, e.g. [\"007\", \"7X\"]."}},
            ["models"],
        ),
    },
    {
        "name": "get_warranty_policy",
        "description": "Warranty terms (vehicle, high-voltage battery, roadside assistance, paint, corrosion) for a "
        "country, or for all regions if no country is given, plus what isn't covered.",
        "input_schema": _schema({"country": _COUNTRY}, []),
    },
    {
        "name": "find_service_centers",
        "description": "Search Elyra service centres by city, country and/or offered service. All filters are optional.",
        "input_schema": _schema(
            {
                "city": {"type": "string", "description": "City name, e.g. Amsterdam."},
                "country": _COUNTRY,
                "service": {"type": "string", "description": "Service keyword, e.g. Tyres or battery."},
            },
            [],
        ),
    },
    {
        "name": "get_service_catalog",
        "description": "Service types with typical duration and indicative price.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_contact_channels",
        "description": "Customer care phone, roadside assistance phone, email and opening hours, for one country or all.",
        "input_schema": _schema({"country": _COUNTRY}, []),
    },
    {
        "name": "get_software_updates",
        "description": "Latest Elyra OS releases with release notes, optionally for one model.",
        "input_schema": _schema({"model": _MODEL}, []),
    },
    {
        "name": "get_service_campaigns",
        "description": "Open service campaigns and recalls by model and model year, optionally for one model.",
        "input_schema": _schema({"model": _MODEL}, []),
    },
    {
        "name": "search_knowledge_base",
        "description": "Search Elyra help articles: charging, home chargers, range, software updates, warranty, "
        "roadside assistance, tyres, digital key, maintenance, test drives, ordering and delivery, connected "
        "services, towing, privacy, battery care and winter driving.",
        "input_schema": _schema({"query": {"type": "string", "description": "The customer's question or keywords."}}, ["query"]),
    },
]


_TOOL_FUNCTIONS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "get_company_info": get_company_info,
    "list_models": list_models,
    "get_model_details": get_model_details,
    "compare_models": compare_models,
    "get_warranty_policy": get_warranty_policy,
    "find_service_centers": find_service_centers,
    "get_service_catalog": get_service_catalog,
    "get_contact_channels": get_contact_channels,
    "get_software_updates": get_software_updates,
    "get_service_campaigns": get_service_campaigns,
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
