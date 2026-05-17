import asyncio
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import aiohttp

from config import (
    WAREHOUSE_KEYWORDS,
    BLOCKED_KEYWORDS,
    FRESH_KEYWORDS,
    CITY_POSTCODES,
    CITY_COORDS,
)

log = logging.getLogger(__name__)

SESSION: Optional[aiohttp.ClientSession] = None
POSTCODE_CACHE: dict[str, tuple[Optional[float], Optional[float], datetime]] = {}

CACHE_TTL = timedelta(minutes=10)
POSTCODE_API_TIMEOUT = 5
POSTCODE_RETRIES = 2


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize(text: Any) -> str:
    text = str(text or "").lower().strip()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text


def contains_phrase(text: str, phrase: str) -> bool:
    phrase = normalize(phrase).strip()

    if not phrase:
        return False

    pattern = r"\b" + re.escape(phrase).replace(r"\ ", r"\s+") + r"\b"
    return re.search(pattern, text) is not None


def is_warehouse_job(title: str) -> bool:
    title_norm = normalize(title)

    if not title_norm:
        return False

    if any(contains_phrase(title_norm, blocked) for blocked in BLOCKED_KEYWORDS):
        return False

    return any(contains_phrase(title_norm, keyword) for keyword in WAREHOUSE_KEYWORDS)


def is_fresh_job(job: Dict[str, Any]) -> bool:
    text = normalize(f"{job.get('title', '')} {job.get('location', '')}")
    return any(contains_phrase(text, kw) for kw in FRESH_KEYWORDS)


def is_night_shift(schedule: Any) -> bool:
    text = normalize(schedule)

    if not text or text == "tbc":
        return False

    return any(
        term in text
        for term in [
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
        ]
    )


def shift_priority(text: Any) -> int:
    text = normalize(text)

    if any(term in text for term in ["18:", "19:", "20:", "21:", "22:", "23:"]):
        return 1

    if any(term in text for term in ["14:", "15:", "16:"]):
        return 2

    return 3


def parse_hours(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None

    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


def parse_pay(value: Any) -> float:
    if value in (None, ""):
        return 0.0

    try:
        clean = str(value).replace("£", "").replace(",", "").strip()
        return round(float(clean), 2)
    except Exception:
        return 0.0


def score_job(job: Dict[str, Any]) -> tuple[int, bool]:
    hours = parse_hours(job.get("hours"))
    contract = normalize(job.get("contract", ""))
    schedule = job.get("schedule", "")

    if hours is not None and hours < 36:
        log.info("[SKIP] Part-time %shrs", hours)
        return 0, True

    score = 0

    if "permanent" in contract:
        score += 50

    if is_night_shift(schedule):
        score += 15

    if hours and hours >= 36:
        score += 10

    return score, False


def job_matches_type(job: Dict[str, Any], job_type: str) -> bool:
    job_type = normalize(job_type)
    contract = normalize(job.get("contract", ""))

    if job_type in ("both", "any", ""):
        return True

    if job_type == "fulltime":
        return "full" in contract or "full-time" in contract or "full time" in contract

    if job_type == "parttime":
        return any(x in contract for x in ["part", "reduced", "flex"])

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


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_miles = 3958.8

    lat1, lon1, lat2, lon2 = map(
        math.radians,
        [lat1, lon1, lat2, lon2],
    )

    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin((lon2 - lon1) / 2) ** 2
    )

    return radius_miles * 2 * math.asin(math.sqrt(a))


def clean_postcode(postcode: str) -> str:
    return str(postcode or "").replace(" ", "").upper().strip()


async def get_session() -> aiohttp.ClientSession:
    global SESSION

    if SESSION is None or SESSION.closed:
        timeout = aiohttp.ClientTimeout(total=POSTCODE_API_TIMEOUT)
        SESSION = aiohttp.ClientSession(timeout=timeout)

    return SESSION


async def close_session() -> None:
    global SESSION

    if SESSION and not SESSION.closed:
        await SESSION.close()

    SESSION = None


async def get_postcode_coords_api(postcode: str) -> tuple[Optional[float], Optional[float]]:
    clean = clean_postcode(postcode)

    if not clean:
        return None, None

    cached = POSTCODE_CACHE.get(clean)

    if cached:
        lat, lon, ts = cached
        if utc_now() - ts < CACHE_TTL:
            return lat, lon

    url = f"https://api.postcodes.io/postcodes/{clean}"

    for attempt in range(1, POSTCODE_RETRIES + 1):
        try:
            session = await get_session()

            async with session.get(url) as response:
                if response.status != 200:
                    log.debug("[POSTCODE_API_STATUS] postcode=%s status=%s", clean, response.status)
                    continue

                data = await response.json()

                result = data.get("result") or {}
                lat = result.get("latitude")
                lon = result.get("longitude")

                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    POSTCODE_CACHE[clean] = (float(lat), float(lon), utc_now())
                    return float(lat), float(lon)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            log.debug("[POSTCODE_API_ERROR] postcode=%s attempt=%s error=%s", clean, attempt, e)

        await asyncio.sleep(0.3 * attempt)

    POSTCODE_CACHE[clean] = (None, None, utc_now())
    return None, None


async def get_coords(postcode: str) -> tuple[Optional[float], Optional[float]]:
    clean = clean_postcode(postcode)

    if not clean:
        return None, None

    spaced = str(postcode or "").strip().upper()

    if spaced in CITY_COORDS:
        return CITY_COORDS[spaced]

    if clean in CITY_COORDS:
        return CITY_COORDS[clean]

    return await get_postcode_coords_api(clean)


async def job_distance_miles(job_postcode: str, location: str) -> Optional[float]:
    try:
        user_postcode = resolve_location(location)

        lat1, lon1 = await get_coords(user_postcode)
        lat2, lon2 = await get_coords(job_postcode)

        if None in (lat1, lon1, lat2, lon2):
            return None

        return round(haversine_miles(float(lat1), float(lon1), float(lat2), float(lon2)), 1)

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log.debug("[DISTANCE_ERROR] job_postcode=%s location=%s error=%s", job_postcode, location, e)
        return None


def build_location(city: str, state: str, geo: str, postcode: str) -> str:
    parts = []

    if city:
        parts.append(city.strip())

    if state and normalize(state) != normalize(city):
        parts.append(state.strip())

    base = ", ".join(parts)

    if geo and postcode:
        return f"{base} ({geo.strip()}) {postcode.strip()}".strip()

    if geo:
        return f"{base} ({geo.strip()})".strip()

    if postcode:
        return f"{base} {postcode.strip()}".strip()

    return base or "Unknown UK Location"


def parse_card(card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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

        if hours is not None and hours < 36:
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

        shift_code = str(card.get("shiftCode") or "").strip()
        schedule = shift_code or None

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

        log.info("[ACCEPT] %s — %s £%s/hr score=%s", title, location, pay, score)

        return job

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log.warning("[PARSE_ERROR] %s card=%s", e, card)
        return None
