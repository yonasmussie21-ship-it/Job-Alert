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
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
CHAT_ID     = os.environ.get("CHAT_ID", "1027065157")
DECODO_USER = os.environ.get("DECODO_USER", "")
DECODO_PASS = os.environ.get("DECODO_PASS", "")
DECODO_HOST = os.environ.get("DECODO_HOST", "gb.decodo.com")
DECODO_PORT = os.environ.get("DECODO_PORT", "30004")
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
otp_waiting   = {}  # chat_id -> asyncio.Event
otp_codes     = {}  # chat_id -> code string

TELEGRAM_API     = f"https://api.telegram.org/bot{BOT_TOKEN}"
SUBSCRIBERS_FILE = "/tmp/subscribers.json"
COOKIES_FILE     = "/tmp/amazon_session.json"

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
        # URL-encode password to handle special characters like +
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
                "name": state.get("name", "Friend"),
                "locations": state.get("locations", []),
                "radius": state.get("radius", 30),
                "job_type": state.get("job_type", "both"),
                "setup_complete": True, "auto_apply": False,
                "joined": datetime.utcnow().isoformat(),
            }
            save_subscribers(subscribers)
            onboarding.pop(cid, None)
            await tg_send("🎉 <b>You're all set!</b> Instant alerts when jobs drop!\n\nUse /help for all commands.", chat_id=cid)
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
        hours       = str(int(card.get("hoursPerWeek"))) if card.get("hoursPerWeek") else None
        first_day   = card.get("firstDayOnSite") or None
        sched_count = card.get("scheduleCount", 0)
        shift_code  = card.get("shiftCode") or ""
        schedule    = shift_code if shift_code else None

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
            "hours": hours, "sched_count": sched_count,
            "link": f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}",
            "found_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None

# ─── FETCH FULL JOB DETAILS (v15 — full shift scraping) ──────────────────────
async def fetch_job_details(job):
    """
    Scrape the full job page to get:
    - Real shift schedules (not TBC)
    - First day
    - Hours per week
    - Job description
    - Multiple shifts
    """
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

            # Intercept GraphQL for shift data
            shifts_data = []
            async def handle_response(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        data = await response.json()
                        # Look for job details with shifts
                        job_detail = (data.get("data", {})
                                         .get("getJobDetailByJobId", {}))
                        if job_detail:
                            shifts = job_detail.get("jobCardDetail", {}).get("scheduleDetails", [])
                            if shifts:
                                shifts_data.extend(shifts)
                                log.info(f"✅ Got {len(shifts)} shifts from GraphQL")
                except:
                    pass

            page.on("response", handle_response)
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(4000)
            content = await page.inner_text("body")

            # ── Extract First Day ──────────────────────────────────────────
            patterns = [
                r'(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+,?\s+\d+\s+[A-Za-z]+\s+\d{4})',
                r'(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+ \d+, \d{4})',
                r'(\d{4}-\d{2}-\d{2})',
            ]
            for pattern in patterns:
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    job["firstDay"] = m.group(1).strip()
                    break

            # ── Extract Job Description ────────────────────────────────────
            desc_match = re.search(
                r'(?:Pick, pack|Sort|Process|Receive|Load|Unload|Pack|Ship)[^.]+\.',
                content, re.IGNORECASE
            )
            if desc_match:
                job["description"] = desc_match.group(0).strip()

            # ── Extract Shifts from page content ──────────────────────────
            # Look for shift timing patterns like "Mon, Tue, Wed 08:00 - 18:30"
            shift_patterns = re.findall(
                r'([A-Za-z]{3}(?:,\s*[A-Za-z]{3})*\s+\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})',
                content
            )

            if shift_patterns:
                # Get unique shifts
                unique_shifts = list(dict.fromkeys(shift_patterns))
                job["shifts"] = unique_shifts
                job["schedule"] = unique_shifts[0] if unique_shifts else None
                log.info(f"✅ Found {len(unique_shifts)} shift patterns")
            elif shifts_data:
                # Use GraphQL shift data
                job["shifts"] = [s.get("scheduleDisplay", "") for s in shifts_data]
                job["schedule"] = job["shifts"][0] if job["shifts"] else None

            # ── Extract Hours ──────────────────────────────────────────────
            m = re.search(r'(\d+)\s*(?:hrs?|hours?)\s*(?:per\s*week|/week)', content, re.IGNORECASE)
            if m:
                job["hours"] = m.group(1)

            # ── Extract Contract Type ──────────────────────────────────────
            for ct in ["Full-time","Part-time","Reduced","Fixed-term","Seasonal","Permanent","Temporary"]:
                if ct.lower() in content.lower():
                    job["contract"] = ct
                    break

            await browser.close()
            log.info(f"✅ Details: day={job.get('firstDay','?')} shifts={len(job.get('shifts',[]))} hrs={job.get('hours','?')}")

    except Exception as e:
        log.warning(f"Detail fetch error: {e}")
    return job

# ─── CORE SCRAPER — CAPTURE + REPLAY IN ONE SESSION ──────────────────────────
async def fetch_jobs():
    all_jobs = {}
    proxy    = get_proxy_url()

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

            page         = await context.new_page()
            captured     = {}
            direct_cards = []

            async def on_request(request):
                if "/graphql" in request.url and not captured:
                    try:
                        body = request.post_data
                        if body and "searchJobCardsByLocation" in body:
                            headers = dict(request.headers)
                            for h in ["content-length","host",":method",":path",":scheme",":authority"]:
                                headers.pop(h, None)

                            # ── v15 KEY FIX: inject UK location into body ──
                            try:
                                body_json = json.loads(body)
                                search_req = body_json.get("variables", {}).get("searchJobRequest", {})
                                # Force UK-wide search centred on Birmingham
                                search_req["country"]  = "United Kingdom"
                                search_req["keyWords"] = ""
                                search_req["pageSize"] = 100
                                # Add location filter if supported
                                if "geoQueryParam" not in search_req:
                                    search_req["geoQueryParam"] = {
                                        "latitude":  52.4862,
                                        "longitude": -1.8904,
                                        "radius":    500
                                    }
                                modified_body = json.dumps(body_json)
                                captured["url"]     = request.url
                                captured["headers"] = headers
                                captured["body"]    = modified_body
                                log.info(f"✅ Captured + injected UK location!")
                            except:
                                # Fallback — use raw body
                                captured["url"]     = request.url
                                captured["headers"] = headers
                                captured["body"]    = body
                                log.info(f"✅ Captured real GraphQL request!")

                            log.info(f"📡 URL: {request.url}")
                    except Exception as e:
                        log.warning(f"Request capture error: {e}")

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
                wait_until="networkidle", timeout=45000
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

            # ── Replay via Decodo UK proxy ────────────────────────────────
            if captured and proxy:
                log.info("🌐 Replaying via Decodo UK proxy...")
                cards = await replay_via_proxy(
                    url=captured["url"],
                    headers=captured["headers"],
                    body=captured["body"],
                    proxy=proxy
                )
                if cards:
                    log.info(f"🎯 Decodo replay: {len(cards)} jobs!")
                    for card in cards:
                        job = parse_card(card)
                        if job and job["id"] not in all_jobs:
                            all_jobs[job["id"]] = job
                else:
                    log.warning("⚠️ Proxy replay returned 0 — using browser results")
                    for card in direct_cards:
                        job = parse_card(card)
                        if job and job["id"] not in all_jobs:
                            all_jobs[job["id"]] = job
            else:
                for card in direct_cards:
                    job = parse_card(card)
                    if job and job["id"] not in all_jobs:
                        all_jobs[job["id"]] = job

    except Exception as e:
        log.error(f"fetch_jobs error: {e}")

    log.info(f"👑 Total valid warehouse jobs: {len(all_jobs)}")
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
                    log.warning(f"⚠️ Proxy replay status {status}: {text[:300]}")
                    return []

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    log.warning(f"⚠️ Invalid JSON: {text[:300]}")
                    return []

                if "errors" in data:
                    log.warning(f"⚠️ GraphQL errors: {json.dumps(data['errors'])[:300]}")
                    return []

                cards = (data.get("data", {})
                             .get("searchJobCardsByLocation", {})
                             .get("jobCards", []))
                return cards

    except Exception as e:
        log.error(f"Proxy replay error: {e}")
        return []

# ─── ALERT — v15 with full shift details ─────────────────────────────────────
async def tg_alert(job, status="new", chat_id=None, distance=None,
                   account_id=None, shift_index=None, total_shifts=None):
    cid = chat_id or CHAT_ID

    headers_map = {
        "new":         "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>",
        "applying":    f"🤖 <b>AUTO-SUBMITTING{' (Acc '+str(account_id)+')' if account_id else ''}...</b>",
        "applied":     f"✅ <b>APPLIED FOR YOU{' (Acc '+str(account_id)+')' if account_id else ''}!</b>",
        "navigating":  "⚡ <b>BOT OPENING APPLICATION...</b>",
        "ready":       "✅ <b>APPLICATION READY — LOG IN & SUBMIT!</b>",
        "fresh_alert": "🌿 <b>AMAZON FRESH JOB — MANUAL APPLY ONLY</b>",
        "otp_needed":  "🔐 <b>AMAZON OTP REQUIRED</b>",
    }
    header   = headers_map.get(status, "⚠️ <b>OPEN MANUALLY!</b>")
    pay_str  = job.get("pay_display") or f"{job.get('pay','?'):.2f}"
    dist_str = f"\n📏 Distance: <b>{distance} miles</b>" if distance else ""

    # ── Shift info ────────────────────────────────────────────────────────
    shifts   = job.get("shifts", [])
    schedule = job.get("schedule") or (shifts[shift_index] if shifts and shift_index is not None and shift_index < len(shifts) else None)
    night    = " 🌙 NIGHT SHIFT" if is_night_shift(schedule or "") else ""
    fresh    = " 🌿 FRESH" if is_fresh_job(job) else ""

    # Shift counter e.g. "Shift 1 of 3"
    shift_str = ""
    if total_shifts and total_shifts > 1 and shift_index is not None:
        shift_str = f"\n🔄 <b>Shift {shift_index+1} of {total_shifts}</b>"

    # ── Format fields — no TBC ────────────────────────────────────────────
    first_day_str = job.get("firstDay") or "Check listing"
    schedule_str  = schedule or "Check listing"
    hours_str     = job.get("hours") or "Check listing"
    desc_str      = f"\n📝 {job.get('description')}" if job.get("description") else ""

    job_id   = job.get("id","")
    job_link = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}" if job_id != "TEST-001" else job.get("link","https://www.jobsatamazon.co.uk")

    text = f"""{header}{shift_str}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location','Unknown')}</b>
📦 {job.get('title','Warehouse Operative')}{night}{fresh}
💰 <b>£{pay_str}/hr</b>
📋 {job.get('contract','Seasonal')}
📅 First Day: <b>{first_day_str}</b>
🕘 Schedule: <b>{schedule_str}</b>
🕐 Hours/Week: <b>{hours_str}</b>{dist_str}{desc_str}
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "applied":
        text += "\n🎉 <b>Check your Amazon Jobs dashboard!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "ready":
        text += "\n👆 <b>TAP OPEN APPLICATION → Log in → Submit!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "fresh_alert":
        text += "\n🌿 <b>Fresh excluded from auto-submit — apply manually!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "otp_needed":
        text += "\n\n<b>Reply with your OTP code to complete login:</b>"

    markup = {
        "inline_keyboard": [
            [{"text": "🚀 OPEN APPLICATION", "url": job_link}],
            [{"text": "✅ APPLIED", "callback_data": f"applied_{job['id']}"},
             {"text": "⏭️ SKIP",   "callback_data": f"skip_{job['id']}"}]
        ]
    } if status in ["new","ready","fresh_alert"] else None

    await tg_send(text, markup, chat_id=cid)


async def send_all_shifts(job, status="new", chat_id=None, distance=None):
    """Send separate alert for each shift found."""
    shifts = job.get("shifts", [])
    if not shifts or len(shifts) <= 1:
        await tg_alert(job, status, chat_id=chat_id, distance=distance)
        return

    for i, shift in enumerate(shifts):
        shift_job           = dict(job)
        shift_job["schedule"] = shift
        await tg_alert(
            shift_job, status,
            chat_id=chat_id, distance=distance,
            shift_index=i, total_shifts=len(shifts)
        )
        await asyncio.sleep(0.5)

# ─── OTP LOGIN FLOW ───────────────────────────────────────────────────────────
async def amazon_login_with_otp(page, chat_id):
    """Login to Amazon using email/PIN then wait for OTP from user."""
    try:
        log.info("🔐 Starting Amazon login with OTP flow...")

        # Enter email
        email_field = await page.query_selector("input[type='email'], input[name='email']")
        if email_field:
            await email_field.fill(AMAZON_EMAIL)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2000)

        # Enter password/PIN
        pass_field = await page.query_selector("input[type='password'], input[name='password']")
        if pass_field:
            await pass_field.fill(AMAZON_PIN)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)

        # Check if OTP needed
        content = await page.inner_text("body")
        if any(w in content.lower() for w in ["verification", "otp", "one-time", "passcode", "authenticator"]):
            log.info("📱 OTP required — asking user...")

            # Ask user for OTP
            await tg_send(
                "🔐 <b>Amazon needs verification!</b>\n\nCheck your phone/email for the OTP code and reply with it here:",
                chat_id=chat_id
            )

            # Wait for OTP from user (60 second timeout)
            event = asyncio.Event()
            otp_waiting[chat_id] = event
            try:
                await asyncio.wait_for(event.wait(), timeout=120)
                otp = otp_codes.pop(chat_id, None)
                otp_waiting.pop(chat_id, None)

                if otp:
                    otp_field = await page.query_selector("input[type='text'], input[name='otpCode'], input[autocomplete='one-time-code']")
                    if otp_field:
                        await otp_field.fill(otp)
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(3000)
                        log.info("✅ OTP submitted!")
                        return True
            except asyncio.TimeoutError:
                log.warning("⏱️ OTP timeout — user didn't respond")
                await tg_send("⏱️ OTP timeout — please apply manually", chat_id=chat_id)
                return False

        return True

    except Exception as e:
        log.error(f"Login error: {e}")
        return False

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

            # Handle login wall with OTP flow
            if "sign in" in content.lower() or "log in" in content.lower():
                log.warning("🔐 Login wall — attempting auto-login with OTP...")
                login_ok = await amazon_login_with_otp(page, cid)
                if not login_ok:
                    await tg_alert(job, "ready", chat_id=cid)
                    await browser.close()
                    return
                content = await page.inner_text("body")

            applied = False

            # JS injection
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

            for sel in ["button:has-text('Continue')","button:has-text('Next')"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=4000)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except:
                    pass

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

            # Shift selection
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

            try:
                btn = await page.wait_for_selector("button:has-text('Accept Offer')", timeout=5000)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    log.info("✅ Offer accepted")
            except:
                pass

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

            # Send alert for each shift separately
            await send_all_shifts(job, "new", chat_id=sub_cid, distance=best_distance)

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

    # ── OTP handler ───────────────────────────────────────────────────────
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
        jlabel = {"fulltime":"Full-time only","parttime":"Part-time only",
                  "both":"Full-time & Part-time"}.get(jt,"Both")
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
                fresh = "🌿" if is_fresh_job(job) else ""
                sched = job.get("schedule") or "Check listing"
                txt  += f"{night}{fresh} {job.get('location')}\n💰 £{job.get('pay')}/hr | {job.get('contract')}\n📅 {job.get('firstDay') or 'Check listing'} | {sched[:30]}\n\n"
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
            "description": "Pick, pack and ship parcels",
            "link": "https://www.jobsatamazon.co.uk",
        }
        await send_all_shifts(test_job, "new", chat_id=cid, distance=47.0)

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
        await tg_send("""👑 <b>Amazon KING BOT v15</b>
━━━━━━━━━━━━━━━━━
/start          — Welcome & setup
/setup          — Update preferences
/mypreferences  — View settings
/status         — Bot status
/scrape         — Scan now
/jobs           — Recent jobs
/history        — All time stats
/test           — Test alert (3 shifts)
/autoon         — Enable auto-submit
/autooff        — Disable auto-submit
/subscribers    — All users (admin)
/clearcache     — Reset job cache (admin)
/pause          — Pause bot (admin)
/resume         — Resume bot (admin)
━━━━━━━━━━━━━━━━━
🔗 Share: t.me/Jibhub_bot
🌿 Fresh = alert only
🌐 Decodo UK proxy
🔐 Auto OTP login""", chat_id=cid)

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info(f"👑 Amazon KING BOT v15 Starting!")
    log.info(f"🌐 Proxy: {'Decodo configured ✅' if get_proxy_url() else 'No proxy ❌'}")
    log.info(f"👥 Subscribers: {len(subscribers)} | 🤖 Accounts: {len(ACCOUNTS)}")
    log.info(f"📧 Email: {'✅' if AMAZON_EMAIL else '❌'} | PIN: {'✅' if AMAZON_PIN else '❌'}")

    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())

    await asyncio.sleep(2)
    await tg_send(f"""👑 <b>Amazon KING BOT v15 ONLINE!</b>
✅ UK location injection in GraphQL
✅ Full shift details (no more TBC)
✅ Multiple shifts per job
✅ OTP auto-login flow
✅ Decodo UK proxy
✅ Auto-submit ON for owner
✅ 3s peak / 10s normal
🌐 Proxy: {'✅ Decodo UK' if get_proxy_url() else '❌ No proxy'}
📧 Login: {'✅ Ready' if AMAZON_EMAIL and AMAZON_PIN else '❌ No credentials'}
👥 {len(subscribers)} subscriber(s) | 🤖 {len(ACCOUNTS)} account(s)
━━━━━━━━━━━━━━━━━
Send /test to see new alert format!
Share: t.me/Jibhub_bot""")

    await check_jobs()

    while True:
        await asyncio.sleep(3 if is_peak_time() else 10)
        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
