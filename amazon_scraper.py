"""
PATCH for amazon_scraper.py
============================
Replace the patch_search_payload function with this version.
Fixes the geoQueryClause structure confirmed from HAR analysis.
"""

import copy
from typing import Any, Optional

PAGE_SIZE = 100


def patch_search_payload(
    body_json: dict[str, Any],
    next_token: Optional[str] = None,
    lat: float = 52.4862,
    lng: float = -1.8904,
    distance: int = 30
) -> dict[str, Any]:
    """
    FIXED: Uses confirmed geoQueryClause structure from HAR.

    OLD (wrong):
      "lat": 52.4862,
      "lng": -1.8904,
      "radius": 80

    NEW (confirmed):
      "geoQueryClause": {
          "lat": 52.4862,
          "lng": -1.8904,
          "unit": "mi",
          "distance": 30
      }

    Also confirmed:
      "containFilters": [{"key": "isPrivateSchedule", "val": ["true","false"]}]
      "orFilters": [bonusJob, featuredJob]
      "consolidateSchedule": True
    """
    payload = copy.deepcopy(body_json)
    variables = payload.setdefault("variables", {})

    search_req = (
        variables.get("searchJobRequest")
        or variables.get("input")
        or variables.get("request")
        or {}
    )

    # Core fields
    search_req["locale"] = "en-GB"
    search_req["country"] = "United Kingdom"
    search_req["keyWords"] = ""
    search_req["pageSize"] = PAGE_SIZE

    # ✅ CONFIRMED geoQueryClause structure
    search_req["geoQueryClause"] = {
        "lat": lat,
        "lng": lng,
        "unit": "mi",
        "distance": distance
    }

    # ✅ CONFIRMED filters
    search_req["containFilters"] = [
        {"key": "isPrivateSchedule", "val": ["true", "false"]}
    ]
    search_req["orFilters"] = [
        {"key": "bonusJob", "val": ["true"]},
        {"key": "featuredJob", "val": ["true"]}
    ]
    search_req["equalFilters"] = []
    search_req["rangeFilters"] = []
    search_req["dateFilters"] = []
    search_req["sorters"] = []
    search_req["consolidateSchedule"] = True

    # Remove old wrong fields
    for old_field in ["lat", "lng", "radius", "distance",
                       "latitude", "longitude"]:
        search_req.pop(old_field, None)

    # Pagination
    if next_token:
        search_req["nextToken"] = next_token
    else:
        search_req.pop("nextToken", None)
        search_req.pop("nextPageToken", None)

    variables["searchJobRequest"] = search_req
    return payload


# ─────────────────────────────────────────────────────────
# UK City coordinates for location-based search
# ─────────────────────────────────────────────────────────

UK_CITIES = {
    "birmingham":    (52.4862, -1.8904),
    "london":        (51.5074, -0.1278),
    "manchester":    (53.4808, -2.2426),
    "coventry":      (52.4068, -1.5197),
    "wolverhampton": (52.5847, -2.1269),
    "derby":         (52.9225, -1.4746),
    "nottingham":    (52.9548, -1.1581),
    "leicester":     (52.6369, -1.1398),
    "bristol":       (51.4545, -2.5879),
    "leeds":         (53.8008, -1.5491),
    "sheffield":     (53.3811, -1.4701),
    "liverpool":     (53.4084, -2.9916),
    "newcastle":     (54.9783, -1.6178),
    "glasgow":       (55.8642, -4.2518),
    "edinburgh":     (55.9533, -3.1883),
    "cardiff":       (51.4816, -3.1791),
    "exeter":        (50.7236, -3.5275),
    "rotherham":     (53.4326, -1.3635),
    "swindon":       (51.5558, -1.7797),
    "plymouth":      (50.3755, -4.1427),
}


def get_coords(city: str) -> tuple:
    return UK_CITIES.get(city.lower().strip(), (52.4862, -1.8904))
