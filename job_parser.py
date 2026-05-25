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

CACHE_TTL = timedelta(minutes=10)
POSTCODE_API_TIMEOUT = 5
POSTCODE_RETRIES = 3
POSTCODE_CACHE_MAX_SIZE = 5000

SCORE_PERMANENT = 50
SCORE_NIGHT_SHIFT = 15
SCORE_FULL_TIME_HOURS = 10

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
    def __init__(self, max_size: int, ttl: timedelta):
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
        self._cache = TTLCache(
            max_size=POSTCODE_CACHE_MAX_SIZE,
            ttl=CACHE_TTL,
        )

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=POSTCODE_API_TIMEOUT)
            self._session = aiohttp.ClientSession(timeout=timeout)

        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

        self._session = None

    async def get_coords(self, postcode: str) -> Optional[Coordinates]:
        clean = clean_postcode(postcode)

        if not clean:
            return None

        spaced = str(postcode or "").strip().upper()

        if spaced in CITY_COORDS:
            lat, lon = CITY_COORDS[spaced]
            return Coordinates(float(lat), float(lon))

        if clean in CITY_COORDS:
            lat, lon = CITY_COORDS[clean]
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
                        log.debug(
                            "[POSTCODE_API_STATUS] postcode=%s status=%s attempt=%s",
                            postcode,
                            response.status,
                            attempt,
                        )
                        await asyncio.sleep(0.3 * attempt)
                        continue

                    data = await response.json()
                    result = data.get("result") or {}

                    lat = result.get("latitude")
                    lon = result.get("longitude")

                    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                        return Coordinates(float(lat), float(lon))

                    log.debug("[POSTCODE_INVALID_RESPONSE] postcode=%s data=%s", postcode, data)
                    return None

            except asyncio.CancelledError:
                raise

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                log.debug(
                    "[POSTCODE_API_ERROR] postcode=%s attempt=%s error=%s",
                    postcode,
                    attempt,
                    exc,
                )
                await asyncio.sleep(0.3 * attempt)

        return None


postcode_client = PostcodeClient()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize(value: Any) -> str:
    text = str(value or "").lower().strip()
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text)


@lru_cache(maxsize=1000)
def phrase_pattern(phrase: str) -> re.Pattern[str]:
    phrase = normalize(phrase)

    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    return re.compile(rf"\b{escaped}\b")


def contains_phrase(text: str, phrase: str) -> bool:
    phrase = normalize(phrase)

    if not phrase:
        return False

    return phrase_pattern(phrase).search(text) is not None


def has_any_phrase(text: str, phrases: list[str] | tuple[str, ...]) -> bool:
    text = normalize(text)
    return any(contains_phrase(text, phrase) for phrase in phrases)


def is_warehouse_job(title: str) -> bool:
    title_norm = normalize(title)

    if not title_norm:
        return False

    if has_any_phrase(title_norm, tuple(BLOCKED_KEYWORDS)):
        return False

    return has_any_phrase(title_norm, tuple(WAREHOUSE_KEYWORDS))


def is_fresh_job(job: dict[str, Any]) -> bool:
    text = f"{job.get('title', '')} {job.get('location', '')}"
    return has_any_phrase(text, tuple(FRESH_KEYWORDS))


def is_night_shift(schedule: Any) -> bool:
    text = normalize(schedule)

    if not text or text == "tbc":
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
    )

    return any(term in text for term in night_terms)


def shift_priority(value: Any) -> int:
    text = normalize(value)

    if any(term in text for term in ("18:", "19:", "20:", "21:", "22:", "23:")):
        return 1

    if any(term in text for term in ("14:", "15:", "16:")):
        return 2

    return 3


def parse_hours(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None

    try:
        hours = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None

    if hours < 0 or hours > 80:
        return None

    return hours


def parse_pay(value: Any) -> float:
    if value in (None, ""):
        return 0.0

    try:
        clean = str(value).replace("£", "").replace(",", "").strip()
        pay = float(clean)
    except (TypeError, ValueError):
        return 0.0

    if pay < 0 or pay > 100:
        return 0.0

    return round(pay, 2)


def score_job(job: dict[str, Any]) -> tuple[int, bool]:
    hours = parse_hours(job.get("hours"))
    contract = normalize(job.get("contract", ""))
    schedule = job.get("schedule", "")

    if hours is not None and hours < MIN_FULL_TIME_HOURS:
        log.info("[SKIP] Part-time %shrs", hours)
        return 0, True

    score = 0

    if "permanent" in contract:
        score += SCORE_PERMANENT

    if is_night_shift(schedule):
        score += SCORE_NIGHT_SHIFT

    if hours and hours >= MIN_FULL_TIME_HOURS:
        score += SCORE_FULL_TIME_HOURS

    return score, False


def job_matches_type(job: dict[str, Any], job_type: str) -> bool:
    job_type = normalize(job_type)
    contract = normalize(job.get("contract", ""))

    if job_type in ("both", "any", ""):
        return True

    if job_type == "fulltime":
        return any(term in contract for term in ("full", "full-time", "full time"))

    if job_type == "parttime":
        return any(term in contract for term in ("part", "reduced", "flex"))

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

    return str(location).strip().upper()


def haversine_miles(origin: Coordinates, destination: Coordinates) -> float:
    lat1, lon1, lat2, lon2 = map(
        math.radians,
        [origin.lat, origin.lon, destination.lat, destination.lon],
    )

    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin((lon2 - lon1) / 2) ** 2
    )

    return EARTH_RADIUS_MILES * 2 * math.asin(math.sqrt(a))


def clean_postcode(postcode: str) -> str:
    return str(postcode or "").replace(" ", "").upper().strip()


async def close_session() -> None:
    await postcode_client.close()


async def get_coords(postcode: str) -> tuple[Optional[float], Optional[float]]:
    coords = await postcode_client.get_coords(postcode)

    if coords is None:
        return None, None

    return coords.lat, coords.lon


async def job_distance_miles(job_postcode: str, location: str) -> Optional[float]:
    try:
        user_postcode = resolve_location(location)

        origin = await postcode_client.get_coords(user_postcode)
        destination = await postcode_client.get_coords(job_postcode)

        if origin is None or destination is None:
            return None

        return round(haversine_miles(origin, destination), 1)

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        log.debug(
            "[DISTANCE_ERROR] job_postcode=%s location=%s error=%s",
            job_postcode,
            location,
            exc,
        )
        return None


def build_location(city: str, state: str, geo: str, postcode: str) -> str:
    city = str(city or "").strip()
    state = str(state or "").strip()
    geo = str(geo or "").strip()
    postcode = str(postcode or "").strip().upper()

    parts = []

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


def parse_card(card: dict[str, Any]) -> Optional[dict[str, Any]]:
    try:
        job_id = str(card.get("jobId") or "").strip()

        if not job_id:
            return None

        title = str(card.get("jobTitle") or "").strip()
        log.info("[SCAN] %s — %s", job_id, title)

        if not is_warehouse_job(title):
            log.info("[SKIP] Not warehouse: %s", title)
            return None

        hours = parse_hours(card.get("hoursPerWeek"))

        if hours is not None and hours < MIN_FULL_TIME_HOURS:
            log.info("[SKIP] Part-time %shrs", hours)
            return None

        city = str(card.get("city") or card.get("locationName") or "").strip()
        state = str(card.get("state") or "England").strip()
        postcode = str(card.get("postalCode") or "").strip().upper()
        geo = str(card.get("geoClusterDescription") or "").strip()

        pay_raw = card.get("totalPayRateMax") or card.get("totalPayRateMin")
        pay = parse_pay(pay_raw)

        employment = str(card.get("employmentType") or "").strip()
        amazon_job_type = str(card.get("jobType") or "").strip()
        contract = employment or amazon_job_type or "Seasonal"

        schedule = str(card.get("shiftCode") or "").strip() or None
        location = build_location(city, state, geo, postcode)

        job = {
            "id": job_id,
            "title": title,
            "location": location,
            "postcode": postcode,
            "pay": pay,
            "pay_display": f"{pay:.2f}",
            "contract": contract,
            "firstDay": card.get("firstDayOnSite") or None,
            "schedule": schedule,
            "hours": str(hours) if hours is not None else None,
            "sched_count": card.get("scheduleCount", 0) or 0,
            "shifts": [],
            "description": None,
            "link": f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}",
            "found_at": utc_now().isoformat(),
        }

        score, skipped = score_job(job)

        if skipped:
            return None

        job["score"] = score
        job["is_fresh"] = is_fresh_job(job)
        job["shift_priority"] = shift_priority(schedule or "")

        log.info(
            "[ACCEPT] %s — %s £%s/hr score=%s",
            title,
            location,
            pay,
            score,
        )

        return job

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        log.warning("[PARSE_ERROR] %s card=%s", exc, card)
        return None
