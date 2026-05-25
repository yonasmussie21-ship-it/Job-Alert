"""
job_parser.py

Production-grade parser/scorer for Amazon UK job cards.

Design rule:
- Be tolerant on unknown/missing Amazon GraphQL fields.
- Reject only clear invalid/non-target jobs.
- Keep incomplete but promising jobs so amazon_scraper.fetch_job_details() can enrich them.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

import aiohttp

from config import (
    BLOCKED_KEYWORDS,
    CITY_COORDS,
    CITY_POSTCODES,
    FRESH_KEYWORDS,
    WAREHOUSE_KEYWORDS,
)

log = logging.getLogger(__name__)

CACHE_TTL = timedelta(minutes=30)
POSTCODE_API_TIMEOUT = 6
POSTCODE_RETRIES = 3
POSTCODE_CACHE_MAX_SIZE = 5000

SCORE_PERMANENT = 50
SCORE_NIGHT_SHIFT = 15
SCORE_FULL_TIME_HOURS = 10
SCORE_PAY_PRESENT = 3
SCORE_SCHEDULE_PRESENT = 3

MIN_FULL_TIME_HOURS = 36
EARTH_RADIUS_MILES = 3958.8


@dataclass(frozen=True)
class Coordinates:
    lat: float
    lon: float


@dataclass(frozen=True)
class CacheEntry:
    coords: Optional[Coordinates]
    created_at: datetime


class TTLCache:
    def __init__(self, max_size: int, ttl: timedelta) -> None:
        self.max_size = max_size
        self.ttl = ttl
        self._items: OrderedDict[str, CacheEntry] = OrderedDict()

    def get(self, key: str) -> Optional[Coordinates]:
        entry = self._items.get(key)
        if not entry:
            return None

        if utc_now() - entry.created_at >= self.ttl:
            self._items.pop(key, None)
            return None

        self._items.move_to_end(key)
        return entry.coords

    def set(self, key: str, coords: Optional[Coordinates]) -> None:
        self._items[key] = CacheEntry(coords=coords, created_at=utc_now())
        self._items.move_to_end(key)
        while len(self._items) > self.max_size:
            self._items.popitem(last=False)


class PostcodeClient:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._cache = TTLCache(POSTCODE_CACHE_MAX_SIZE, CACHE_TTL)

    async def session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=POSTCODE_API_TIMEOUT)
                self._session = aiohttp.ClientSession(timeout=timeout)
            return self._session

    async def close(self) -> None:
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def get_coords(self, postcode_or_city: str) -> Optional[Coordinates]:
        raw = str(postcode_or_city or "").strip()
        clean = clean_postcode(raw)
        norm = normalize(raw)

        if not raw:
            return None

        for key in (raw.upper(), clean, norm):
            if key in CITY_COORDS:
                lat, lon = CITY_COORDS[key]
                return Coordinates(float(lat), float(lon))

        cached = self._cache.get(clean)
        if cached is not None:
            return cached

        coords = await self._fetch_postcode_coords(clean)
        self._cache.set(clean, coords)
        return coords

    async def _fetch_postcode_coords(self, postcode: str) -> Optional[Coordinates]:
        url = f"https://api.postcodes.io/postcodes/{postcode}"

        for attempt in range(1, POSTCODE_RETRIES + 1):
            try:
                session = await self.session()
                async with session.get(url) as response:
                    if response.status == 404:
                        log.debug("[POSTCODE_NOT_FOUND] postcode=%s", postcode)
                        return None

                    if response.status != 200:
                        log.debug("[POSTCODE_STATUS] postcode=%s status=%s attempt=%s", postcode, response.status, attempt)
                        await asyncio.sleep(0.25 * attempt)
                        continue

                    data = await response.json()
                    result = data.get("result") or {}
                    lat = result.get("latitude")
                    lon = result.get("longitude")

                    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                        return Coordinates(float(lat), float(lon))

                    log.debug("[POSTCODE_BAD_RESPONSE] postcode=%s data=%s", postcode, data)
                    return None

            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.debug("[POSTCODE_ERROR] postcode=%s attempt=%s error=%s", postcode, attempt, exc)
                await asyncio.sleep(0.25 * attempt)

        return None


postcode_client = PostcodeClient()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: Any) -> str:
    text = str(value or "").lower().strip()
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("&amp;", "&")
    return re.sub(r"\s+", " ", text)


@lru_cache(maxsize=2000)
def phrase_pattern(phrase: str) -> re.Pattern[str]:
    phrase = normalize(phrase)
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


def contains_phrase(text: str, phrase: str) -> bool:
    phrase = normalize(phrase)
    if not phrase:
        return False
    return phrase_pattern(phrase).search(text) is not None


def has_any_phrase(text: str, phrases: list[str] | tuple[str, ...]) -> bool:
    text = normalize(text)
    return any(contains_phrase(text, phrase) for phrase in phrases)


def get_nested(data: dict[str, Any], paths: tuple[tuple[str, ...], ...], default: Any = None) -> Any:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)

        if current not in (None, ""):
            return current

    return default


def first_value(card: dict[str, Any], *paths: tuple[str, ...], default: Any = None) -> Any:
    return get_nested(card, paths, default=default)


def is_warehouse_job(title: str) -> bool:
    title_norm = normalize(title)
    if not title_norm:
        return True  # Keep unknown title; detail page may enrich it.

    if has_any_phrase(title_norm, tuple(BLOCKED_KEYWORDS)):
        return False

    if has_any_phrase(title_norm, tuple(WAREHOUSE_KEYWORDS)):
        return True

    fallback_terms = (
        "warehouse",
        "fulfilment",
        "fulfillment",
        "sortation",
        "delivery station",
        "operative",
        "associate",
        "picker",
        "packer",
    )
    return has_any_phrase(title_norm, fallback_terms)


def is_fresh_job(job: dict[str, Any]) -> bool:
    text = " ".join(
        str(job.get(key, "") or "")
        for key in ("title", "location", "description", "contract")
    )
    return has_any_phrase(text, tuple(FRESH_KEYWORDS))


def is_night_shift(schedule: Any) -> bool:
    text = normalize(schedule)
    if not text or text in {"tbc", "see listing"}:
        return False

    night_terms = (
        "night",
        "overnight",
        "nightshift",
        "night shift",
        "18:",
        "19:",
        "20:",
        "21:",
        "22:",
        "23:",
        "00:",
        "01:",
        "02:",
        "03:",
        "04:",
    )
    return any(term in text for term in night_terms)


def shift_priority(value: Any) -> int:
    text = normalize(value)
    if any(term in text for term in ("20:", "21:", "22:", "23:", "00:", "01:", "02:", "03:")):
        return 1
    if any(term in text for term in ("18:", "19:")):
        return 2
    if any(term in text for term in ("14:", "15:", "16:", "17:")):
        return 3
    return 4


def parse_hours(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None

    text = str(value).strip()
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        hours = int(float(match.group()))
    except ValueError:
        return None

    if hours < 0 or hours > 80:
        return None
    return hours


def parse_pay(value: Any) -> float:
    if value in (None, ""):
        return 0.0

    text = str(value).replace("£", "").replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return 0.0

    try:
        pay = float(match.group())
    except ValueError:
        return 0.0

    if pay < 0 or pay > 100:
        return 0.0
    return round(pay, 2)


def clean_postcode(postcode: str) -> str:
    return str(postcode or "").replace(" ", "").upper().strip()


def extract_postcode(value: Any) -> str:
    text = str(value or "").upper()
    match = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", text)
    if match:
        return match.group(1).replace(" ", "")
    return ""


def build_location(city: str, state: str, geo: str, postcode: str) -> str:
    city = str(city or "").strip()
    state = str(state or "").strip()
    geo = str(geo or "").strip()
    postcode = str(postcode or "").strip().upper()

    parts: list[str] = []
    if city:
        parts.append(city)
    if state and normalize(state) != normalize(city):
        parts.append(state)

    base = ", ".join(parts)

    if geo and postcode:
        return f"{base} ({geo}) {postcode}".strip()
    if geo:
        return f"{base} ({geo})".strip()
    if postcode:
        return f"{base} {postcode}".strip()
    return base or "Unknown UK Location"


def score_job(job: dict[str, Any]) -> tuple[int, bool]:
    hours = parse_hours(job.get("hours"))
    contract = normalize(job.get("contract", ""))
    schedule = job.get("schedule", "")

    # Only skip part-time when hours are explicitly known.
    if hours is not None and hours < MIN_FULL_TIME_HOURS:
        log.info("[JOB_REJECTED] reason=part_time_hours id=%s hours=%s", job.get("id"), hours)
        return 0, True

    score = 0
    if "permanent" in contract:
        score += SCORE_PERMANENT
    if is_night_shift(schedule):
        score += SCORE_NIGHT_SHIFT
    if hours and hours >= MIN_FULL_TIME_HOURS:
        score += SCORE_FULL_TIME_HOURS
    if parse_pay(job.get("pay")) > 0:
        score += SCORE_PAY_PRESENT
    if schedule:
        score += SCORE_SCHEDULE_PRESENT

    return score, False


def job_matches_type(job: dict[str, Any], job_type: str) -> bool:
    job_type = normalize(job_type)
    contract = normalize(job.get("contract", ""))
    hours = parse_hours(job.get("hours"))

    if job_type in ("both", "any", ""):
        return True

    if job_type == "fulltime":
        if hours is not None:
            return hours >= MIN_FULL_TIME_HOURS
        return any(term in contract for term in ("full", "full-time", "full time"))

    if job_type == "parttime":
        if hours is not None:
            return hours < MIN_FULL_TIME_HOURS
        return any(term in contract for term in ("part", "part-time", "part time", "reduced", "flex"))

    return True


def resolve_location(location: str) -> str:
    loc = normalize(location)
    if not loc:
        return ""

    if loc in CITY_POSTCODES:
        return CITY_POSTCODES[loc]

    for city, postcode in CITY_POSTCODES.items():
        city_norm = normalize(city)
        if loc == city_norm or city_norm in loc or loc in city_norm:
            return postcode

    extracted = extract_postcode(location)
    return extracted or str(location).strip().upper()


def haversine_miles(origin: Coordinates, destination: Coordinates) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, [origin.lat, origin.lon, destination.lat, destination.lon])
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(a))


async def get_coords(postcode: str) -> tuple[Optional[float], Optional[float]]:
    coords = await postcode_client.get_coords(postcode)
    if coords is None:
        return None, None
    return coords.lat, coords.lon


async def job_distance_miles(job_postcode: str, location: str) -> Optional[float]:
    try:
        user_postcode = resolve_location(location)
        job_postcode = resolve_location(job_postcode)

        origin = await postcode_client.get_coords(user_postcode)
        destination = await postcode_client.get_coords(job_postcode)

        if origin is None or destination is None:
            return None

        return round(haversine_miles(origin, destination), 1)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("[DISTANCE_ERROR] job_postcode=%s location=%s error=%s", job_postcode, location, exc)
        return None


def parse_card(card: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        if not isinstance(card, dict):
            log.info("[JOB_REJECTED] reason=card_not_dict")
            return None

        nested_job = card.get("job") if isinstance(card.get("job"), dict) else {}

        job_id = str(
            first_value(
                card,
                ("jobId",),
                ("id",),
                ("job_id",),
                ("job", "id"),
                ("job", "jobId"),
                default="",
            )
            or ""
        ).strip()

        if not job_id and nested_job:
            job_id = str(nested_job.get("id") or nested_job.get("jobId") or "").strip()

        if not job_id:
            log.info("[JOB_REJECTED] reason=missing_job_id keys=%s", sorted(card.keys())[:30])
            return None

        title = str(
            first_value(
                card,
                ("jobTitle",),
                ("title",),
                ("name",),
                ("job", "title"),
                ("job", "jobTitle"),
                default="Warehouse Operative",
            )
            or "Warehouse Operative"
        ).strip()

        log.info("[JOB_SCAN] id=%s title=%s", job_id, title)

        if not is_warehouse_job(title):
            log.info("[JOB_REJECTED] reason=not_warehouse id=%s title=%s", job_id, title)
            return None

        hours = parse_hours(
            first_value(
                card,
                ("hoursPerWeek",),
                ("hours",),
                ("weeklyHours",),
                ("schedule", "hoursPerWeek"),
                ("job", "hoursPerWeek"),
                default=None,
            )
        )

        city = str(
            first_value(card, ("city",), ("locationName",), ("location", "city"), ("job", "city"), default="")
            or ""
        ).strip()

        state = str(
            first_value(card, ("state",), ("region",), ("location", "state"), default="England")
            or "England"
        ).strip()

        geo = str(
            first_value(card, ("geoClusterDescription",), ("location", "geoClusterDescription"), default="")
            or ""
        ).strip()

        postcode = str(
            first_value(card, ("postalCode",), ("postcode",), ("location", "postalCode"), ("job", "postalCode"), default="")
            or ""
        ).strip().upper()

        if not postcode:
            postcode = extract_postcode(" ".join(str(v) for v in (city, state, geo, card.get("location", ""))))

        pay_raw = first_value(
            card,
            ("totalPayRateMax",),
            ("totalPayRateMin",),
            ("payRate",),
            ("pay",),
            ("compensation", "max"),
            ("compensation", "min"),
            default=None,
        )
        pay = parse_pay(pay_raw)

        employment = str(
            first_value(card, ("employmentType",), ("contractType",), ("job", "employmentType"), default="")
            or ""
        ).strip()

        amazon_job_type = str(
            first_value(card, ("jobType",), ("job", "jobType"), default="")
            or ""
        ).strip()

        contract = employment or amazon_job_type or "Unknown"

        schedule = (
            first_value(
                card,
                ("shiftCode",),
                ("schedule",),
                ("scheduleDisplay",),
                ("schedule", "displayText"),
                ("job", "shiftCode"),
                default=None,
            )
            or None
        )
        schedule = str(schedule).strip() if schedule else None

        first_day = first_value(
            card,
            ("firstDayOnSite",),
            ("firstDay",),
            ("startDate",),
            ("job", "firstDayOnSite"),
            default=None,
        )

        location = build_location(city, state, geo, postcode)

        job = {
            "id": job_id,
            "title": title or "Warehouse Operative",
            "location": location,
            "postcode": postcode,
            "pay": pay,
            "pay_display": f"{pay:.2f}" if pay > 0 else None,
            "contract": contract,
            "firstDay": first_day or None,
            "schedule": schedule,
            "hours": str(hours) if hours is not None else None,
            "sched_count": first_value(card, ("scheduleCount",), ("schedulesCount",), default=0) or 0,
            "shifts": [],
            "description": first_value(card, ("description",), ("job", "description"), default=None),
            "link": f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}",
            "found_at": utc_now().isoformat(),
            "raw_keys": sorted(card.keys())[:40],
        }

        score, skipped = score_job(job)
        if skipped:
            return None

        job["score"] = score
        job["is_fresh"] = is_fresh_job(job)
        job["shift_priority"] = shift_priority(schedule or "")

        log.info(
            "[JOB_ACCEPTED] id=%s title=%s location=%s pay=%s hours=%s score=%s",
            job_id,
            title,
            location,
            pay,
            hours if hours is not None else "?",
            score,
        )

        return job

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[PARSE_ERROR] error=%s card=%s", exc, card)
        return None


async def close_session() -> None:
    await postcode_client.close()
