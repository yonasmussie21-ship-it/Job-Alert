import math
import logging
import aiohttp
from datetime import datetime, timedelta

from config import (
    WAREHOUSE_KEYWORDS,
    BLOCKED_KEYWORDS,
    FRESH_KEYWORDS,
    CITY_POSTCODES,
    CITY_COORDS,
)

log = logging.getLogger(__name__)

SESSION: aiohttp.ClientSession | None = None

# Cache with expiry (prevents permanent caching of API failures)
POSTCODE_CACHE: dict[str, tuple[float | None, float | None, datetime]] = {}
CACHE_TTL = timedelta(minutes=10)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

async def get_session() -> aiohttp.ClientSession:
    global SESSION

    if SESSION is None or SESSION.closed:
        SESSION = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5)
        )

    return SESSION


async def close_session() -> None:
    global SESSION

    if SESSION and not SESSION.closed:
        await SESSION.close()

    SESSION = None


# ─────────────────────────────────────────────────────────────────────────────
# TEXT NORMALIZATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    return str(text).lower().strip().replace("–", "-")


# ─────────────────────────────────────────────────────────────────────────────
# JOB FILTERS
# ─────────────────────────────────────────────────────────────────────────────

def is_warehouse_job(title: str) -> bool:
    if not title:
        return False

    title = normalize(title)
    tokens = set(title.replace("-", " ").split())

    # Blocked keywords as tokens
    for blocked in BLOCKED_KEYWORDS:
        b = normalize(blocked)
        if b in tokens:
            return False

    return any(k in title for k in WAREHOUSE_KEYWORDS)


def is_fresh_job(job: dict) -> bool:
    title = normalize(job.get("title", ""))
    location = normalize(job.get("location", ""))
    return any(kw in title or kw in location for kw in FRESH_KEYWORDS)


def is_night_shift(schedule) -> bool:
    if not schedule or schedule == "TBC":
        return False

    text = normalize(str(schedule))

    night_terms = [
        "night", "overnight", "nightshift", "night shift",
        "18:", "19:", "20:", "21:", "22:", "23:",
        "00:", "01:", "02:", "03:",
    ]

    return any(term in text for term in night_terms)


def shift_priority(text) -> int:
    text = normalize(str(text))

    if any(term in text for term in ["18:", "19:", "20:", "21:", "22:", "23:"]):
        return 1

    if any(term in text for term in ["14:", "15:", "16:"]):
        return 2

    return 3


def score_job(job: dict) -> tuple[int, bool]:
    hours = job.get("hours")
    contract = normalize(job.get("contract", ""))
    schedule = job.get("schedule", "")

    if hours:
        try:
            if int(hours) < 36:
                log.info("[SKIP] Part-time %shrs", hours)
                return 0, True
        except Exception:
            pass

    score = 0

    if "permanent" in contract:
        score += 50

    if is_night_shift(schedule):
        score += 15

    return score, False


def job_matches_type(job: dict, job_type: str) -> bool:
    if job_type == "both":
        return True

    contract = normalize(job.get("contract", ""))

    if job_type == "fulltime":
        return "full" in contract

    if job_type == "parttime":
        return any(x in contract for x in ["part", "reduced", "flex"] )

    return True


# ─────────────────────────────────────────────────────────────────────────────
# LOCATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def resolve_location(location: str) -> str:
    loc = normalize(location)

    if loc in CITY_POSTCODES:
        return CITY_POSTCODES[loc]

    for city, postcode in CITY_POSTCODES.items():
        if loc in city or city in loc:
            return postcode

    return str(location).upper()


def haversine_miles(lat1, lon1, lat2, lon2) -> float:
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


async def get_postcode_coords_api(postcode: str) -> tuple[float | None, float | None]:
    clean = postcode.replace(" ", "").upper()

    # Cache check with TTL
    if clean in POSTCODE_CACHE:
        lat, lon, ts = POSTCODE_CACHE[clean]
        if datetime.utcnow() - ts < CACHE_TTL:
            return lat, lon

    try:
        session = await get_session()

        async with session.get(f"https://api.postcodes.io/postcodes/{clean}") as response:
            data = await response.json()

            if data.get("status") == 200:
                result = data.get("result", {})
                coords = result.get("latitude"), result.get("longitude")
                POSTCODE_CACHE[clean] = (*coords, datetime.utcnow())
                return coords

    except Exception as e:
        log.debug("[POSTCODE_API_ERROR] %s", e)

    POSTCODE_CACHE[clean] = (None, None, datetime.utcnow())
    return None, None


async def get_coords(postcode: str) -> tuple[float | None, float | None]:
    clean = postcode.strip().upper()

    if clean in CITY_COORDS:
        return CITY_COORDS[clean]

    return await get_postcode_coords_api(clean)


async def job_distance_miles(job_postcode: str, location: str) -> float | None:
    try:
        postcode = resolve_location(location)

        lat1, lon1 = await get_coords(postcode)
        lat2, lon2 = await get_coords(job_postcode)

        if all(value is not None for value in [lat1, lon1, lat2, lon2]):
            return round(haversine_miles(lat1, lon1, lat2, lon2), 1)

    except Exception as e:
        log.debug("[DISTANCE_ERROR] %s", e)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# PARSE AMAZON JOB CARD
# ─────────────────────────────────────────────────────────────────────────────

def parse_card(card: dict) -> dict | None:
    try:
        job_id = str(card.get("jobId", ""))

        if not job_id:
            return None

        title = card.get("jobTitle", "") or ""

        log.info("[SCAN] %s — %s", job_id, title)

        if not is_warehouse_job(title):
            log.info("[SKIP] Not warehouse: %s", title)
            return None

        city = card.get("city") or card.get("locationName") or ""
        state = card.get("state") or "England"
        postcode = card.get("postalCode") or ""
        geo = card.get("geoClusterDescription") or ""

        pay_raw = card.get("totalPayRateMax") or card.get("totalPayRateMin") or 0

        try:
            pay = float(str(pay_raw).replace("£", "").strip())
        except Exception:
            pay = 0.0

        employment = card.get("employmentType") or ""
        job_type = card.get("jobType") or ""
        contract = employment or job_type or "Seasonal"

        hours = None

        if card.get("hoursPerWeek"):
            try:
                hours = str(int(card.get("hoursPerWeek")))
            except Exception:
                hours = None

        if hours and int(hours) < 36:
            log.info("[SKIP] Part-time %shrs", hours)
            return None

        first_day = card.get("firstDayOnSite") or None
        sched_count = card.get("scheduleCount", 0)
        shift_code = card.get("shiftCode") or ""
        schedule = shift_code if shift_code else None

        parts = []

        if city:
            parts.append(city)

        if state and state != city:
            parts.append(state)

        # Clean location formatting
        parts_clean = ", ".join(p.strip() for p in parts if p.strip())

        if geo and postcode:
            location = f"{parts_clean} ({geo}) {postcode}"
        elif geo:
            location = f"{parts_clean} ({geo})"
        elif postcode:
            location = f"{parts_clean} {postcode}"
        else:
            location = parts_clean or "Unknown UK Location"

        log.info("[ACCEPT] %s — %s £%s/hr", title, location, pay)

        return {
            "id": job_id,
            "title": title,
            "location": location,
            "postcode": postcode,
            "pay": round(pay, 2),
            "pay_display": f"{pay:.2f}",
            "contract": contract,
            "firstDay": first_day,
            "schedule": schedule,
            "hours": hours,
            "sched_count": sched_count,
            "shifts": [],
            "description": None,
            "link": f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}",
            "found_at": datetime.utcnow().isoformat(),
        }

    except Exception as e:
        log.warning("[ERROR] Parse error: %s", e)
        return None
