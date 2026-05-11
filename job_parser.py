import math
import logging
import aiohttp
from datetime import datetime
from config import (
    WAREHOUSE_KEYWORDS, BLOCKED_KEYWORDS, FRESH_KEYWORDS,
    CITY_POSTCODES, CITY_COORDS
)

log = logging.getLogger(__name__)

# ─── FILTERS ─────────────────────────────────────────────────────────────────
def is_warehouse_job(title: str) -> bool:
    if not title:
        return False
    title = title.lower().strip()
    if any(b in title for b in BLOCKED_KEYWORDS):
        return False
    return any(k in title for k in WAREHOUSE_KEYWORDS)

def is_fresh_job(job) -> bool:
    title    = job.get("title", "").lower()
    location = job.get("location", "").lower()
    return any(kw in title or kw in location for kw in FRESH_KEYWORDS)

def is_night_shift(schedule) -> bool:
    if not schedule or schedule == "TBC":
        return False
    return any(t in str(schedule) for t in [
        "18:30","19:00","20:00","21:00","22:00","23:00","23:45",
        "0:00","1:00","2:00","3:00"
    ])

def shift_priority(text) -> int:
    text = str(text)
    if any(t in text for t in ["18:30","19:00","20:00","21:00","22:00","23:00","23:45"]):
        return 1
    if any(t in text for t in ["14:00","15:00","16:00"]):
        return 2
    return 3

def score_job(job):
    """Score job quality. Returns (score, skip)."""
    hours    = job.get("hours")
    contract = job.get("contract", "").lower()
    schedule = job.get("schedule", "")

    if hours:
        try:
            if int(hours) < 36:
                log.info(f"⏭️ Skipped (part-time {hours}hrs)")
                return 0, True
        except:
            pass

    score = 0
    if "permanent" in contract:   score += 50
    if is_night_shift(schedule):  score += 15
    return score, False

def job_matches_type(job, job_type) -> bool:
    if job_type == "both":
        return True
    contract = job.get("contract", "").lower()
    if job_type == "fulltime":
        return "full" in contract
    if job_type == "parttime":
        return "part" in contract or "reduced" in contract
    return True

# ─── LOCATION HELPERS ─────────────────────────────────────────────────────────
def resolve_location(location) -> str:
    loc = location.lower().strip()
    if loc in CITY_POSTCODES:
        return CITY_POSTCODES[loc]
    for city, postcode in CITY_POSTCODES.items():
        if loc in city or city in loc:
            return postcode
    return location.upper()

def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    a = (math.sin((lat2-lat1)/2)**2 +
         math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

async def get_postcode_coords_api(postcode):
    try:
        clean = postcode.replace(" ", "").upper()
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"https://api.postcodes.io/postcodes/{clean}",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                data = await r.json()
                if data.get("status") == 200:
                    return data["result"]["latitude"], data["result"]["longitude"]
    except:
        pass
    return None, None

async def get_coords(postcode):
    clean = postcode.strip().upper()
    if clean in CITY_COORDS:
        return CITY_COORDS[clean]
    return await get_postcode_coords_api(clean)

async def job_distance_miles(job_postcode, location):
    try:
        postcode   = resolve_location(location)
        lat1, lon1 = await get_coords(postcode)
        lat2, lon2 = await get_coords(job_postcode)
        if all(x is not None for x in [lat1, lon1, lat2, lon2]):
            return round(haversine_miles(lat1, lon1, lat2, lon2), 1)
    except:
        pass
    return None

# ─── PARSE CARD ──────────────────────────────────────────────────────────────
def parse_card(card) -> dict | None:
    try:
        job_id = str(card.get("jobId", ""))
        if not job_id:
            return None

        title = card.get("jobTitle", "") or ""
        log.info(f"🔍 [{job_id}] {title}")

        if not is_warehouse_job(title):
            log.info(f"⏭️ Skipped (not warehouse): {title}")
            return None

        city        = card.get("city") or card.get("locationName") or ""
        state       = card.get("state") or "England"
        postcode    = card.get("postalCode") or ""
        geo         = card.get("geoClusterDescription") or ""
        pay         = float(card.get("totalPayRateMax") or card.get("totalPayRateMin") or 0)
        employment  = card.get("employmentType") or ""
        job_type    = card.get("jobType") or ""
        contract    = employment or job_type or "Seasonal"
        hours       = str(int(card.get("hoursPerWeek"))) if card.get("hoursPerWeek") else None
        first_day   = card.get("firstDayOnSite") or None
        sched_count = card.get("scheduleCount", 0)
        shift_code  = card.get("shiftCode") or ""
        schedule    = shift_code if shift_code else None

        if hours:
            try:
                if int(hours) < 36:
                    log.info(f"⏭️ Skipped (part-time {hours}hrs)")
                    return None
            except:
                pass

        parts = []
        if city: parts.append(city)
        if state and state != city: parts.append(state)
        if geo and postcode:   location = f"{', '.join(parts)} ({geo}) {postcode}".strip()
        elif geo:              location = f"{', '.join(parts)} ({geo})".strip()
        elif postcode:         location = f"{', '.join(parts)} {postcode}".strip()
        else:                  location = ", ".join(parts) or "Unknown UK Location"

        log.info(f"✅ Accepted: {title} — {location} £{pay}/hr")

        return {
            "id":          job_id,
            "title":       title,
            "location":    location,
            "postcode":    postcode,
            "pay":         round(pay, 2),
            "pay_display": f"{pay:.2f}",
            "contract":    contract,
            "firstDay":    first_day,
            "schedule":    schedule,
            "hours":       hours,
            "sched_count": sched_count,
            "shifts":      [],
            "description": None,
            "link":        f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}",
            "found_at":    datetime.utcnow().isoformat(),
        }
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None
