import asyncio
import os
import json
import logging
import aiohttp
import re
import math
from datetime import datetime
from playwright.async_api import async_playwright
from collections import defaultdict

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
CHAT_ID      = os.environ.get("CHAT_ID", "1027065157")
DECODO_USER  = os.environ.get("DECODO_USER", "")
DECODO_PASS  = os.environ.get("DECODO_PASS", "")
DECODO_HOST  = os.environ.get("DECODO_HOST", "gate.decodo.com")
DECODO_PORT  = os.environ.get("DECODO_PORT", "7777")

# ─── MULTI-ACCOUNT CONFIG ────────────────────────────────────────────────────
ACCOUNTS = []
for i in range(1, 6):
    email   = os.environ.get(f"AMAZON_EMAIL_{i}", "")
    pin     = os.environ.get(f"AMAZON_PIN_{i}", "")
    cookies = os.environ.get(f"AMAZON_COOKIES_{i}", "")
    if i == 1:
        email   = email   or os.environ.get("AMAZON_EMAIL", "")
        pin     = pin     or os.environ.get("AMAZON_PIN", "")
        cookies = cookies or os.environ.get("AMAZON_COOKIES", "")
    if email or cookies:
        ACCOUNTS.append({
            "id": i, "email": email, "pin": pin,
            "cookies": cookies, "session": [], "logged_in": False,
        })

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs    = {}
bot_paused    = False
job_history   = []
posting_times = defaultdict(list)

TELEGRAM_API     = f"https://api.telegram.org/bot{BOT_TOKEN}"
SUBSCRIBERS_FILE = "/tmp/subscribers.json"
COOKIES_FILE     = "/tmp/amazon_session.json"

# ─── JOB FILTERING (from feedback) ───────────────────────────────────────────
WAREHOUSE_KEYWORDS = [
    "warehouse", "fulfillment", "fulfilment", "sortation",
    "sort centre", "sort center", "delivery station",
    "fc associate", "warehouse operative", "warehouse associate",
    "sortation operative", "fulfillment associate", "fulfilment associate",
    "seasonal associate", "process assistant", "picker", "packer",
    "stower", "problem solver", "production operator", "site assistant",
    "amazon associate", "operations associate",
]

BLOCKED_KEYWORDS = [
    "customer service", "software", "engineer", "manager",
    "corporate", "marketing", "hr ", "finance", "recruiter",
    "sales", "vcc", "loss prevention", "learning ambassador",
    "data entry", "legal", "it support", "business analyst",
]

FRESH_KEYWORDS = ["amazon fresh", "whole foods", "fresh grocery"]

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

# ─── UK CITY → POSTCODE MAP ──────────────────────────────────────────────────
CITY_POSTCODES = {
    "birmingham": "B1 1BB", "london": "EC1A 1BB", "manchester": "M1 1AE",
    "leeds": "LS1 1BA", "glasgow": "G1 1AA", "liverpool": "L1 1JF",
    "sheffield": "S1 1AA", "bristol": "BS1 1AA", "newcastle": "NE1 1AA",
    "nottingham": "NG1 1AA", "leicester": "LE1 1AA", "coventry": "CV1 1AA",
    "wolverhampton": "WV1 1AA", "derby": "DE1 1AA", "cardiff": "CF10 1AA",
    "edinburgh": "EH1 1AA", "belfast": "BT1 1AA", "southampton": "SO14 1AA",
    "portsmouth": "PO1 1AA", "oxford": "OX1 1AA", "cambridge": "CB1 1AA",
    "reading": "RG1 1AA", "luton": "LU1 1AA", "northampton": "NN1 1AA",
    "milton keynes": "MK9 1AA", "warrington": "WA1 1AA", "hull": "HU1 1AA",
    "doncaster": "DN1 1AA", "chesterfield": "S40 1AA", "wakefield": "WF1 1AA",
    "durham": "DH1 1AA", "sunderland": "SR1 1AA", "middlesbrough": "TS1 1AA",
    "bolton": "BL1 1AA", "wigan": "WN1 1AA", "stockport": "SK1 1AA",
    "stoke": "ST1 1AA", "swansea": "SA1 1AA", "exeter": "EX1 1AA",
    "enfield": "EN1 1AA", "slough": "SL1 1AA", "watford": "WD17 1AA",
    "rugby": "CV21 1AA", "dunstable": "LU5 4AA", "coalville": "LE67 1AA",
    "knowsley": "L33 1AA", "west thurrock": "RM20 1AA", "motherwell": "ML1 1AA",
    "barking": "IG11 1AA", "tilbury": "RM18 1AA", "bathgate": "EH48 2FB",
    "gloucester": "GL4 3HR", "poole": "BH15 2AA", "bournemouth": "BH1 1AA",
    "swindon": "SN1 1AA", "peterborough": "PE1 1AA", "ipswich": "IP1 1AA",
    "norwich": "NR1 1AA", "basildon": "SS14 1AA", "chelmsford": "CM1 1AA",
    "colchester": "CO1 1AA", "stevenage": "SG1 1AA",
}

CITY_COORDS = {
    "B1 1BB": (52.4862, -1.8904), "EC1A 1BB": (51.5200, -0.0990),
    "M1 1AE": (53.4808, -2.2426), "LS1 1BA": (53.7997, -1.5492),
    "G1 1AA": (55.8642, -4.2518), "L1 1JF": (53.4084, -2.9916),
    "S1 1AA": (53.3811, -1.4701), "BS1 1AA": (51.4545, -2.5879),
    "NE1 1AA": (54.9783, -1.6178), "NG1 1AA": (52.9540, -1.1549),
    "LE1 1AA": (52.6369, -1.1398), "CV1 1AA": (52.4068, -1.5197),
    "WV1 1AA": (52.5852, -2.1297), "DE1 1AA": (52.9225, -1.4746),
    "CF10 1AA": (51.4816, -3.1791), "EH1 1AA": (55.9533, -3.1883),
    "BT1 1AA": (54.5973, -5.9301), "SO14 1AA": (50.9097, -1.4044),
    "PO1 1AA": (50.7989, -1.0919), "OX1 1AA": (51.7520, -1.2577),
    "CB1 1AA": (52.2053, 0.1218),  "RG1 1AA": (51.4543, -0.9781),
    "LU1 1AA": (51.8787, -0.4200), "NN1 1AA": (52.2405, -0.9027),
    "MK9 1AA": (52.0406, -0.7594), "WA1 1AA": (53.3900, -2.5970),
    "HU1 1AA": (53.7457, -0.3367), "DN1 1AA": (53.5228, -1.1286),
    "WF1 1AA": (53.6830, -1.4977), "DH1 1AA": (54.7761, -1.5733),
    "SR1 1AA": (54.9069, -1.3838), "TS1 1AA": (54.5740, -1.2343),
    "BL1 1AA": (53.5780, -2.4286), "WN1 1AA": (53.5450, -2.6333),
    "SK1 1AA": (53.4083, -2.1578), "ST1 1AA": (53.0271, -2.1772),
    "SA1 1AA": (51.6214, -3.9436), "EX1 1AA": (50.7236, -3.5275),
    "EN1 1AA": (51.6522, -0.0808), "SL1 1AA": (51.5105, -0.5950),
    "WD17 1AA": (51.6565, -0.3903), "CV21 1AA": (52.3711, -1.2660),
    "LU5 4AA": (51.8868, -0.5216), "LE67 1AA": (52.7236, -1.3698),
    "L33 1AA": (53.4597, -2.8480), "RM20 1AA": (51.4833, 0.2667),
    "ML1 1AA": (55.7900, -3.9833), "IG11 1AA": (51.5390, 0.0799),
    "RM18 1AA": (51.4617, 0.3590), "EH48 2FB": (55.9069, -3.6427),
    "GL4 3HR": (51.8585, -2.2180), "BH15 2AA": (50.7192, -1.9874),
    "BH1 1AA": (50.7209, -1.8795), "SN1 1AA": (51.5558, -1.7797),
    "PE1 1AA": (52.5695, -0.2405), "IP1 1AA": (52.0567, 1.1482),
    "NR1 1AA": (52.6309, 1.2974),  "SS14 1AA": (51.5790, 0.4553),
    "CM1 1AA": (51.7356, 0.4685),  "CO1 1AA": (51.8960, 0.8919),
    "SG1 1AA": (51.9024, -0.2082), "S40 1AA": (53.2354, -1.4210),
}

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def resolve_location(location):
    loc = location.lower().strip()
    if loc in CITY_POSTCODES:
        return CITY_POSTCODES[loc]
    for city, postcode in CITY_POSTCODES.items():
        if loc in city or city in loc:
            return postcode
    return location.upper()

def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return R * 2 * math.asin(math.sqrt(a))

async def get_postcode_coords_api(postcode):
    try:
        clean = postcode.replace(" ", "").upper()
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.postcodes.io/postcodes/{clean}",
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
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
        postcode = resolve_location(location)
        lat1, lon1 = await get_coords(postcode)
        lat2, lon2 = await get_coords(job_postcode)
        if all(x is not None for x in [lat1, lon1, lat2, lon2]):
            return round(haversine_miles(lat1, lon1, lat2, lon2), 1)
    except:
        pass
    return None

def job_matches_type(job, job_type):
    if job_type == "both":
        return True
    contract = job.get("contract", "").lower()
    if job_type == "fulltime":
        return "full" in contract
    if job_type == "parttime":
        return "part" in contract or "reduced" in contract
    return True

def is_night_shift(schedule):
    if not schedule or schedule == "TBC":
        return False
    return any(t in str(schedule) for t in [
        "18:30","19:00","20:00","21:00","22:00","23:00","23:45","0:00","1:00","2:00","3:00"
    ])

def shift_priority(text):
    text = str(text)
    if any(t in text for t in ["18:30","19:00","20:00","21:00","22:00","23:00","23:45"]):
        return 1
    if any(t in text for t in ["14:00","15:00","16:00"]):
        return 2
    return 3

def is_peak_time():
    now  = datetime.utcnow()
    h, m = now.hour, now.minute
    am   = (h == 10 and m >= 55) or (h == 11 and m <= 25)
    pm   = (h == 22 and m >= 55) or (h == 23 and m <= 25)
    return am or pm

# ─── DECODO PROXY URL ─────────────────────────────────────────────────────────
def get_proxy_url():
    if DECODO_USER and DECODO_PASS:
        return f"http://{DECODO_USER}:{DECODO_PASS}@{DECODO_HOST}:{DECODO_PORT}"
    return None

# ─── SUBSCRIBER MANAGEMENT ───────────────────────────────────────────────────
def load_subscribers():
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {}

def save_subscribers(subs):
    try:
        with open(SUBSCRIBERS_FILE, "w") as f:
            json.dump(subs, f, indent=2)
    except Exception as e:
        log.error(f"Save subscribers error: {e}")

subscribers = load_subscribers()

if CHAT_ID not in subscribers:
    subscribers[CHAT_ID] = {
        "name": "Yonas", "locations": ["Birmingham"],
        "radius": 50, "job_type": "both",
        "setup_complete": True, "auto_apply": True,
        "joined": datetime.utcnow().isoformat(),
    }
    save_subscribers(subscribers)
else:
    subscribers[CHAT_ID]["auto_apply"] = True
    save_subscribers(subscribers)

onboarding = {}

# ─── COOKIE PERSISTENCE ──────────────────────────────────────────────────────
def load_cookies():
    try:
        if os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return []

def save_cookies(cookies):
    try:
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies, f)
        log.info(f"✅ Saved {len(cookies)} cookies")
    except Exception as e:
        log.error(f"Cookie save error: {e}")

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
async def tg_send(text, reply_markup=None, chat_id=None):
    cid = chat_id or CHAT_ID
    payload = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ─── ONBOARDING ──────────────────────────────────────────────────────────────
async def start_onboarding(cid, name="there"):
    onboarding[cid] = {"step": "job_type", "locations": [], "name": name}
    await tg_send(f"""👑 <b>Welcome {name}!</b>

I'm the Amazon Warehouse Job Alert Bot.
I find jobs and alert you instantly!

<b>Step 1 of 4 — Job Type</b>
1️⃣ Full-time only
2️⃣ Part-time only
3️⃣ Both""", chat_id=cid)

async def handle_onboarding(cid, text):
    global subscribers
    state = onboarding.get(cid, {})
    step  = state.get("step", "")

    if step == "job_type":
        mapping = {"1": "fulltime", "2": "parttime", "3": "both",
                   "1️⃣": "fulltime", "2️⃣": "parttime", "3️⃣": "both"}
        jt = mapping.get(text)
        if not jt:
            await tg_send("Please reply 1, 2 or 3 ☝️", chat_id=cid)
            return
        labels = {"fulltime": "Full-time only", "parttime": "Part-time only", "both": "Both"}
        state["job_type"] = jt
        state["step"]     = "location_1"
        onboarding[cid]   = state
        await tg_send(f"""✅ Job type: <b>{labels[jt]}</b>

<b>Step 2 of 4 — Primary Location</b>
Enter your preferred city or postcode:
Examples: <b>Birmingham</b>, <b>Leeds</b>, <b>B1 1BB</b>""", chat_id=cid)

    elif step == "location_1":
        state["locations"] = [text.strip()]
        state["step"]      = "location_2"
        onboarding[cid]    = state
        await tg_send(f"""✅ Location: <b>{text.strip()}</b>

<b>Step 3 of 4 — Second Location (Optional)</b>
Add a 2nd location or type <b>DONE</b> to skip.""", chat_id=cid)

    elif step == "location_2":
        if text.upper() != "DONE":
            state["locations"].append(text.strip())
        state["step"]   = "radius"
        onboarding[cid] = state
        await tg_send("""<b>Step 4 of 4 — Travel Radius</b>
How far can you travel? (miles)
🚗 <b>10</b> — Very local
🚗 <b>25</b> — Nearby
🚗 <b>50</b> — Wide search""", chat_id=cid)

    elif step == "radius":
        try:
            radius = int(re.search(r'\d+', text).group())
        except:
            await tg_send("Please enter a number e.g. <b>25</b>", chat_id=cid)
            return
        state["radius"] = radius
        state["step"]   = "confirm"
        onboarding[cid] = state
        locs   = state.get("locations", [])
        jt     = state.get("job_type", "both")
        jlabel = {"fulltime": "Full-time only", "parttime": "Part-time only", "both": "Full-time & Part-time"}.get(jt)
        ltext  = "\n".join([f"📍 {'⭐ ' if i==0 else ''}{l}" for i, l in enumerate(locs)])
        await tg_send(f"""<b>Confirm Your Preferences</b>
━━━━━━━━━━━━━━━━━
{ltext}
🚗 Radius: <b>{radius} miles</b>
📋 Type: <b>{jlabel}</b>
━━━━━━━━━━━━━━━━━
Reply <b>CONFIRM</b> or <b>RESTART</b>""", chat_id=cid)

    elif step == "confirm":
        if text.upper() == "CONFIRM":
            subscribers[cid] = {
                "name": state.get("name", "Friend"),
                "locations": state.get("locations", []),
                "radius": state.get("radius", 30),
                "job_type": state.get("job_type", "both"),
                "setup_complete": True, "auto_apply": False,
                "joined": datetime.utcnow().isoformat(),
            }
            save_subscribers(subscribers)
            onboarding.pop(cid, None)
            await tg_send("🎉 <b>You're all set!</b> You'll get instant alerts when jobs drop!\n\nUse /help for all commands.", chat_id=cid)
        elif text.upper() == "RESTART":
            await start_onboarding(cid, state.get("name", "there"))
        else:
            await tg_send("Reply <b>CONFIRM</b> or <b>RESTART</b>", chat_id=cid)

# ─── GRAPHQL RESPONSE HANDLER (from feedback) ─────────────────────────────────
async def handle_graphql_response(response, payload):
    status = response.status
    text   = await response.text()

    if status != 200:
        log.warning(f"⚠️ GraphQL status {status} — falling back to browser")
        log.warning(f"GraphQL error response: {text[:800]}")
        log.warning(f"GraphQL payload sent: {json.dumps(payload)[:1000]}")
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("⚠️ GraphQL returned invalid JSON — falling back to browser")
        log.warning(f"Bad response: {text[:800]}")
        return None

    if "errors" in data:
        log.warning("⚠️ GraphQL returned errors:")
        log.warning(json.dumps(data["errors"], indent=2)[:1000])
        return None

    return data

# ─── PARSE CARD ───────────────────────────────────────────────────────────────
def parse_card(card):
    try:
        job_id = str(card.get("jobId", ""))
        if not job_id:
            return None

        title = card.get("jobTitle", "") or ""
        log.info(f"🔍 [{job_id}] {title}")

        if not is_warehouse_job(title):
            log.info(f"⏭️ Skipped: {title}")
            return None

        log.info(f"✅ Accepted: {title}")

        city        = card.get("city") or card.get("locationName") or ""
        state       = card.get("state") or "England"
        postcode    = card.get("postalCode") or ""
        geo         = card.get("geoClusterDescription") or ""
        pay         = float(card.get("totalPayRateMax") or card.get("totalPayRateMin") or 0)
        employment  = card.get("employmentType") or ""
        job_type    = card.get("jobType") or ""
        contract    = employment or job_type or "Seasonal"
        hours       = str(int(card.get("hoursPerWeek"))) if card.get("hoursPerWeek") else "TBC"
        first_day   = card.get("firstDayOnSite") or "TBC"
        sched_count = card.get("scheduleCount", 0)
        shift_code  = card.get("shiftCode") or ""
        schedule    = shift_code if shift_code else (f"{sched_count} schedule(s)" if sched_count else "TBC")

        parts = []
        if city: parts.append(city)
        if state and state != city: parts.append(state)
        if geo and postcode:   location = f"{', '.join(parts)} ({geo}) {postcode}".strip()
        elif geo:              location = f"{', '.join(parts)} ({geo})".strip()
        elif postcode:         location = f"{', '.join(parts)} {postcode}".strip()
        else:                  location = ", ".join(parts) or "Unknown UK Location"

        return {
            "id": job_id, "title": title, "location": location,
            "postcode": postcode, "pay": round(pay, 2), "pay_display": f"{pay:.2f}",
            "contract": contract, "firstDay": first_day, "schedule": schedule,
            "hours": hours,
            "link": f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}",
            "found_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None

# ─── PRIMARY SCRAPER — DIRECT GRAPHQL VIA DECODO UK PROXY ────────────────────
async def fetch_jobs():
    all_jobs = {}
    proxy    = get_proxy_url()

    if not proxy:
        log.warning("⚠️ No Decodo proxy configured — falling back to browser")
        return await fetch_jobs_browser()

    graphql_url = "https://www.jobsatamazon.co.uk/api/graphql"

    # GraphQL query
    query = """
    query searchJobCardsByLocation($input: SearchJobCardsByLocationInput!) {
        searchJobCardsByLocation(input: $input) {
            jobCards {
                jobId jobTitle city state postalCode geoClusterDescription
                totalPayRateMin totalPayRateMax employmentType jobType
                hoursPerWeek firstDayOnSite scheduleCount shiftCode distance
            }
        }
    }
    """

    # Build payload
    payload = {
        "query": query,
        "variables": {
            "input": {
                "locale":     "en-GB",
                "country":    "GBR",
                "cityName":   "London",
                "postalCode": "EC1A 1BB",
                "radius":     500,
                "jobType":    ["Full-Time", "Part-Time", "Reduced-Time", "Seasonal", "Temporary"],
                "sortBy":     "DISTANCE",
                "pageNumber": 0,
                "pageSize":   100,
            }
        }
    }

    headers = {
        "Content-Type":   "application/json",
        "Accept":         "application/json",
        "Accept-Language":"en-GB,en;q=0.9",
        "country":        "United Kingdom",
        "locale":         "en-GB",
        "Origin":         "https://www.jobsatamazon.co.uk",
        "Referer":        "https://www.jobsatamazon.co.uk/app",
        "User-Agent":     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    # Add saved cookies if available
    saved = load_cookies()
    if saved:
        headers["Cookie"] = "; ".join([f"{c['name']}={c['value']}" for c in saved])

    log.info(f"🌐 Fetching via Decodo UK proxy...")

    try:
        connector = aiohttp.TCPConnector()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                graphql_url,
                json=payload,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                data = await handle_graphql_response(response, payload)

                if data is None:
                    log.info("↩️ GraphQL failed — trying browser fallback")
                    return await fetch_jobs_browser()

                cards = (data.get("data", {})
                             .get("searchJobCardsByLocation", {})
                             .get("jobCards", []))

                log.info(f"🎯 GraphQL via Decodo: {len(cards)} jobs returned")

                for card in cards:
                    job = parse_card(card)
                    if job and job["id"] not in all_jobs:
                        all_jobs[job["id"]] = job

    except Exception as e:
        log.error(f"Decodo proxy error: {e}")
        log.info("↩️ Falling back to browser")
        return await fetch_jobs_browser()

    log.info(f"👑 Total valid warehouse jobs: {len(all_jobs)}")
    return list(all_jobs.values())

# ─── FALLBACK SCRAPER — BROWSER (NO PROXY, saves bandwidth) ──────────────────
async def fetch_jobs_browser():
    all_jobs = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled","--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}", lambda route: route.abort())

            saved = load_cookies()
            if saved:
                await context.add_cookies(saved)

            page     = await context.new_page()
            captured = []

            async def handle_response(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        body = response.request.post_data
                        if body:
                            log.info(f"📡 Captured GraphQL request body: {body[:300]}")
                        data  = await response.json()
                        cards = (data.get("data", {})
                                     .get("searchJobCardsByLocation", {})
                                     .get("jobCards", []))
                        if cards:
                            log.info(f"🎯 Browser intercepted {len(cards)} jobs")
                            captured.extend(cards)
                except:
                    pass

            page.on("response", handle_response)
            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle", timeout=45000
            )
            await page.wait_for_timeout(4000)

            if not captured:
                log.info("⚡ Scrolling to trigger GraphQL...")
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(3000)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(2000)

            await browser.close()

            log.info(f"🎯 Browser intercepted {len(captured)} total cards")
            for card in captured:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job

    except Exception as e:
        log.error(f"Browser error: {e}")

    log.info(f"👑 Browser total: {len(all_jobs)} jobs")
    return list(all_jobs.values())

# ─── FETCH FULL JOB DETAILS ──────────────────────────────────────────────────
async def fetch_job_details(job):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox","--disable-gpu"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}", lambda route: route.abort())
            saved = load_cookies()
            if saved:
                await context.add_cookies(saved)
            page = await context.new_page()
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
            content = await page.inner_text("body")

            m = re.search(r'(?:Start [Dd]ate|Tentative start date)[:\s]+([A-Za-z]+,?\s+\d+\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+ \d+, \d{4})', content)
            if m: job["firstDay"] = m.group(1).strip()

            m = re.search(r'Shift timing[:\s]+([A-Za-z,\s]+\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})', content)
            if m: job["schedule"] = m.group(1).strip()
            else:
                m = re.search(r'Shift[:\s]+([A-Za-z,\s]+\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})', content)
                if m: job["schedule"] = m.group(1).strip()

            m = re.search(r'(\d+)\s*hrs?\s*per\s*week', content, re.IGNORECASE)
            if m: job["hours"] = m.group(1)

            for ct in ["Full-time","Part-time","Reduced","Fixed-term","Seasonal","Permanent","Temporary"]:
                if ct.lower() in content.lower():
                    job["contract"] = ct
                    break

            await browser.close()
            log.info(f"✅ Details: {job.get('firstDay','?')} | {str(job.get('schedule','?'))[:40]}")
    except Exception as e:
        log.warning(f"Detail fetch error: {e}")
    return job

# ─── ALERT ───────────────────────────────────────────────────────────────────
async def tg_alert(job, status="new", chat_id=None, distance=None, account_id=None):
    cid = chat_id or CHAT_ID

    headers_map = {
        "new":         "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>",
        "applying":    f"🤖 <b>AUTO-SUBMITTING{' (Acc '+str(account_id)+')' if account_id else ''}...</b>",
        "applied":     f"✅ <b>APPLIED FOR YOU{' (Acc '+str(account_id)+')' if account_id else ''}!</b>",
        "navigating":  "⚡ <b>BOT OPENING APPLICATION...</b>",
        "ready":       "✅ <b>APPLICATION READY — LOG IN & SUBMIT!</b>",
        "fresh_alert": "🌿 <b>AMAZON FRESH JOB — MANUAL APPLY ONLY</b>",
    }
    header   = headers_map.get(status, "⚠️ <b>OPEN MANUALLY!</b>")
    pay_str  = job.get("pay_display") or f"{job.get('pay','?'):.2f}"
    dist_str = f"\n📏 Distance: <b>{distance} miles</b>" if distance else ""
    night    = " 🌙 NIGHT SHIFT" if is_night_shift(job.get("schedule","")) else ""
    fresh    = " 🌿 FRESH" if is_fresh_job(job) else ""
    job_id   = job.get("id","")
    job_link = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}" if job_id != "TEST-001" else job.get("link","https://www.jobsatamazon.co.uk")

    text = f"""{header}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location','Unknown')}</b>
📦 {job.get('title','Warehouse Operative')}{night}{fresh}
💰 <b>£{pay_str}/hr</b>
📋 {job.get('contract','Seasonal')}
📅 First Day: <b>{job.get('firstDay','TBC')}</b>
🕘 Schedule: <b>{job.get('schedule','TBC')}</b>
🕐 Hours/Week: <b>{job.get('hours','TBC')}</b>{dist_str}
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "applied":
        text += "\n🎉 <b>Check your Amazon Jobs dashboard!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "ready":
        text += "\n👆 <b>TAP OPEN APPLICATION → Log in → Submit!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "fresh_alert":
        text += "\n🌿 <b>Fresh excluded from auto-submit — apply manually!</b>\n━━━━━━━━━━━━━━━━━━━━━"

    markup = {
        "inline_keyboard": [
            [{"text": "🚀 OPEN APPLICATION", "url": job_link}],
            [{"text": "✅ APPLIED", "callback_data": f"applied_{job['id']}"},
             {"text": "⏭️ SKIP",   "callback_data": f"skip_{job['id']}"}]
        ]
    }
    await tg_send(text, markup, chat_id=cid)

# ─── AUTO SUBMIT ─────────────────────────────────────────────────────────────
async def auto_submit_account(job, account, chat_id=None):
    cid    = chat_id or CHAT_ID
    acc_id = account["id"]

    if is_fresh_job(job):
        log.info("🌿 Fresh job — skipping auto-submit")
        await tg_alert(job, "fresh_alert", chat_id=cid)
        return

    log.info(f"🤖 Auto-submitting Account {acc_id}: {job['location']}")
    await tg_alert(job, "applying", chat_id=cid, account_id=acc_id)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox","--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB", timezone_id="Europe/London",
            )

            saved = load_cookies()
            if saved:
                await context.add_cookies(saved)
            elif account["session"]:
                await context.add_cookies(account["session"])
            elif account["cookies"]:
                try:
                    await context.add_cookies(json.loads(account["cookies"]))
                except:
                    pass

            page = await context.new_page()
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)

            content = await page.inner_text("body")
            if "sign in" in content.lower() or "log in" in content.lower():
                log.warning("🔐 Login wall hit — sending manual alert")
                await tg_alert(job, "ready", chat_id=cid)
                await browser.close()
                return

            applied = False

            # JS injection — most reliable
            try:
                applied = await page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button, a'));
                    const btn  = btns.find(b =>
                        b.textContent.trim().toLowerCase().includes('apply') && !b.disabled
                    );
                    if (btn) { btn.click(); return true; }
                    return false;
                }""")
                if applied:
                    await page.wait_for_timeout(3000)
                    log.info("✅ Apply clicked via JS")
            except:
                pass

            # CSS selector fallback
            if not applied:
                for sel in ["button:has-text('Apply now')","button:has-text('Apply')","a:has-text('Apply')","[data-test='apply-button']"]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            await page.wait_for_timeout(2500)
                            applied = True
                            log.info(f"✅ Apply clicked: {sel}")
                            break
                    except:
                        pass

            if not applied:
                log.warning("❌ Apply button not found")
                await tg_alert(job, "ready", chat_id=cid)
                await browser.close()
                return

            # Continue / Next
            for sel in ["button:has-text('Continue')","button:has-text('Next')"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=4000)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except:
                    pass

            # Start Application
            for sel in ["button:has-text('Start Application')","[data-test='start-application']"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=5000)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(3000)
                        log.info("✅ Start Application clicked")
                        break
                except:
                    pass

            # Shift selection — pick best shift
            try:
                await page.wait_for_selector("button:has-text('Select this job')", timeout=8000)
                shift_buttons = await page.query_selector_all("button:has-text('Select this job')")
                if shift_buttons:
                    best_btn = shift_buttons[0]
                    best_pri = 999
                    cards    = await page.query_selector_all("[class*='shift'],[class*='card'],[class*='schedule']")
                    for i, card in enumerate(cards[:len(shift_buttons)]):
                        try:
                            pri = shift_priority(await card.inner_text())
                            if pri < best_pri:
                                best_pri = pri
                                if i < len(shift_buttons):
                                    best_btn = shift_buttons[i]
                        except:
                            pass
                    await best_btn.click()
                    await page.wait_for_timeout(3000)
                    log.info(f"✅ Shift selected (priority {best_pri})")
            except:
                log.info("ℹ️ No shift selection page")

            # Accept Offer
            try:
                btn = await page.wait_for_selector("button:has-text('Accept Offer')", timeout=5000)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    log.info("✅ Offer accepted")
            except:
                pass

            # Check success
            final_url     = page.url
            final_content = await page.inner_text("body")
            success = (
                "thank you" in final_content.lower() or
                "applied" in final_content.lower() or
                "checklist" in final_url or
                "dashboard" in final_url or
                "confirmation" in final_url
            )

            if success:
                log.info(f"🎉 Applied! {job['location']}")
                await tg_alert(job, "applied", chat_id=cid, account_id=acc_id)
                fresh_cookies = await context.cookies()
                save_cookies(fresh_cookies)
                account["session"] = fresh_cookies
            else:
                log.warning("⚠️ Apply uncertain — sending manual alert")
                job["link"] = final_url if final_url != "about:blank" else job["link"]
                await tg_alert(job, "ready", chat_id=cid)

            await browser.close()

    except Exception as e:
        log.error(f"Auto-submit error: {e}")
        await tg_alert(job, "ready", chat_id=cid)

# ─── MAIN CHECK ──────────────────────────────────────────────────────────────
async def check_jobs():
    global known_jobs, job_history, posting_times
    if bot_paused:
        return 0

    jobs      = await fetch_jobs()
    new_count = 0

    for job in jobs:
        jid = job["id"]
        if jid in known_jobs:
            continue

        new_count += 1
        posting_times[job["location"][:20]].append(datetime.utcnow().hour)
        log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr")

        job = await fetch_job_details(job)
        known_jobs[jid] = job
        job_history.append(job)

        for sub_cid, prefs in list(subscribers.items()):
            if not prefs.get("setup_complete"):
                continue
            if not job_matches_type(job, prefs.get("job_type","both")):
                continue

            job_postcode  = job.get("postcode","")
            locations     = prefs.get("locations",[])
            radius        = prefs.get("radius", 50)
            best_distance = None
            too_far       = False

            if locations and job_postcode:
                distances = []
                for loc in locations:
                    d = await job_distance_miles(job_postcode, loc)
                    if d is not None:
                        distances.append(d)
                        if best_distance is None or d < best_distance:
                            best_distance = d
                if distances:
                    too_far = min(distances) > radius

            if too_far:
                log.info(f"📍 Sub {sub_cid}: too far ({best_distance}mi)")
                continue

            await tg_alert(job, "new", chat_id=sub_cid, distance=best_distance)

            # Owner always auto-submits regardless of saved preference
            is_owner    = (sub_cid == CHAT_ID)
            should_auto = (is_owner or prefs.get("auto_apply")) and ACCOUNTS and not is_fresh_job(job)

            if should_auto:
                log.info(f"🤖 Auto-submitting for {'owner' if is_owner else sub_cid}")
                asyncio.create_task(auto_submit_account(job, ACCOUNTS[0], chat_id=sub_cid))
            elif is_fresh_job(job):
                pass
            else:
                asyncio.create_task(navigate_job(job, sub_cid))

    if new_count == 0:
        log.info(f"👑 No new jobs — {len(known_jobs)} tracked")
    return new_count

# ─── AUTO NAVIGATE ───────────────────────────────────────────────────────────
async def navigate_job(job, chat_id=None):
    cid = chat_id or CHAT_ID
    await tg_alert(job, "navigating", chat_id=cid)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            saved = load_cookies()
            if saved:
                await context.add_cookies(saved)
            page = await context.new_page()
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
            for sel in ["button:has-text('Apply')","a:has-text('Apply')"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except:
                    pass
            job["link"] = page.url if page.url != "about:blank" else job["link"]
            await browser.close()
            await tg_alert(job, "ready", chat_id=cid)
    except Exception as e:
        log.error(f"Navigate error: {e}")

# ─── DAILY SUMMARY ───────────────────────────────────────────────────────────
async def send_daily_summary():
    while True:
        now = datetime.utcnow()
        if now.hour == 7 and now.minute == 0:
            today = [j for j in job_history if j.get("found_at","")[:10] == now.strftime("%Y-%m-%d")]
            if today:
                best    = max(today, key=lambda x: x.get("pay",0))
                avg_pay = sum(j.get("pay",0) for j in today) / len(today)
                nights  = sum(1 for j in today if is_night_shift(j.get("schedule","")))
                await tg_send(f"""📊 <b>Daily Summary</b>
━━━━━━━━━━━━━━━━━
📅 {now.strftime('%Y-%m-%d')}
🆕 Jobs: {len(today)} | 🌙 Nights: {nights}
💰 Avg: £{avg_pay:.2f}/hr
⭐ Best: {best.get('location','?')} £{best.get('pay','?')}/hr
👥 Subscribers: {len(subscribers)}
━━━━━━━━━━━━━━━━━
Keep going Yonas! 💪""")
            await asyncio.sleep(60)
        await asyncio.sleep(30)

async def session_refresh_loop():
    while True:
        await asyncio.sleep(4 * 60 * 60)
        if os.path.exists(COOKIES_FILE):
            os.remove(COOKIES_FILE)
        log.info("🔄 Cookie cache cleared — fresh session next scan")

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def handle_updates():
    offset = 0
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout=10") as r:
                    data = await r.json()
                    for update in data.get("result",[]):
                        offset = update["update_id"] + 1
                        await process_update(update)
        except Exception as e:
            log.error(f"Update error: {e}")
        await asyncio.sleep(2)

async def process_update(update):
    global bot_paused

    if "callback_query" in update:
        cb = update["callback_query"]
        d  = cb.get("data","")
        if d.startswith("applied_"): await tg_send("✅ Applied! Good luck! 💪🔥")
        elif d.startswith("skip_"):  await tg_send("⏭️ Skipped!")
        return

    msg     = update.get("message",{})
    text    = msg.get("text","").strip()
    cid     = str(msg.get("chat",{}).get("id", CHAT_ID))
    name    = msg.get("chat",{}).get("first_name","Friend")
    text_lw = text.lower()

    if cid in onboarding:
        await handle_onboarding(cid, text)
        return

    if text_lw == "/start":
        if cid in subscribers and subscribers[cid].get("setup_complete"):
            sub  = subscribers[cid]
            locs = ", ".join(sub.get("locations",[]))
            auto = "✅ ON (always)" if cid == CHAT_ID else ("✅ ON" if sub.get("auto_apply") else "❌ OFF")
            await tg_send(f"""👋 <b>Welcome back {name}!</b>

📍 {locs}
🚗 {sub.get('radius',30)} miles
📋 {sub.get('job_type','both')}
🤖 Auto-submit: {auto}

Use /help for all commands!""", chat_id=cid)
        else:
            await start_onboarding(cid, name)

    elif text_lw == "/setup":
        await start_onboarding(cid, name)

    elif text_lw == "/mypreferences":
        sub = subscribers.get(cid,{})
        if not sub:
            await tg_send("You haven't set up yet! Send /start", chat_id=cid)
            return
        locs   = sub.get("locations",[])
        jt     = sub.get("job_type","both")
        jlabel = {"fulltime":"Full-time only","parttime":"Part-time only","both":"Full-time & Part-time"}.get(jt,"Both")
        ltext  = "\n".join([f"📍 {'⭐ ' if i==0 else ''}{l}" for i, l in enumerate(locs)])
        auto   = "✅ ON (always)" if cid == CHAT_ID else ("✅ ON" if sub.get("auto_apply") else "❌ OFF")
        await tg_send(f"""📋 <b>Your Preferences</b>
━━━━━━━━━━━━━━━━━
{ltext}
🚗 Radius: {sub.get('radius',30)} miles
📋 Job type: {jlabel}
🤖 Auto-submit: {auto}
━━━━━━━━━━━━━━━━━
Use /setup to update""", chat_id=cid)

    elif text_lw == "/autoon" and cid == CHAT_ID:
        subscribers[cid]["auto_apply"] = True
        save_subscribers(subscribers)
        await tg_send("🤖 Auto-submit ON ✅", chat_id=cid)

    elif text_lw == "/autooff" and cid == CHAT_ID:
        subscribers[cid]["auto_apply"] = False
        save_subscribers(subscribers)
        await tg_send("🤖 Auto-submit OFF ❌ (still fires as owner)", chat_id=cid)

    elif text_lw == "/status":
        peak  = is_peak_time()
        speed = "3s ⚡ PEAK" if peak else "10s 🔄 Normal"
        proxy = "✅ Decodo UK" if get_proxy_url() else "❌ No proxy"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {"⏸️ PAUSED" if bot_paused else "✅ RUNNING"}
🌐 Proxy: {proxy}
👥 Subscribers: {len(subscribers)}
🤖 Accounts: {len(ACCOUNTS)}
Jobs tracked: {len(known_jobs)}
History: {len(job_history)}
⚡ Speed: {speed}
━━━━━━━━━━━━━━━━━""", chat_id=cid)

    elif text_lw == "/subscribers" and cid == CHAT_ID:
        txt = f"👥 <b>{len(subscribers)} Subscribers:</b>\n━━━━━━━━━━━\n"
        for scid, sub in subscribers.items():
            locs = ", ".join(sub.get("locations",[]))
            auto = "✅" if sub.get("auto_apply") or scid == CHAT_ID else "❌"
            txt += f"• {sub.get('name','?')} | {locs} | {sub.get('radius',30)}mi | Auto:{auto}\n"
        await tg_send(txt, chat_id=cid)

    elif text_lw == "/scrape":
        await tg_send("🔍 <b>Scanning ALL UK Amazon jobs...</b>", chat_id=cid)
        count = await check_jobs()
        await tg_send(f"✅ New: {count} | Tracked: {len(known_jobs)}\n{'🎉 New jobs found!' if count > 0 else '⏳ No new jobs this scan'}", chat_id=cid)

    elif text_lw == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet — send /scrape to scan!", chat_id=cid)
        else:
            txt = f"📋 <b>Last {min(5,len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                night = "🌙" if is_night_shift(job.get("schedule","")) else "☀️"
                fresh = "🌿" if is_fresh_job(job) else ""
                txt  += f"{night}{fresh} {job.get('location')}\n💰 £{job.get('pay')}/hr | {job.get('contract')}\n📅 {job.get('firstDay','TBC')}\n\n"
            await tg_send(txt, chat_id=cid)

    elif text_lw == "/history":
        if not job_history:
            await tg_send("📭 No history yet!", chat_id=cid)
        else:
            total  = len(job_history)
            avg    = sum(j.get("pay",0) for j in job_history) / total
            best   = max(job_history, key=lambda x: x.get("pay",0))
            nights = sum(1 for j in job_history if is_night_shift(j.get("schedule","")))
            await tg_send(f"""📊 <b>History</b>
Total: {total} | 🌙 Nights: {nights}
Avg: £{avg:.2f}/hr
Best: {best.get('location','?')} £{best.get('pay','?')}/hr""", chat_id=cid)

    elif text_lw == "/test":
        await tg_alert({
            "id": "TEST-001", "title": "Warehouse Operative",
            "location": "Birmingham, England (West Midlands) B21 0UT",
            "postcode": "B21 0UT", "pay": 14.30, "pay_display": "14.30",
            "contract": "Full-time", "firstDay": "2026-05-14",
            "schedule": "Mon, Tue, Wed, Thu 23:45-10:15", "hours": "40",
            "link": "https://www.jobsatamazon.co.uk",
        }, "new", chat_id=cid, distance=2.1)

    elif text_lw == "/pause" and cid == CHAT_ID:
        bot_paused = True
        await tg_send("⏸️ Bot paused.", chat_id=cid)

    elif text_lw == "/resume" and cid == CHAT_ID:
        bot_paused = False
        await tg_send("▶️ Bot resumed! 🔥", chat_id=cid)

    elif text_lw == "/clearcache" and cid == CHAT_ID:
        known_jobs.clear()
        await tg_send("🗑️ Cache cleared — bot will re-alert all jobs next scan.", chat_id=cid)

    elif text_lw == "/help":
        await tg_send("""👑 <b>Amazon KING BOT v12</b>
━━━━━━━━━━━━━━━━━
/start          — Welcome & setup
/setup          — Update preferences
/mypreferences  — View settings
/status         — Bot status + proxy
/scrape         — Scan now
/jobs           — Recent jobs
/history        — All time stats
/test           — Test alert
/autoon         — Enable auto-submit
/autooff        — Disable auto-submit
/subscribers    — All users (admin)
/clearcache     — Reset job cache (admin)
/pause          — Pause bot (admin)
/resume         — Resume bot (admin)
━━━━━━━━━━━━━━━━━
🔗 Share: t.me/Jibhub_bot
🌿 Amazon Fresh = alert only
🌐 Powered by Decodo UK proxy""", chat_id=cid)

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info(f"👑 Amazon KING BOT v12 Starting!")
    log.info(f"🌐 Proxy: {'Decodo configured ✅' if get_proxy_url() else 'No proxy ❌'}")
    log.info(f"👥 Subscribers: {len(subscribers)}")
    log.info(f"🤖 Accounts: {len(ACCOUNTS)}")

    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())
    asyncio.create_task(session_refresh_loop())

    await asyncio.sleep(2)
    proxy_status = "✅ Decodo UK Proxy active" if get_proxy_url() else "❌ No proxy — add DECODO credentials"
    await tg_send(f"""👑 <b>Amazon KING BOT v12 ONLINE!</b>
🌐 {proxy_status}
✅ Direct GraphQL via UK IP (~5KB/scan)
✅ Auto-submit ON for owner (always)
✅ Amazon Fresh blocked from auto
✅ Smart warehouse job filtering
✅ 3s peak / 10s normal scanning
👥 {len(subscribers)} subscriber(s) | 🤖 {len(ACCOUNTS)} account(s)
━━━━━━━━━━━━━━━━━
Send /scrape to check now!
Share: t.me/Jibhub_bot""")

    await check_jobs()

    while True:
        await asyncio.sleep(3 if is_peak_time() else 10)
        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
