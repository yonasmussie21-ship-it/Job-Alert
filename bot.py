import asyncio
import os
import json
import logging
import aiohttp
import re
import math
from datetime import datetime
from urllib.parse import quote
from playwright.async_api import async_playwright
from collections import defaultdict

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
CHAT_ID      = os.environ.get("CHAT_ID", "1027065157")
DECODO_USER  = os.environ.get("DECODO_USER", "")
DECODO_PASS  = os.environ.get("DECODO_PASS", "")
DECODO_HOST  = os.environ.get("DECODO_HOST", "gb.decodo.com")
DECODO_PORT  = os.environ.get("DECODO_PORT", "30004")
AMAZON_EMAIL = os.environ.get("AMAZON_EMAIL", "")
AMAZON_PIN   = os.environ.get("AMAZON_PIN", "")

# ─── MULTI-ACCOUNT CONFIG ────────────────────────────────────────────────────
ACCOUNTS = []
for i in range(1, 6):
    email   = os.environ.get(f"AMAZON_EMAIL_{i}", "")
    pin     = os.environ.get(f"AMAZON_PIN_{i}", "")
    cookies = os.environ.get(f"AMAZON_COOKIES_{i}", "")
    if i == 1:
        email   = email   or AMAZON_EMAIL
        pin     = pin     or AMAZON_PIN
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
otp_waiting   = {}
otp_codes     = {}

TELEGRAM_API     = f"https://api.telegram.org/bot{BOT_TOKEN}"
SUBSCRIBERS_FILE = "/tmp/subscribers.json"
COOKIES_FILE     = "/tmp/amazon_session.json"

# ─── TIERS ───────────────────────────────────────────────────────────────────
# free     = alerts only
# standard = alerts + application prepared (stops before submit) £5/mo
# premium  = full auto-submit £10/mo
# owner    = everything, always

def get_tier(sub_cid, prefs):
    if sub_cid == CHAT_ID:
        return "owner"
    return prefs.get("tier", "free")

# ─── JOB FILTERING ───────────────────────────────────────────────────────────
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
    "corporate", "marketing", " hr ", "finance", "recruiter",
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

def score_job(job):
    """Score job quality. Returns (score, skip)."""
    hours    = job.get("hours")
    contract = job.get("contract", "").lower()
    schedule = job.get("schedule", "")

    # Hard filter — skip part-time
    if hours:
        try:
            if int(hours) < 36:
                log.info(f"⏭️ Skipped (part-time {hours}hrs)")
                return 0, True
        except:
            pass

    score = 0
    if "permanent" in contract:      score += 50
    if is_night_shift(schedule):     score += 15
    return score, False

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

def get_proxy_url():
    if DECODO_USER and DECODO_PASS:
        encoded_pass = quote(DECODO_PASS, safe="")
        return f"http://{DECODO_USER}:{encoded_pass}@{DECODO_HOST}:{DECODO_PORT}"
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
        "tier": "owner",
        "joined": datetime.utcnow().isoformat(),
    }
    save_subscribers(subscribers)
else:
    subscribers[CHAT_ID]["auto_apply"] = True
    subscribers[CHAT_ID]["tier"]       = "owner"
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
I find UK warehouse jobs and alert you instantly!

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

<b>Step 2 of 4 — Your Location</b>
Enter your city or postcode:
Examples: <b>Birmingham</b>, <b>Leeds</b>, <b>B1 1BB</b>""", chat_id=cid)

    elif step == "location_1":
        state["locations"] = [text.strip()]
        state["step"]      = "location_2"
        onboarding[cid]    = state
        await tg_send(f"""✅ Location: <b>{text.strip()}</b>

<b>Step 3 of 4 — Second Location (Optional)</b>
Add another location or type <b>DONE</b> to skip.""", chat_id=cid)

    elif step == "location_2":
        if text.strip().upper() not in ["DONE", "SKIP", "NO", "N"]:
            state["locations"].append(text.strip())
        state["step"]   = "radius"
        onboarding[cid] = state
        await tg_send("""<b>Step 4 of 4 — Travel Radius</b>
How far can you travel from your location?
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
        jlabel = {"fulltime": "Full-time only", "parttime": "Part-time only",
                  "both": "Full-time & Part-time"}.get(jt)
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
                "name":           state.get("name", "Friend"),
                "locations":      state.get("locations", []),
                "radius":         state.get("radius", 30),
                "job_type":       state.get("job_type", "both"),
                "setup_complete": True,
                "auto_apply":     False,
                "tier":           "free",
                "joined":         datetime.utcnow().isoformat(),
            }
            save_subscribers(subscribers)
            onboarding.pop(cid, None)
            await tg_send("""🎉 <b>You're all set!</b>

You'll get instant alerts when Amazon warehouse jobs drop near you!

Use /help for all commands.""", chat_id=cid)
        elif text.upper() == "RESTART":
            await start_onboarding(cid, state.get("name", "there"))
        else:
            await tg_send("Reply <b>CONFIRM</b> or <b>RESTART</b>", chat_id=cid)

# ─── PARSE CARD ───────────────────────────────────────────────────────────────
def parse_card(card):
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

        # Hard filter — skip part time
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

# ─── FETCH FULL JOB DETAILS ───────────────────────────────────────────────────
async def fetch_job_details(job):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox","--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            await context.route(
                "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}",
                lambda route: route.abort()
            )
            saved = load_cookies()
            if saved:
                await context.add_cookies(saved)

            page = await context.new_page()

            # Capture shift data from GraphQL
            shifts_data = []
            async def handle_response(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        data       = await response.json()
                        job_detail = data.get("data", {}).get("getJobDetailByJobId", {})
                        if job_detail:
                            shifts = (job_detail.get("jobCardDetail", {})
                                               .get("scheduleDetails", []))
                            if shifts:
                                shifts_data.extend(shifts)
                except:
                    pass

            page.on("response", handle_response)
            await page.goto(job["link"], wait_until="domcontentloaded", timeout=60000)

            # Wait for React to fully render
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.split('Loading').length < 4",
                    timeout=10000
                )
            except:
                pass
            await page.wait_for_timeout(2000)

            content = await page.inner_text("body")

            # Extract First Day
            for pattern in [
                r'(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+,?\s+\d+\s+[A-Za-z]+\s+\d{4})',
                r'(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+ \d+, \d{4})',
                r'(?:Tentative start date|Start date|First day)[:\s]+(\d{4}-\d{2}-\d{2})',
            ]:
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    job["firstDay"] = m.group(1).strip()
                    break

            # Extract Shifts
            shift_patterns = re.findall(
                r'([A-Za-z]{3}(?:,\s*[A-Za-z]{3})*\s+\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})',
                content
            )
            if shift_patterns:
                unique_shifts   = list(dict.fromkeys(shift_patterns))
                job["shifts"]   = unique_shifts
                job["schedule"] = unique_shifts[0]
                log.info(f"✅ Found {len(unique_shifts)} shifts")
            elif shifts_data:
                job["shifts"]   = [s.get("scheduleDisplay","") for s in shifts_data if s.get("scheduleDisplay")]
                job["schedule"] = job["shifts"][0] if job["shifts"] else None

            # Extract Hours
            m = re.search(r'(\d+)\s*(?:hrs?|hours?)\s*(?:per\s*week|/\s*week)', content, re.IGNORECASE)
            if m:
                job["hours"] = m.group(1)

            # Extract Contract
            for ct in ["Permanent","Full-time","Fixed-term","Seasonal","Temporary","Part-time"]:
                if ct.lower() in content.lower():
                    job["contract"] = ct
                    break

            # Extract Description (clean)
            desc_match = re.search(
                r'((?:Pick|Sort|Process|Receive|Load|Unload|Pack|Ship|Stow)[^.\n]{15,120}\.)',
                content, re.IGNORECASE
            )
            if desc_match:
                desc = desc_match.group(1).strip()
                if "Loading" not in desc and len(desc) >= 20:
                    job["description"] = desc

            await browser.close()
            log.info(f"✅ Details: day={job.get('firstDay','?')} "
                     f"shifts={len(job.get('shifts',[]))} "
                     f"hrs={job.get('hours','?')}")

    except Exception as e:
        log.warning(f"Detail fetch error: {e}")
    return job

# ─── CORE SCRAPER — ONE SEARCH, ALL UK JOBS ───────────────────────────────────
async def fetch_jobs():
    """
    Simple and correct:
    1. Load jobsatamazon.co.uk once
    2. Capture the real GraphQL request
    3. Replay through Decodo UK proxy → Amazon returns ALL UK jobs
    4. Parse and filter results
    """
    all_jobs     = {}
    proxy        = get_proxy_url()
    captured     = {}
    direct_cards = []

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
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            await context.route(
                "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}",
                lambda route: route.abort()
            )
            saved = load_cookies()
            if saved:
                await context.add_cookies(saved)

            page = await context.new_page()

            async def on_request(request):
                if "/graphql" in request.url and not captured:
                    try:
                        body = request.post_data
                        if body and "searchJobCardsByLocation" in body:
                            headers = dict(request.headers)
                            for h in ["content-length","host",":method",
                                      ":path",":scheme",":authority"]:
                                headers.pop(h, None)
                            # Only modify valid fields
                            try:
                                body_json  = json.loads(body)
                                search_req = body_json.get("variables", {}).get("searchJobRequest", {})
                                search_req["country"]  = "United Kingdom"
                                search_req["keyWords"] = ""
                                search_req["pageSize"] = 100
                                captured["body"] = json.dumps(body_json)
                            except:
                                captured["body"] = body
                            captured["url"]     = request.url
                            captured["headers"] = headers
                            log.info(f"✅ Captured GraphQL — URL: {request.url}")
                    except Exception as e:
                        log.warning(f"Capture error: {e}")

            async def on_response(response):
                if "/graphql" in response.url and response.status == 200:
                    try:
                        data  = await response.json()
                        cards = (data.get("data", {})
                                     .get("searchJobCardsByLocation", {})
                                     .get("jobCards", []))
                        if cards:
                            log.info(f"🎯 Browser intercepted {len(cards)} jobs directly")
                            direct_cards.extend(cards)
                    except:
                        pass

            page.on("request",  on_request)
            page.on("response", on_response)

            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="domcontentloaded", timeout=60000
            )
            await page.wait_for_timeout(3000)

            if not captured:
                log.info("⚡ Scrolling to trigger GraphQL...")
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(3000)
                await page.mouse.wheel(0, -3000)
                await page.wait_for_timeout(2000)

            cookies = await context.cookies()
            if cookies:
                save_cookies(cookies)
            await browser.close()

    except Exception as e:
        log.error(f"Browser error: {e}")

    # Replay via Decodo UK proxy → gets ALL UK jobs
    if captured and proxy:
        log.info("🌐 Replaying via Decodo UK proxy (all UK jobs)...")
        cards = await replay_via_proxy(
            url=captured["url"],
            headers=captured["headers"],
            body=captured["body"],
            proxy=proxy
        )
        if cards:
            log.info(f"🎯 Decodo returned {len(cards)} jobs!")
            for card in cards:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job
        else:
            log.warning("⚠️ Proxy returned 0 — using browser results")
            for card in direct_cards:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job
    else:
        for card in direct_cards:
            job = parse_card(card)
            if job and job["id"] not in all_jobs:
                all_jobs[job["id"]] = job

    log.info(f"👑 Total unique UK warehouse jobs: {len(all_jobs)}")
    return list(all_jobs.values())


async def replay_via_proxy(url, headers, body, proxy):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, data=body, headers=headers, proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                status = response.status
                text   = await response.text()
                if status != 200:
                    log.warning(f"⚠️ Proxy status {status}: {text[:300]}")
                    return []
                try:
                    data = json.loads(text)
                except:
                    log.warning(f"⚠️ Invalid JSON: {text[:200]}")
                    return []
                if "errors" in data:
                    log.warning(f"⚠️ GraphQL errors: {json.dumps(data['errors'])[:300]}")
                    return []
                return (data.get("data", {})
                            .get("searchJobCardsByLocation", {})
                            .get("jobCards", []))
    except Exception as e:
        log.error(f"Proxy replay error: {e}")
        return []

# ─── ALERT ───────────────────────────────────────────────────────────────────
async def tg_alert(job, status="new", chat_id=None, distance=None,
                   account_id=None, shift_index=None, total_shifts=None,
                   score=None):
    cid = chat_id or CHAT_ID

    headers_map = {
        "new":         "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>",
        "applying":    f"🤖 <b>AUTO-SUBMITTING{' (Acc '+str(account_id)+')' if account_id else ''}...</b>",
        "applied":     f"✅ <b>APPLIED FOR YOU{' (Acc '+str(account_id)+')' if account_id else ''}!</b>",
        "ready":       "👆 <b>APPLICATION READY — TAP TO SUBMIT!</b>",
        "prepared":    "✅ <b>SHIFT SELECTED — TAP TO CONTINUE!</b>",
        "fresh_alert": "🌿 <b>AMAZON FRESH — MANUAL APPLY ONLY</b>",
    }
    header = headers_map.get(status, "⚠️ <b>OPEN MANUALLY!</b>")

    pay_str  = job.get("pay_display") or f"{job.get('pay','?'):.2f}"
    dist_str = f"\n📏 Distance: <b>{distance} miles</b>" if distance else ""

    shifts   = job.get("shifts", [])
    schedule = job.get("schedule")
    if shift_index is not None and shifts and shift_index < len(shifts):
        schedule = shifts[shift_index]

    night    = " 🌙 NIGHT SHIFT" if is_night_shift(schedule or "") else ""
    fresh    = " 🌿 FRESH" if is_fresh_job(job) else ""
    perm     = " ⭐ PERMANENT" if "permanent" in job.get("contract","").lower() else ""
    shift_str = ""
    if total_shifts and total_shifts > 1 and shift_index is not None:
        shift_str = f"\n🔄 <b>Shift {shift_index+1} of {total_shifts}</b>"
    score_str = f"\n⭐ Score: <b>{score}</b>" if score else ""

    first_day_str = job.get("firstDay") or "See listing"
    schedule_str  = schedule or "See listing"
    hours_str     = job.get("hours") or "See listing"
    desc_str      = f"\n📝 {job.get('description')}" if job.get("description") else ""

    job_id   = job.get("id","")
    job_link = (f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}"
                if job_id != "TEST-001"
                else job.get("link","https://www.jobsatamazon.co.uk"))

    text = f"""{header}{shift_str}{perm}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location','Unknown')}</b>
📦 {job.get('title','Warehouse Operative')}{night}{fresh}
💰 <b>£{pay_str}/hr</b>
📋 {job.get('contract','Seasonal')}
📅 First Day: <b>{first_day_str}</b>
🕘 Schedule: <b>{schedule_str}</b>
🕐 Hours/Week: <b>{hours_str}</b>{dist_str}{score_str}{desc_str}
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "applied":
        text += "\n🎉 <b>Check your Amazon Jobs dashboard!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "ready":
        text += "\n👆 <b>Tap OPEN APPLICATION → Log in → Submit!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "prepared":
        text += "\n✅ <b>Bot selected the best shift for you!</b>\n👆 Tap OPEN APPLICATION → Schedule appointment → Review → Submit!\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "fresh_alert":
        text += "\n🌿 <b>Fresh excluded from auto-submit</b>\n━━━━━━━━━━━━━━━━━━━━━"

    markup = {
        "inline_keyboard": [
            [{"text": "🚀 OPEN APPLICATION",  "url": job_link}],
            [{"text": "✅ MARK SUBMITTED",    "callback_data": f"applied_{job['id']}"},
             {"text": "❌ IGNORE",            "callback_data": f"skip_{job['id']}"}]
        ]
    } if status in ["new","ready","prepared","fresh_alert"] else None

    await tg_send(text, markup, chat_id=cid)


async def send_all_shifts(job, status="new", chat_id=None, distance=None, score=None):
    shifts = job.get("shifts", [])
    if not shifts or len(shifts) <= 1:
        await tg_alert(job, status, chat_id=chat_id, distance=distance, score=score)
        return
    for i in range(len(shifts)):
        await tg_alert(job, status, chat_id=chat_id, distance=distance,
                       shift_index=i, total_shifts=len(shifts), score=score)
        await asyncio.sleep(0.5)

# ─── OTP LOGIN ────────────────────────────────────────────────────────────────
async def amazon_login_with_otp(page, chat_id):
    try:
        log.info("🔐 Attempting Amazon auto-login...")

        # Step 1 — Enter email
        await page.wait_for_timeout(2000)
        for sel in ["input[type='email']", "input[name='email']", "input[name='username']"]:
            email_field = await page.query_selector(sel)
            if email_field and await email_field.is_visible():
                await email_field.clear()
                await email_field.fill(AMAZON_EMAIL)
                log.info(f"✅ Email entered")
                break

        # Click Continue/Next after email
        for sel in ["button:has-text('Continue')", "input[type='submit']",
                    "button[type='submit']", "button:has-text('Next')"]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    break
            except:
                pass

        # Step 2 — Enter PIN/Password
        await page.wait_for_timeout(2000)
        for sel in ["input[type='password']", "input[name='password']",
                    "input[name='pin']", "input[placeholder*='PIN']",
                    "input[placeholder*='password']"]:
            pass_field = await page.query_selector(sel)
            if pass_field and await pass_field.is_visible():
                await pass_field.clear()
                await pass_field.fill(AMAZON_PIN)
                log.info(f"✅ PIN entered")
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(3000)
                break

        # Step 3 — Check for OTP
        content = await page.inner_text("body")
        otp_keywords = ["verification", "otp", "one-time", "passcode",
                        "authentication code", "security code", "verify"]

        if any(w in content.lower() for w in otp_keywords):
            log.info("📱 OTP required — asking user...")
            await tg_send(
                "🔐 <b>Amazon needs your OTP code!</b>\n\n"
                "Check your phone/email for the 6-digit code.\n"
                "<b>Reply here with the code now:</b>",
                chat_id=chat_id
            )
            event = asyncio.Event()
            otp_waiting[chat_id] = event
            try:
                await asyncio.wait_for(event.wait(), timeout=120)
                otp = otp_codes.pop(chat_id, None)
                otp_waiting.pop(chat_id, None)

                if otp:
                    # Try multiple OTP field selectors
                    for sel in [
                        "input[autocomplete='one-time-code']",
                        "input[name='otpCode']",
                        "input[type='text']",
                        "input[inputmode='numeric']",
                        "input[placeholder*='code']",
                    ]:
                        otp_field = await page.query_selector(sel)
                        if otp_field and await otp_field.is_visible():
                            await otp_field.clear()
                            await otp_field.fill(otp)
                            await page.keyboard.press("Enter")
                            await page.wait_for_timeout(3000)
                            log.info("✅ OTP submitted!")
                            break

                    # Verify login succeeded
                    content = await page.inner_text("body")
                    if any(w in content.lower() for w in ["sign in", "login", "otp", "verification"]):
                        log.warning("⚠️ Still on login page after OTP")
                        return False

                    log.info("✅ Login successful!")
                    return True
                else:
                    log.warning("⚠️ No OTP received")
                    return False

            except asyncio.TimeoutError:
                await tg_send(
                    "⏱️ <b>OTP timeout!</b> Please apply manually.",
                    chat_id=chat_id
                )
                return False

        # Check if login succeeded without OTP
        content = await page.inner_text("body")
        if any(w in content.lower() for w in ["sign in", "log in", "enter your email"]):
            log.warning("⚠️ Still on login page — login may have failed")
            return False

        log.info("✅ Login successful (no OTP needed)!")
        return True

    except Exception as e:
        log.error(f"Login error: {e}")
        return False

# ─── DIRECT API AUTO SUBMIT ───────────────────────────────────────────────────
async def extract_auth_from_cookies(cookies):
    """Extract HVH_ACCESS_TOKEN and other auth from cookies"""
    auth = {}
    for c in cookies:
        name = c.get("name", "")
        if name == "HVH_ACCESS_TOKEN":
            auth["token"] = c.get("value", "")
        elif name == "hvhcid":
            auth["hvhcid"] = c.get("value", "")
        elif name == "aws-waf-token":
            auth["waf_token"] = c.get("value", "")
        elif name == "JSESSIONID":
            auth["jsessionid"] = c.get("value", "")
    return auth

async def get_candidate_applications(cookies, auth):
    """Query Amazon API for active applications using GraphQL"""
    try:
        # Build cookie string
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "cookie": cookie_str,
            "authorization": auth.get("token", ""),
            "bb-ui-version": "bb-ui-v2",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "origin": "https://www.jobsatamazon.co.uk",
            "referer": "https://www.jobsatamazon.co.uk/app",
        }

        # First get candidate ID
        query_candidate = {
            "operationName": "queryCandidate",
            "query": """query queryCandidate($bbCandidateId: String!) {
                queryCandidate(bbCandidateId: $bbCandidateId) {
                    candidateId
                    candidateSFId
                    firstName
                    lastName
                    emailId
                    __typename
                }
            }""",
            "variables": {"bbCandidateId": auth.get("hvhcid", "")}
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://www.jobsatamazon.co.uk/graphql",
                json=query_candidate,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidate = data.get("data", {}).get("queryCandidate", {})
                    log.info(f"✅ Got candidate: {candidate.get('firstName')} {candidate.get('lastName')}")

            # Now get applications
            query_apps = {
                "operationName": "queryApplicationsByBBCandidateIdV2",
                "query": """query queryApplicationsByBBCandidateIdV2($locale: String!, $bbCandidateId: String!) {
                    queryApplicationsByBBCandidateIdV2(locale: $locale, bbCandidateId: $bbCandidateId) {
                        applications {
                            active
                            applicationId
                            applicationState
                            step
                            subStep
                            continueApplicationLink
                            jobDetail {
                                jobId
                                jobTitle
                                city
                                state
                                postalCode
                                totalPayRateMax
                            }
                            sfShift {
                                shiftCode
                                hoursPerWeek
                                startTime
                                endTime
                                firstDayOnSite
                            }
                        }
                    }
                }""",
                "variables": {
                    "locale": "en-GB",
                    "bbCandidateId": auth.get("hvhcid", "")
                }
            }

            async with session.post(
                "https://www.jobsatamazon.co.uk/graphql",
                json=query_apps,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    apps = (data.get("data", {})
                               .get("queryApplicationsByBBCandidateIdV2", {})
                               .get("applications", []))
                    active = [a for a in apps if a.get("active")]
                    log.info(f"✅ Got {len(active)} active applications")
                    return active, headers, cookie_str
    except Exception as e:
        log.error(f"API error: {e}")
    return [], {}, ""

async def api_submit_job(job, cookies, auth, cid):
    """Submit application directly via Amazon's REST API — no browser needed"""
    try:
        job_id = job.get("id", "")
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        today = datetime.now().strftime("%d/%m/%Y")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "cookie": cookie_str,
            "authorization": auth.get("token", ""),
            "bb-ui-version": "bb-ui-v2",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "origin": "https://www.jobsatamazon.co.uk",
            "referer": f"https://www.jobsatamazon.co.uk/application/uk/?jobId={job_id}",
        }

        base = "https://www.jobsatamazon.co.uk/application/api"

        async with aiohttp.ClientSession() as session:

            # ── Step 1: Get CSRF token ─────────────────────────────────────
            async with session.get(
                "https://www.jobsatamazon.co.uk/authorize/api/csrf?countryCode=UK",
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                csrf_data = await r.json() if r.status == 200 else {}
                csrf_token = csrf_data.get("token", "")
                if csrf_token:
                    headers["x-csrf-token"] = csrf_token
                    log.info("✅ CSRF token obtained")

            # ── Step 2: Get active applications ───────────────────────────
            active_apps, _, _ = await get_candidate_applications(cookies, auth)

            # Check if application already exists for this job
            app_id = None
            for app in active_apps:
                if app.get("jobDetail", {}).get("jobId") == job_id:
                    app_id = app.get("applicationId")
                    step = app.get("step", "")
                    log.info(f"✅ Found existing application {app_id} at step {step}")

                    # If already at shift selection or beyond — navigate directly
                    continue_link = app.get("continueApplicationLink")
                    if continue_link and "shift" in continue_link.lower():
                        log.info(f"✅ Already at shift stage!")
                        return continue_link, app_id
                    break

            # ── Step 3: Create new application if needed ───────────────────
            if not app_id:
                log.info(f"🆕 Creating new application for {job_id}...")
                async with session.post(
                    f"{base}/candidate-application/application",
                    json={"jobId": job_id, "locale": "en-GB"},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status in [200, 201]:
                        data = await r.json()
                        app_id = data.get("applicationId") or data.get("id")
                        log.info(f"✅ Application created: {app_id}")
                    else:
                        text = await r.text()
                        log.warning(f"⚠️ Create app failed {r.status}: {text[:200]}")
                        return None, None

            if not app_id:
                log.warning("⚠️ No application ID obtained")
                return None, None

            headers["referer"] = f"https://www.jobsatamazon.co.uk/application/uk/?applicationId={app_id}&jobId={job_id}"

            # ── Step 4: Update workflow to job-opportunities ───────────────
            async with session.put(
                f"{base}/candidate-application/update-workflow-step-name",
                json={"applicationId": app_id, "workflowStepName": "job-opportunities"},
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                log.info(f"✅ Workflow → job-opportunities ({r.status})")

            # ── Step 5: Submit shift preferences ──────────────────────────
            async with session.put(
                f"{base}/candidate-application/candidate/shiftPreferences",
                json={
                    "earliestStartDate": today,
                    "preferredDaysToWork": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"],
                    "hoursPerWeek": [{"maximumValue": 40, "minimumValue": 36}],
                    "shiftTimePattern": "Any"
                },
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                log.info(f"✅ Shift preferences submitted ({r.status})")

            # ── Step 6: Update workflow to additional-information ──────────
            async with session.put(
                f"{base}/candidate-application/update-workflow-step-name",
                json={"applicationId": app_id, "workflowStepName": "additional-information"},
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                log.info(f"✅ Workflow → additional-information ({r.status})")

            # ── Step 7: Update workflow to review-submit ───────────────────
            async with session.put(
                f"{base}/candidate-application/update-workflow-step-name",
                json={"applicationId": app_id, "workflowStepName": "review-submit"},
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                log.info(f"✅ Workflow → review-submit ({r.status})")

            # ── Step 8: Submit application ─────────────────────────────────
            async with session.put(
                f"{base}/candidate-application/submit-application",
                json={"applicationId": app_id, "locale": "en-GB"},
                headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                text = await r.text()
                log.info(f"✅ Submit application ({r.status}): {text[:100]}")
                if r.status not in [200, 201, 204]:
                    log.warning(f"⚠️ Submit may have failed: {text[:300]}")

            log.info(f"🎉 Application submitted via API! app_id={app_id}")

            # Return checklist URL for shift selection
            checklist_url = f"https://www.jobsatamazon.co.uk/checklist/{job_id}/{app_id}"
            return checklist_url, app_id

    except Exception as e:
        log.error(f"API submit error: {e}")
    return None, None


# ─── AUTO SUBMIT ─────────────────────────────────────────────────────────────
async def auto_submit_account(job, account, chat_id=None, tier="owner"):
    cid    = chat_id or CHAT_ID
    acc_id = account["id"]

    if is_fresh_job(job):
        await tg_alert(job, "fresh_alert", chat_id=cid)
        return

    log.info(f"🤖 Auto-submitting Acc {acc_id} [{tier}]: {job['location']}")
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

            # Load cookies — AMAZON_COOKIES env var first (freshest)
            amazon_cookies_env = os.environ.get("AMAZON_COOKIES", "")
            if amazon_cookies_env:
                try:
                    env_cookies = json.loads(amazon_cookies_env)
                    # Fix sameSite values Playwright doesn't accept
                    for c in env_cookies:
                        if c.get("sameSite") not in ["Strict", "Lax", "None"]:
                            c["sameSite"] = "Lax"
                    await context.add_cookies(env_cookies)
                    log.info(f"✅ Loaded {len(env_cookies)} cookies from env var")
                except Exception as e:
                    log.warning(f"⚠️ Failed to load env cookies: {e}")
                    saved = load_cookies()
                    if saved:
                        await context.add_cookies(saved)
            elif account["session"]:
                await context.add_cookies(account["session"])

            page = await context.new_page()
            await page.goto(job["link"], wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            content = await page.inner_text("body")

            # ── Detect login wall more accurately ─────────────────────────
            login_keywords = [
                "sign in", "log in", "signin", "login",
                "email address", "enter your email",
                "create account", "amazon sign-in"
            ]
            is_login_wall = any(k in content.lower() for k in login_keywords)
            is_job_page   = any(k in content.lower() for k in [
                "apply", "warehouse", "operative", "shift", "hourly"
            ])

            if is_login_wall and not is_job_page:
                log.warning("🔐 Login wall detected — attempting auto-login with OTP...")
                login_ok = await amazon_login_with_otp(page, cid)
                if not login_ok:
                    log.warning("❌ Login failed — sending manual alert")
                    await tg_alert(job, "ready", chat_id=cid)
                    await browser.close()
                    return
                # Re-read content after login
                await page.wait_for_timeout(3000)
                content = await page.inner_text("body")
                log.info("✅ Logged in — continuing auto-submit...")

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

            if not applied:
                for sel in ["button:has-text('Apply now')","button:has-text('Apply')",
                            "a:has-text('Apply')","[data-test='apply-button']"]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            await page.wait_for_timeout(2500)
                            applied = True
                            break
                    except:
                        pass

            if not applied:
                log.warning("❌ Apply button not found")
                await tg_alert(job, "ready", chat_id=cid)
                await browser.close()
                return

            # ── Try direct API approach first ─────────────────────────────
            browser_cookies = await context.cookies()
            auth = await extract_auth_from_cookies(browser_cookies)

            if auth.get("token") and auth.get("hvhcid"):
                log.info("🚀 Trying direct API approach...")
                continue_link, app_id = await api_submit_job(job, browser_cookies, auth, cid)

                if continue_link:
                    log.info(f"✅ Got continue link — navigating directly!")
                    await page.goto(continue_link, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(5000)
                    shift_selected = True

                    # Click Prepare for your Appointment or Submit
                    for sel in [
                        "button:has-text('Prepare for your Appointment')",
                        "button:has-text('Submit')",
                        "button:has-text('Continue')",
                        "button:has-text('Next')",
                    ]:
                        try:
                            btn = await page.wait_for_selector(sel, timeout=5000)
                            if btn and await btn.is_visible():
                                await btn.click()
                                await page.wait_for_timeout(3000)
                                log.info(f"✅ Clicked: {sel}")
                                break
                        except:
                            pass

                    await tg_alert(job, "prepared", chat_id=cid, account_id=acc_id)
                    await browser.close()
                    return
            try:
                btn = await page.wait_for_selector(
                    "button:has-text('Next')", timeout=6000
                )
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    log.info("✅ Next clicked (Page 2)")
            except:
                pass

            # ── PAGE 3: Eligibility checklist ─────────────────────────────
            for sel in [
                "button:has-text('Start Application')",
                "button:has-text('Start application')",
                "[data-test='start-application']",
            ]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=6000)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(4000)
                        log.info("✅ Start Application clicked")
                        break
                except:
                    pass

            # ── PAGE 4: POPUP — "You have an active job application" ───────
            # Must click Continue to proceed (not Go to dashboard)
            try:
                btn = await page.wait_for_selector(
                    "button:has-text('Continue')", timeout=5000
                )
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    log.info("✅ Active application popup — Continue clicked")
            except:
                pass

            # ── PAGE 4b: Login wall check ──────────────────────────────────
            content = await page.inner_text("body")
            login_keywords = ["sign in", "log in", "signin", "email address",
                              "enter your email", "create account"]
            is_job_page = any(k in content.lower() for k in [
                "apply", "warehouse", "shift", "appointment", "pre-hire", "select this job"
            ])
            if any(k in content.lower() for k in login_keywords) and not is_job_page:
                log.warning("🔐 Login wall — attempting login...")
                login_ok = await amazon_login_with_otp(page, cid)
                if not login_ok:
                    await tg_alert(job, "ready", chat_id=cid)
                    await browser.close()
                    return
                await page.wait_for_timeout(3000)

            # ── PAGE 5: Shift selection — "Select this job" ────────────────
            shift_selected = False
            await page.wait_for_timeout(5000)  # wait for React to render shifts

            try:
                btn = await page.wait_for_selector(
                    "button:has-text('Select this job')", timeout=10000
                )
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(4000)
                    log.info("✅ Select this job clicked!")
                    shift_selected = True
            except:
                log.info("ℹ️ No 'Select this job' button found")

            # ── PAGE 6: "Important Notice" — Accept Offer ──────────────────
            if shift_selected:
                try:
                    btn = await page.wait_for_selector(
                        "button:has-text('Accept Offer')", timeout=8000
                    )
                    if btn and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(4000)
                        log.info("✅ Accept Offer clicked!")
                except:
                    log.info("ℹ️ No Accept Offer button")

            # ── PAGE 7: Pre-hire appointment ───────────────────────────────
            try:
                btn = await page.wait_for_selector(
                    "button:has-text('Prepare for your Appointment')", timeout=8000
                )
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    log.info("✅ Prepare for Appointment clicked!")
            except:
                pass

            # ── HALF BOT HALF HUMAN — works on any server ─────────────────
            final_url = page.url if page.url != "about:blank" else job["link"]
            job["link"] = final_url

            current_content = await page.inner_text("body")

            # Check we actually got past login and reached a meaningful page
            is_still_login = any(w in final_url.lower() for w in [
                "signin", "login", "sign-in"
            ]) or any(w in current_content.lower() for w in [
                "enter your email", "sign in to your account",
                "create account", "forgot your password"
            ])

            is_meaningful_page = (
                not is_still_login and
                any(w in current_content.lower() for w in [
                    "shift", "schedule", "select", "confirm",
                    "pre-hire", "appointment", "checklist"
                ])
            )

            if is_meaningful_page and shift_selected:
                log.info(f"✅ Shift actually selected — notifying user")
                await tg_alert(job, "prepared", chat_id=cid, account_id=acc_id)
            elif is_meaningful_page:
                log.info(f"✅ Bot past login but no shift selected — sending ready")
                await tg_alert(job, "ready", chat_id=cid)
            else:
                log.warning(f"⚠️ Bot stuck at login: {final_url}")
                await tg_alert(job, "ready", chat_id=cid)

            # Save cookies for next time
            fresh_cookies = await context.cookies()
            if fresh_cookies:
                save_cookies(fresh_cookies)
                account["session"] = fresh_cookies

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

        # Score the job
        job_score, skip = score_job(job)
        if skip:
            continue

        # ── OWNER ONLY MODE — subscribers paused ─────────────────────────
        # Only process owner (CHAT_ID) for now
        owner_prefs = subscribers.get(CHAT_ID, {})

        # Distance for info only
        job_postcode  = job.get("postcode", "")
        best_distance = None
        if job_postcode:
            for loc in owner_prefs.get("locations", ["Birmingham"]):
                d = await job_distance_miles(job_postcode, loc)
                if d is not None:
                    if best_distance is None or d < best_distance:
                        best_distance = d

        # Alert owner
        await send_all_shifts(job, "new", chat_id=CHAT_ID,
                              distance=best_distance,
                              score=job_score if job_score > 0 else None)

        # Auto-submit ALL jobs — no radius filter, owner decides after
        if ACCOUNTS and not is_fresh_job(job):
            log.info(f"🤖 Auto-submitting for owner: {job['location']}")
            asyncio.create_task(
                auto_submit_account(job, ACCOUNTS[0], chat_id=CHAT_ID, tier="owner")
            )

    if new_count == 0:
        log.info(f"👑 No new jobs — {len(known_jobs)} tracked")
    return new_count

# ─── AUTO NAVIGATE ───────────────────────────────────────────────────────────
async def navigate_job(job, chat_id=None):
    cid = chat_id or CHAT_ID
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
            await page.goto(job["link"], wait_until="domcontentloaded", timeout=60000)
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
            today = [j for j in job_history
                     if j.get("found_at","")[:10] == now.strftime("%Y-%m-%d")]
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

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def handle_updates():
    offset = 0
    processed = set()
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout=10"
                ) as r:
                    data = await r.json()
                    for update in data.get("result",[]):
                        uid = update["update_id"]
                        offset = uid + 1
                        if uid not in processed:
                            processed.add(uid)
                            # Keep set small
                            if len(processed) > 1000:
                                processed.clear()
                            await process_update(update)
        except Exception as e:
            log.error(f"Update error: {e}")
        await asyncio.sleep(2)

async def process_update(update):
    global bot_paused

    if "callback_query" in update:
        cb = update["callback_query"]
        d  = cb.get("data","")
        if d.startswith("applied_"):
            await tg_send("✅ Marked as submitted! Good luck! 💪🔥")
        elif d.startswith("skip_"):
            await tg_send("❌ Job ignored.")
        return

    msg     = update.get("message",{})
    text    = msg.get("text","").strip()
    cid     = str(msg.get("chat",{}).get("id", CHAT_ID))
    name    = msg.get("chat",{}).get("first_name","Friend")
    text_lw = text.lower()

    # OTP handler
    if cid in otp_waiting and text and text.isdigit():
        otp_codes[cid] = text
        otp_waiting[cid].set()
        await tg_send("✅ OTP received! Submitting...", chat_id=cid)
        return

    if cid in onboarding:
        await handle_onboarding(cid, text)
        return

    if text_lw == "/start":
        if cid in subscribers and subscribers[cid].get("setup_complete"):
            sub   = subscribers[cid]
            locs  = ", ".join(sub.get("locations",[]))
            tier  = get_tier(cid, sub)
            auto  = "✅ Full auto-submit" if tier in ("owner","premium") else \
                    "📋 Prepare & notify" if tier == "standard" else \
                    "🔔 Alerts only"
            await tg_send(f"""👋 <b>Welcome back {name}!</b>

📍 {locs}
🚗 {sub.get('radius',30)} miles
📋 {sub.get('job_type','both')}
🤖 {auto}

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
        jlabel = {"fulltime":"Full-time only","parttime":"Part-time only",
                  "both":"Full-time & Part-time"}.get(jt,"Both")
        ltext  = "\n".join([f"📍 {'⭐ ' if i==0 else ''}{l}" for i, l in enumerate(locs)])
        tier   = get_tier(cid, sub)
        auto   = "✅ Full auto-submit" if tier in ("owner","premium") else \
                 "📋 Prepare & notify" if tier == "standard" else \
                 "🔔 Alerts only"
        await tg_send(f"""📋 <b>Your Preferences</b>
━━━━━━━━━━━━━━━━━
{ltext}
🚗 Radius: {sub.get('radius',30)} miles
📋 Job type: {jlabel}
🤖 {auto}
━━━━━━━━━━━━━━━━━
Use /setup to update""", chat_id=cid)

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
            tier = get_tier(scid, sub)
            txt += f"• {sub.get('name','?')} | {locs} | {sub.get('radius',30)}mi | {tier}\n"
        await tg_send(txt, chat_id=cid)

    elif text_lw == "/scrape":
        await tg_send("🔍 <b>Scanning ALL UK Amazon jobs...</b>", chat_id=cid)
        count = await check_jobs()
        await tg_send(
            f"✅ New: {count} | Tracked: {len(known_jobs)}\n"
            f"{'🎉 New jobs found!' if count > 0 else '⏳ No new jobs this scan'}",
            chat_id=cid
        )

    elif text_lw == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet — send /scrape to scan!", chat_id=cid)
        else:
            txt = f"📋 <b>Last {min(5,len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                night = "🌙" if is_night_shift(job.get("schedule","")) else "☀️"
                sched = job.get("schedule") or "See listing"
                day   = job.get("firstDay") or "See listing"
                txt  += f"{night} {job.get('location')}\n💰 £{job.get('pay')}/hr | {job.get('contract')}\n📅 {day} | {sched[:30]}\n\n"
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
        test_job = {
            "id": "TEST-001", "title": "Warehouse Operative",
            "location": "Weybridge, England (West Surrey) KT13 0YU",
            "postcode": "KT13 0YU", "pay": 15.30, "pay_display": "15.30",
            "contract": "Seasonal | Full-time",
            "firstDay": "2026-05-10",
            "shifts": [
                "Sat, Sun, Mon, Tue 23:45 - 10:15",
                "Fri, Sat, Sun, Mon, Tue 6:30 - 13:00",
                "Fri, Sat, Sun, Mon 23:45 - 10:15",
            ],
            "schedule": "Sat, Sun, Mon, Tue 23:45 - 10:15",
            "hours": "40",
            "description": "Pick, pack and ship parcels at our fulfilment centre.",
            "link": "https://www.jobsatamazon.co.uk",
        }
        await send_all_shifts(test_job, "new", chat_id=cid, distance=47.0, score=15)

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
        await tg_send("""👑 <b>Amazon KING BOT v16</b>
━━━━━━━━━━━━━━━━━
/start          — Welcome & setup
/setup          — Update preferences
/mypreferences  — View settings
/status         — Bot status
/scrape         — Scan now
/jobs           — Recent jobs
/history        — All time stats
/test           — Test alert (3 shifts)
/subscribers    — All users (admin)
/clearcache     — Reset job cache (admin)
/pause          — Pause bot (admin)
/resume         — Resume bot (admin)
━━━━━━━━━━━━━━━━━
🔗 Share: t.me/Jibhub_bot
🆓 Free = alerts only
💰 Standard = prepared application
👑 Premium = full auto-submit
🌿 Fresh = alert only""", chat_id=cid)

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("👑 Amazon KING BOT v16 Starting!")
    log.info(f"🌐 Proxy: {'Decodo ✅' if get_proxy_url() else '❌'}")
    log.info(f"👥 Subscribers: {len(subscribers)} | 🤖 Accounts: {len(ACCOUNTS)}")
    log.info(f"📧 Login: {'✅' if AMAZON_EMAIL and AMAZON_PIN else '❌'}")

    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())

    await asyncio.sleep(2)
    await tg_send(f"""👑 <b>Amazon KING BOT v16 ONLINE!</b>
━━━━━━━━━━━━━━━━━
✅ One search → ALL UK jobs
✅ Subscriber radius filtering
✅ 3 tier system (Free/Standard/Premium)
✅ Full shift details
✅ 36hr+ filter (no part-time)
✅ OTP auto-login
✅ Decodo UK proxy
⚡ 3s peak / 10s normal
━━━━━━━━━━━━━━━━━
🌐 Proxy: {'✅ Decodo UK' if get_proxy_url() else '❌'}
📧 Login: {'✅ Ready' if AMAZON_EMAIL and AMAZON_PIN else '❌'}
👥 {len(subscribers)} subscriber(s) | 🤖 {len(ACCOUNTS)} account(s)
━━━━━━━━━━━━━━━━━
Send /test to preview!
Share: t.me/Jibhub_bot""")

    await check_jobs()

    while True:
        await asyncio.sleep(3 if is_peak_time() else 10)
        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
