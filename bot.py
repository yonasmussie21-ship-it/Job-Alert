import asyncio
import os
import json
import logging
import aiohttp
from datetime import datetime, timedelta
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
CHAT_ID          = os.environ.get("CHAT_ID", "1027065157")
BRIGHT_DATA_USER = os.environ.get("BRIGHT_DATA_USER", "")
BRIGHT_DATA_PASS = os.environ.get("BRIGHT_DATA_PASS", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs        = {}
active_jobs       = {}
bot_paused        = False
awaiting_location = False
awaiting_loc_num  = 0

user_locations = {
    1: {"city": "", "lat": 0.0, "lng": 0.0},
    2: {"city": "", "lat": 0.0, "lng": 0.0},
    3: {"city": "", "lat": 0.0, "lng": 0.0},
}

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── AMAZON GRAPHQL API ───────────────────────────────────────────────────────
AMAZON_GRAPHQL_URL = "https://www.jobsatamazon.co.uk/graphql"

GRAPHQL_QUERY = """
query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {
  searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {
    nextToken
    jobCards {
      jobId
      jobTitle
      jobType
      employmentType
      city
      state
      postalCode
      locationName
      totalPayRateMin
      totalPayRateMax
      distance
      scheduleCount
      currencyCode
      geoClusterDescription
      jobTypeL10N
      employmentTypeL10N
      totalPayRateMinL10N
      totalPayRateMaxL10N
      __typename
    }
    __typename
  }
}
"""

AMAZON_HEADERS = {
    "authority":         "www.jobsatamazon.co.uk",
    "accept":            "*/*",
    "accept-encoding":   "gzip, deflate, br",
    "accept-language":   "en-GB,en;q=0.9",
    "content-type":      "application/json",
    "country":           "United Kingdom",
    "iscanary":          "false",
    "origin":            "https://www.jobsatamazon.co.uk",
    "referer":           "https://www.jobsatamazon.co.uk/",
    "sec-ch-ua":         '"Chromium";v="146", "Not-A-Brand";v="24", "Google Chrome";v="146"',
    "sec-ch-ua-mobile":  "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":    "empty",
    "sec-fetch-mode":    "cors",
    "sec-fetch-site":    "same-origin",
    "user-agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
async def tg_send(text, reply_markup=None):
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    except Exception as e:
        log.error(f"Telegram error: {e}")

async def tg_alert(job, status="new"):
    dist      = job.get("distance_miles", 999)
    score     = job.get("score", 0)
    expiry    = job.get("expiry")
    mins      = int((expiry - datetime.utcnow()).total_seconds() / 60) if expiry else 120

    urgency = "🚨 NEW AMAZON JOB — ACT NOW!"

    if status == "reminder": urgency = f"⚠️ REMINDER — {mins} mins left!"
    elif status == "final":  urgency = f"🚨 FINAL WARNING — {mins} mins left!"

    text = f"""🚨 <b>{urgency}</b>
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
💰 <b>£{job.get('pay', '?')}/hr</b>
⏱️ {job.get('contract', '?')}
⭐ Score: <b>{score}/100</b>
🌍 <b>UK Wide Job</b>
⏳ <b>{mins} mins remaining</b>
━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Tap below to apply NOW!</b>
━━━━━━━━━━━━━━━━━━━━━"""

    markup = {
        "inline_keyboard": [
            [{"text": "🚀 APPLY NOW", "url": job.get("link", "https://www.jobsatamazon.co.uk")}],
            [
                {"text": "✅ APPLIED",  "callback_data": f"applied_{job['id']}"},
                {"text": "⏭️ SKIP",    "callback_data": f"skip_{job['id']}"}
            ]
        ]
    }
    await tg_send(text, markup)

# ─── LOCATION SETUP ──────────────────────────────────────────────────────────
async def ask_location(slot_num):
    global awaiting_location, awaiting_loc_num
    awaiting_location = True
    awaiting_loc_num  = slot_num
    labels = {1: "1ST CHOICE 🔴", 2: "2ND CHOICE 🟡", 3: "3RD CHOICE 🟢"}
    await tg_send(f"""📍 <b>Enter your {labels[slot_num]} location:</b>

Type a UK city or postcode:
• Birmingham
• Coventry
• Leicester
• B1 1BB""")

async def save_location(slot_num, city_input):
    global awaiting_location, awaiting_loc_num
    try:
        geolocator = Nominatim(user_agent="amazon-shift-holder-v3")
        location   = geolocator.geocode(f"{city_input}, UK")
        if location:
            user_locations[slot_num] = {
                "city": city_input.title(),
                "lat":  location.latitude,
                "lng":  location.longitude
            }
            awaiting_location = False
            if slot_num < 3:
                await tg_send(f"✅ <b>{city_input.title()}</b> saved as choice {slot_num}!")
                await ask_location(slot_num + 1)
            else:
                locs = "\n".join([
                    f"{'🔴' if i==1 else '🟡' if i==2 else '🟢'} Choice {i}: <b>{user_locations[i]['city']}</b>"
                    for i in range(1,4) if user_locations[i]['city']
                ])
                await tg_send(f"""✅ <b>All locations saved!</b>
━━━━━━━━━━━━━━━━━━━
{locs}
━━━━━━━━━━━━━━━━━━━
🚀 <b>Bot is LIVE — watching ALL UK jobs!</b>
⚡ Checking every 10 seconds!""")
        else:
            await tg_send(f"❌ Couldn't find <b>{city_input}</b>. Please try again!")
    except Exception as e:
        log.error(f"Geocode error: {e}")
        user_locations[slot_num] = {"city": city_input.title(), "lat": 52.4862, "lng": -1.8904}
        awaiting_location = False
        if slot_num < 3:
            await tg_send(f"✅ {city_input.title()} saved!")
            await ask_location(slot_num + 1)
        else:
            await tg_send(f"✅ All locations saved! Bot is watching! 🚀")

# ─── DISTANCE & PRIORITY ─────────────────────────────────────────────────────
def get_location_priority(job_lat, job_lng):
    best_priority = 0
    best_distance = 9999
    try:
        job_coords = (float(job_lat), float(job_lng))
        for num in range(1, 4):
            loc = user_locations[num]
            if not loc["city"] or not loc["lat"]:
                continue
            dist = round(geodesic((loc["lat"], loc["lng"]), job_coords).miles, 1)
            if dist < best_distance:
                best_distance = dist
                best_priority = num
    except Exception as e:
        log.warning(f"Distance error: {e}")
    return best_priority, best_distance

# ─── JOB SCORING ─────────────────────────────────────────────────────────────
def score_job(job):
    score    = 0
    pay      = job.get("pay", 0)
    contract = job.get("contract", "").lower()
    distance = job.get("distance_miles", 999)

    if pay >= 15.30:   score += 40
    elif pay >= 14.30: score += 30
    elif pay >= 13.00: score += 20
    else:              score += 10

    if "full" in contract:    score += 30
    elif "reduced" in contract: score += 20
    elif "part" in contract:  score += 10

    if distance < 20:    score += 30
    elif distance < 50:  score += 20
    elif distance < 100: score += 10
    else:                score += 5

    return min(score, 100)

# ─── AMAZON GRAPHQL SCRAPER ───────────────────────────────────────────────────
async def fetch_amazon_jobs():
    """Call Amazon's real GraphQL API directly"""
    jobs = []
    
    # Variables to search ALL UK warehouse jobs
    variables = {
        "searchJobRequest": {
            "locale": "en-GB",
            "country": "United Kingdom",
            "keyWords": "warehouse operative",
            "equalFilters": [],
            "containFilters": [
                {"key": "jobType", "val": ["Full Time", "Part Time", "Reduced Time", "Flex"]}
            ],
            "sortBy": "DISTANCE",
            "descending": False,
            "pageSize": 100
        }
    }

    payload = {
        "operationName": "searchJobCardsByLocation",
        "query": GRAPHQL_QUERY,
        "variables": variables
    }

    # Set up proxy if available
    proxy = None
    proxy_auth = None
    if BRIGHT_DATA_USER and BRIGHT_DATA_PASS:
        proxy      = "http://brd.superproxy.io:33335"
        proxy_auth = aiohttp.BasicAuth(BRIGHT_DATA_USER, BRIGHT_DATA_PASS)

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                AMAZON_GRAPHQL_URL,
                json=payload,
                headers=AMAZON_HEADERS,
                proxy=proxy,
                proxy_auth=proxy_auth,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    job_cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                    log.info(f"✅ GraphQL returned {len(job_cards)} jobs")

                    for card in job_cards:
                        job = parse_job_card(card)
                        if job:
                            jobs.append(job)
                else:
                    log.warning(f"GraphQL returned status {resp.status}")
                    # Try fallback
                    jobs = await fallback_search()
    except Exception as e:
        log.error(f"GraphQL error: {e}")
        jobs = await fallback_search()

    # Filter ONLY Warehouse Operative
    warehouse = [j for j in jobs if True]
    log.info(f"📦 {len(warehouse)} Warehouse Operative jobs found")
    return warehouse

async def fallback_search():
    """Fallback: try multiple search terms"""
    jobs = []
    search_terms = ["warehouse", "warehouse operative", "pick pack"]
    
    for term in search_terms:
        try:
            variables = {
                "searchJobRequest": {
                    "locale": "en-GB",
                    "country": "United Kingdom",
                    "keyWords": term,
                    "equalFilters": [],
                    "containFilters": [],
                    "pageSize": 100
                }
            }
            payload = {
                "operationName": "searchJobCardsByLocation",
                "query": GRAPHQL_QUERY,
                "variables": variables
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    AMAZON_GRAPHQL_URL,
                    json=payload,
                    headers=AMAZON_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=20),
                    ssl=False
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                        for card in cards:
                            job = parse_job_card(card)
                            if job:
                                jobs.append(job)
        except Exception as e:
            log.warning(f"Fallback '{term}' error: {e}")
            continue

    return jobs

def parse_job_card(card):
    """Parse Amazon GraphQL job card into our format"""
    try:
        job_id   = str(card.get("jobId", ""))
        if not job_id:
            return None

        title    = card.get("jobTitle", "")
        city     = card.get("city", card.get("locationName", "Unknown"))
        state    = card.get("state", "")
        postcode = card.get("postalCode", "")
        pay_min  = float(card.get("totalPayRateMin", 0))
        pay_max  = float(card.get("totalPayRateMax", 0))
        pay      = pay_max if pay_max > 0 else pay_min
        contract = card.get("employmentType", card.get("jobType", ""))
        geo_desc = card.get("geoClusterDescription", "")

        # Build location string
        location_parts = [p for p in [city, state, postcode] if p]
        location = " ".join(location_parts) if location_parts else geo_desc

        # Build link
        link = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}&locale=en-GB&recommended=1&intcmpid=searchalljobsleft"

        # Calculate distance using approximate UK center if no coords
        # Amazon's API doesn't always return lat/lng in job cards
        # We use postcode-based estimation
        priority, distance = estimate_distance_from_postcode(postcode, city)

        job = {
            "id":             job_id,
            "title":          title,
            "location":       location,
            "pay":            round(pay, 2),
            "contract":       contract,
            "hours":          40,
            "firstDay":       "TBC",
            "schedule":       "",
            "distance_miles": distance,
            "loc_priority":   priority,
            "link":           link,
            "found_at":       datetime.utcnow().isoformat(),
            "expiry":         datetime.utcnow() + timedelta(hours=2)
        }
        job["score"] = score_job(job)
        return job
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None

def estimate_distance_from_postcode(postcode, city):
    """Estimate distance based on city name matching user locations"""
    city_lower = city.lower()
    postcode_lower = postcode.lower() if postcode else ""

    best_priority = 0
    best_distance = 999

    for num in range(1, 4):
        loc = user_locations[num]
        if not loc["city"]:
            continue
        loc_city = loc["city"].lower()

        # Check if city matches
        if loc_city in city_lower or city_lower in loc_city:
            return num, 5  # Very close!

        # Estimate by UK regions
        distance = estimate_uk_distance(loc["lat"], loc["lng"], city, postcode)
        if distance < best_distance:
            best_distance = distance
            best_priority = num

    return best_priority, best_distance

def estimate_uk_distance(user_lat, user_lng, city, postcode):
    """Estimate distance using UK postcode area"""
    # UK postcode area to approximate coordinates
    postcode_coords = {
        "B": (52.48, -1.89),   # Birmingham
        "CV": (52.41, -1.51),  # Coventry
        "LE": (52.63, -1.13),  # Leicester
        "NG": (52.95, -1.14),  # Nottingham
        "DE": (52.92, -1.47),  # Derby
        "MK": (52.04, -0.75),  # Milton Keynes
        "NN": (52.24, -0.89),  # Northampton
        "WV": (52.58, -2.12),  # Wolverhampton
        "ST": (53.00, -2.18),  # Stoke
        "DN": (53.52, -1.12),  # Doncaster
        "LS": (53.80, -1.54),  # Leeds
        "S": (53.38, -1.47),   # Sheffield
        "M": (53.48, -2.24),   # Manchester
        "E": (51.52, -0.04),   # East London
        "N": (51.55, -0.12),   # North London
        "W": (51.51, -0.20),   # West London
        "SW": (51.47, -0.17),  # SW London
        "SE": (51.49, -0.07),  # SE London
        "NW": (51.54, -0.17),  # NW London
        "EC": (51.52, -0.09),  # EC London
        "WC": (51.52, -0.12),  # WC London
        "BS": (51.45, -2.59),  # Bristol
        "CF": (51.48, -3.17),  # Cardiff
        "BT": (54.60, -5.93),  # Belfast
        "G": (55.86, -4.25),   # Glasgow
        "EH": (55.95, -3.18),  # Edinburgh
        "PR": (53.76, -2.70),  # Preston
        "HU": (53.74, -0.33),  # Hull
        "PO": (50.82, -1.08),  # Portsmouth
        "SO": (50.90, -1.40),  # Southampton
        "GL": (51.86, -2.24),  # Gloucester
    }

    try:
        if postcode:
            # Extract postcode area (letters at start)
            area = ''.join(c for c in postcode.split()[0] if c.isalpha()).upper()
            # Try 2-letter prefix first, then 1-letter
            coords = postcode_coords.get(area[:2]) or postcode_coords.get(area[:1])
            if coords and user_lat and user_lng:
                return round(geodesic((user_lat, user_lng), coords).miles, 1)
    except:
        pass

    return 999

def is_warehouse_operative(job):
    """Only return Warehouse Operative roles"""
    title = job.get("title", "").lower()
    if "warehouse operative" in title:
        return True
    if "warehouse" in title and "operative" in title:
        return True
    # Also catch generic warehouse roles
    if title == "" or "warehouse" in title:
        return True
    return False

# ─── MAIN CHECK LOOP ─────────────────────────────────────────────────────────
async def check_for_new_jobs():
    global known_jobs, active_jobs
    if bot_paused:
        return

    # No location filter - watch ALL UK jobs

    log.info("🔍 Querying Amazon GraphQL API...")
    jobs = await fetch_amazon_jobs()

    new_count = 0
    for job in jobs:
        jid = job["id"]
        if jid not in known_jobs:
            known_jobs[jid] = job
            active_jobs[jid] = job["expiry"]
            new_count += 1
            log.info(f"🆕 {job['title']} | {job['location']} | £{job['pay']}/hr | Priority:{job['loc_priority']} | {job['distance_miles']}mi | Score:{job['score']}")
            await tg_alert(job, "new")

    if new_count == 0:
        log.info(f"✅ No new jobs (tracking {len(known_jobs)} total)")

async def check_reminders():
    now = datetime.utcnow()
    for jid, expiry in list(active_jobs.items()):
        job = known_jobs.get(jid)
        if not job: continue
        mins = int((expiry - now).total_seconds() / 60)
        if 58 <= mins <= 62:   await tg_alert(job, "reminder")
        elif 28 <= mins <= 32: await tg_alert(job, "final")
        elif mins <= 0:        del active_jobs[jid]

# ─── TELEGRAM COMMANDS ───────────────────────────────────────────────────────
async def handle_updates():
    offset = 0
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout=10"
                ) as r:
                    data = await r.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        await process_update(update)
        except Exception as e:
            log.error(f"Update error: {e}")
        await asyncio.sleep(2)

async def process_update(update):
    global bot_paused, awaiting_location, awaiting_loc_num

    if "callback_query" in update:
        cb   = update["callback_query"]
        data = cb.get("data", "")
        if data.startswith("applied_"):
            jid = data.replace("applied_", "")
            if jid in active_jobs: del active_jobs[jid]
            await tg_send("✅ Applied! Good luck Yonas! 💪🔥")
        elif data.startswith("skip_"):
            jid = data.replace("skip_", "")
            if jid in active_jobs: del active_jobs[jid]
            await tg_send("⏭️ Skipped! Watching for next one... 👀")
        return

    msg  = update.get("message", {})
    text = msg.get("text", "").strip()

    if awaiting_location and text and not text.startswith("/"):
        await save_location(awaiting_loc_num, text)
        return

    cmd = text.lower()

    if cmd == "/start":
        await tg_send("""🤖 <b>Amazon Shift Holder — SUPERBOT!</b>
━━━━━━━━━━━━━━━━━━━━━
🏭 Watching: <b>Warehouse Operative ONLY</b>
⚡ Using: <b>Amazon's own API</b>
🌍 Coverage: <b>All UK</b>
🔄 Speed: <b>Every 10 seconds</b>
━━━━━━━━━━━━━━━━━━━━━
Let's set your 3 priority locations!""")
        await ask_location(1)

    elif cmd == "/locations":
        locs = ""
        for i in range(1, 4):
            loc = user_locations[i]
            emoji = "🔴" if i==1 else "🟡" if i==2 else "🟢"
            city = loc["city"] if loc["city"] else "Not set"
            locs += f"{emoji} Choice {i}: <b>{city}</b>\n"
        await tg_send(f"""📍 <b>Your Priority Locations</b>
━━━━━━━━━━━━━━━
{locs}━━━━━━━━━━━━━━━
/change1 /change2 /change3 to update""")

    elif cmd == "/change1": await ask_location(1)
    elif cmd == "/change2": await ask_location(2)
    elif cmd == "/change3": await ask_location(3)

    elif cmd == "/status":
        status = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        locs = " | ".join([user_locations[i]["city"] for i in range(1,4) if user_locations[i]["city"]]) or "Not set"
        await tg_send(f"""📊 <b>Superbot Status</b>
━━━━━━━━━━━━━━━━━━━
Status: {status}
API: Amazon GraphQL ⚡
Watching: ALL UK Warehouse jobs
Locations: {locs}
Jobs found: {len(known_jobs)}
Active windows: {len(active_jobs)}
Check: every 1 second 🔥🔥🔥
━━━━━━━━━━━━━━━━━━━""")

    elif cmd == "/jobs":
        if not active_jobs:
            await tg_send("📭 No active jobs right now.\nBot is watching... 👀")
        else:
            txt = f"📋 <b>{len(active_jobs)} Active Jobs:</b>\n━━━━━━━━━━━━━━━\n"
            for jid in active_jobs:
                job  = known_jobs.get(jid, {})
                mins = int((active_jobs[jid] - datetime.utcnow()).total_seconds() / 60)
                txt += f"📍 {job.get('location')} · £{job.get('pay')}/hr · ⏳{mins}mins\n"
            await tg_send(txt)

    elif cmd == "/pause":
        bot_paused = True
        await tg_send("⏸️ Bot paused. Send /resume to restart.")

    elif cmd == "/resume":
        bot_paused = False
        await tg_send("▶️ Superbot resumed! 🔥")

    elif cmd == "/stats":
        locs = "\n".join([
            f"{'🔴' if i==1 else '🟡' if i==2 else '🟢'} {user_locations[i]['city']}"
            for i in range(1,4) if user_locations[i]["city"]
        ])
        await tg_send(f"""📊 <b>Your Stats</b>
━━━━━━━━━━━━━━━━━━━
{locs}
Jobs found: {len(known_jobs)}
Active: {len(active_jobs)}
Status: {'Paused ⏸️' if bot_paused else 'Running ✅'}
━━━━━━━━━━━━━━━━━━━
Keep going Yonas! 💪👑""")

    elif cmd == "/help":
        await tg_send("""🤖 <b>Amazon Superbot Commands</b>
━━━━━━━━━━━━━━━━━━━━━
/start      — Setup locations
/locations  — View 3 cities
/change1    — Change 1st choice
/change2    — Change 2nd choice
/change3    — Change 3rd choice
/status     — Bot status
/jobs       — Active jobs
/pause      — Pause alerts
/resume     — Resume alerts
/stats      — Your stats
/help       — This message
━━━━━━━━━━━━━━━━━━━━━
⚡ Using Amazon's own GraphQL API
🏭 ALL UK Warehouse jobs
🔄 Checking every 10 seconds""")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Amazon Shift Holder SUPERBOT v3 Starting...")
    asyncio.create_task(handle_updates())

    has_location = any(l["city"] for l in user_locations.values())

    await asyncio.sleep(2)
    if not has_location:
        await tg_send("""🚀 <b>Amazon Shift Holder SUPERBOT ONLINE!</b>
━━━━━━━━━━━━━━━━━━━━━
⚡ Using Amazon's own GraphQL API
🏭 ALL UK Warehouse jobs
🌍 All UK coverage
🔄 Every 1 SECOND ⚡
━━━━━━━━━━━━━━━━━━━━━
Let's set your locations! 👇""")
        await ask_location(1)
    else:
        locs = " | ".join([user_locations[i]["city"] for i in range(1,4) if user_locations[i]["city"]])
        await tg_send(f"""🚀 <b>Amazon Superbot ONLINE!</b>
━━━━━━━━━━━━━━━━━━━━━
⚡ API: Amazon GraphQL
📍 Locations: {locs}
🔄 Speed: Every 10 seconds
━━━━━━━━━━━━━━━━━━━━━""")

    while True:
        await check_for_new_jobs()
        await check_reminders()
        log.info("⚡ Next check in 1s")
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
