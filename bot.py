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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "1027065157")

# ─── MULTI-ACCOUNT CONFIG ────────────────────────────────────────────────────
ACCOUNTS = []
for i in range(1, 6):
    email   = os.environ.get(f"AMAZON_EMAIL_{i}", "")
    pin     = os.environ.get(f"AMAZON_PIN_{i}", "")
    cookies = os.environ.get(f"AMAZON_COOKIES_{i}", "")
    if i == 1:
        email   = email or os.environ.get("AMAZON_EMAIL", "")
        pin     = pin or os.environ.get("AMAZON_PIN", "")
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
session_headers = {}

verification_waiting = {}
verification_codes   = {}

TELEGRAM_API     = f"https://api.telegram.org/bot{BOT_TOKEN}"
SUBSCRIBERS_FILE = "/tmp/subscribers.json"

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
    "norwich": "NR1 1AA", "milton": "MK9 1AA", "basildon": "SS14 1AA",
    "chelmsford": "CM1 1AA", "colchester": "CO1 1AA", "stevenage": "SG1 1AA",
}

# ─── POSTCODE COORDS CACHE ───────────────────────────────────────────────────
# Hardcoded coords for major cities to avoid API calls
CITY_COORDS = {
    "B1 1BB":   (52.4862, -1.8904),   # Birmingham
    "EC1A 1BB": (51.5200, -0.0990),   # London
    "M1 1AE":   (53.4808, -2.2426),   # Manchester
    "LS1 1BA":  (53.7997, -1.5492),   # Leeds
    "G1 1AA":   (55.8642, -4.2518),   # Glasgow
    "L1 1JF":   (53.4084, -2.9916),   # Liverpool
    "S1 1AA":   (53.3811, -1.4701),   # Sheffield
    "BS1 1AA":  (51.4545, -2.5879),   # Bristol
    "NE1 1AA":  (54.9783, -1.6178),   # Newcastle
    "NG1 1AA":  (52.9540, -1.1549),   # Nottingham
    "LE1 1AA":  (52.6369, -1.1398),   # Leicester
    "CV1 1AA":  (52.4068, -1.5197),   # Coventry
    "WV1 1AA":  (52.5852, -2.1297),   # Wolverhampton
    "DE1 1AA":  (52.9225, -1.4746),   # Derby
    "CF10 1AA": (51.4816, -3.1791),   # Cardiff
    "EH1 1AA":  (55.9533, -3.1883),   # Edinburgh
    "BT1 1AA":  (54.5973, -5.9301),   # Belfast
    "SO14 1AA": (50.9097, -1.4044),   # Southampton
    "PO1 1AA":  (50.7989, -1.0919),   # Portsmouth
    "OX1 1AA":  (51.7520, -1.2577),   # Oxford
    "CB1 1AA":  (52.2053, 0.1218),    # Cambridge
    "RG1 1AA":  (51.4543, -0.9781),   # Reading
    "LU1 1AA":  (51.8787, -0.4200),   # Luton
    "NN1 1AA":  (52.2405, -0.9027),   # Northampton
    "MK9 1AA":  (52.0406, -0.7594),   # Milton Keynes
    "WA1 1AA":  (53.3900, -2.5970),   # Warrington
    "HU1 1AA":  (53.7457, -0.3367),   # Hull
    "DN1 1AA":  (53.5228, -1.1286),   # Doncaster
    "WF1 1AA":  (53.6830, -1.4977),   # Wakefield
    "DH1 1AA":  (54.7761, -1.5733),   # Durham
    "SR1 1AA":  (54.9069, -1.3838),   # Sunderland
    "TS1 1AA":  (54.5740, -1.2343),   # Middlesbrough
    "BL1 1AA":  (53.5780, -2.4286),   # Bolton
    "WN1 1AA":  (53.5450, -2.6333),   # Wigan
    "SK1 1AA":  (53.4083, -2.1578),   # Stockport
    "ST1 1AA":  (53.0271, -2.1772),   # Stoke
    "SA1 1AA":  (51.6214, -3.9436),   # Swansea
    "EX1 1AA":  (50.7236, -3.5275),   # Exeter
    "EN1 1AA":  (51.6522, -0.0808),   # Enfield
    "SL1 1AA":  (51.5105, -0.5950),   # Slough
    "WD17 1AA": (51.6565, -0.3903),   # Watford
    "CV21 1AA": (52.3711, -1.2660),   # Rugby
    "LU5 4AA":  (51.8868, -0.5216),   # Dunstable
    "LE67 1AA": (52.7236, -1.3698),   # Coalville
    "L33 1AA":  (53.4597, -2.8480),   # Knowsley
    "RM20 1AA": (51.4833, 0.2667),    # West Thurrock
    "ML1 1AA":  (55.7900, -3.9833),   # Motherwell
    "IG11 1AA": (51.5390, 0.0799),    # Barking
    "RM18 1AA": (51.4617, 0.3590),    # Tilbury
    "EH48 2FB": (55.9069, -3.6427),   # Bathgate
    "GL4 3HR":  (51.8585, -2.2180),   # Gloucester
    "BH15 2AA": (50.7192, -1.9874),   # Poole
    "BH1 1AA":  (50.7209, -1.8795),   # Bournemouth
    "SN1 1AA":  (51.5558, -1.7797),   # Swindon
    "PE1 1AA":  (52.5695, -0.2405),   # Peterborough
    "IP1 1AA":  (52.0567, 1.1482),    # Ipswich
    "NR1 1AA":  (52.6309, 1.2974),    # Norwich
    "SS14 1AA": (51.5790, 0.4553),    # Basildon
    "CM1 1AA":  (51.7356, 0.4685),    # Chelmsford
    "CO1 1AA":  (51.8960, 0.8919),    # Colchester
    "SG1 1AA":  (51.9024, -0.2082),   # Stevenage
    "S40 1AA":  (53.2354, -1.4210),   # Chesterfield
}

def resolve_location(location):
    """Convert city name to postcode."""
    loc = location.lower().strip()
    if loc in CITY_POSTCODES:
        return CITY_POSTCODES[loc]
    for city, postcode in CITY_POSTCODES.items():
        if loc in city or city in loc:
            return postcode
    # If it looks like a postcode already, return it
    return location.upper()

def get_coords_for_postcode(postcode):
    """Get coords from cache first, avoids API calls."""
    clean = postcode.strip().upper()
    # Try exact match
    if clean in CITY_COORDS:
        return CITY_COORDS[clean]
    # Try prefix match (e.g. "B21" matches "B1 1BB" Birmingham area)
    prefix = clean.split()[0] if " " in clean else clean[:3]
    for pc, coords in CITY_COORDS.items():
        if pc.startswith(prefix[0]):  # Same letter prefix = same region
            pass  # Don't guess, fall through to None
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

# Ensure owner is always a subscriber
if CHAT_ID not in subscribers:
    subscribers[CHAT_ID] = {
        "name": "Yonas",
        "locations": [os.environ.get("MY_POSTCODE", "Birmingham")],
        "radius": int(os.environ.get("MY_RADIUS", "50")),
        "job_type": "both",
        "setup_complete": True,
        "auto_apply": True,
        "joined": datetime.utcnow().isoformat(),
    }
    save_subscribers(subscribers)

onboarding = {}

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
I'll find jobs and alert you instantly!

Let's set up your preferences.

<b>Step 1 of 5 — Job Type</b>
What type of jobs do you want?

Reply with:
1️⃣ - Full-time only
2️⃣ - Part-time only
3️⃣ - Both (Full-time and Part-time)""", chat_id=cid)

async def handle_onboarding(cid, text):
    global subscribers
    state = onboarding.get(cid, {})
    step  = state.get("step", "")

    if step == "job_type":
        if text in ["1", "1️⃣"]:
            state["job_type"] = "fulltime"
            job_label = "Full-time only"
        elif text in ["2", "2️⃣"]:
            state["job_type"] = "parttime"
            job_label = "Part-time only"
        elif text in ["3", "3️⃣"]:
            state["job_type"] = "both"
            job_label = "Full-time and Part-time"
        else:
            await tg_send("Please reply with 1, 2 or 3 ☝️", chat_id=cid)
            return
        state["step"] = "location_1"
        onboarding[cid] = state
        await tg_send(f"""✅ Job type: <b>{job_label}</b>

<b>Step 2 of 5 — Primary Location</b>
Enter your <b>first preferred location</b>:
This will be your priority location.

Examples: Birmingham, London, Leeds, Manchester
Or postcode: B1 1BB, LS9 0DZ""", chat_id=cid)

    elif step == "location_1":
        location = text.strip()
        state["locations"] = [location]
        state["step"] = "location_2"
        onboarding[cid] = state
        await tg_send(f"""✅ Location 1: <b>{location}</b> (Priority)

<b>Step 3 of 5 — Second Location (Optional)</b>
Enter your 2nd preferred location.

Or type <b>DONE</b> to skip.""", chat_id=cid)

    elif step == "location_2":
        if text.upper() == "DONE":
            state["step"] = "radius"
            onboarding[cid] = state
            await ask_radius(cid)
        else:
            state["locations"].append(text.strip())
            state["step"] = "location_3"
            onboarding[cid] = state
            await tg_send(f"""✅ Location 2: <b>{text.strip()}</b>

<b>Step 4 of 5 — Third Location (Optional)</b>
Enter your 3rd preferred location.

Or type <b>DONE</b> to skip.""", chat_id=cid)

    elif step == "location_3":
        if text.upper() != "DONE":
            state["locations"].append(text.strip())
        state["step"] = "radius"
        onboarding[cid] = state
        await ask_radius(cid)

    elif step == "radius":
        try:
            radius = int(re.search(r'\d+', text).group())
            state["radius"] = radius
            state["step"]   = "confirm"
            onboarding[cid] = state
            locs      = state.get("locations", [])
            job_type  = state.get("job_type", "both")
            job_label = {"fulltime": "Full-time only", "parttime": "Part-time only", "both": "Full-time & Part-time"}.get(job_type, "Both")
            loc_text  = ""
            for i, loc in enumerate(locs):
                priority = " ⭐ (Priority)" if i == 0 else ""
                loc_text += f"\n📍 Location {i+1}: <b>{loc}</b>{priority}"
            await tg_send(f"""<b>Step 5 of 5 — Confirm Your Preferences</b>
━━━━━━━━━━━━━━━━━
📋 Job type: <b>{job_label}</b>{loc_text}
🚗 Radius: <b>{radius} miles</b>
━━━━━━━━━━━━━━━━━
Reply with:
✅ <b>CONFIRM</b> — Save preferences
🔄 <b>RESTART</b> — Start again""", chat_id=cid)
        except:
            await tg_send("Please enter a number e.g. <b>20</b> or <b>30</b>", chat_id=cid)

    elif step == "confirm":
        if text.upper() == "CONFIRM":
            name = state.get("name", "Friend")
            subscribers[cid] = {
                "name":           name,
                "locations":      state.get("locations", []),
                "radius":         state.get("radius", 30),
                "job_type":       state.get("job_type", "both"),
                "setup_complete": True,
                "auto_apply":     False,
                "joined":         datetime.utcnow().isoformat(),
            }
            save_subscribers(subscribers)
            onboarding.pop(cid, None)
            locs      = subscribers[cid]["locations"]
            loc_lines = "\n".join([f"📍 {'⭐ ' if i==0 else ''}{loc}" for i, loc in enumerate(locs)])
            await tg_send(f"""🎉 <b>You're all set!</b>

Your preferences have been saved:
{loc_lines}
🚗 Within {subscribers[cid]['radius']} miles
📋 {subscribers[cid]['job_type'].replace('fulltime','Full-time').replace('parttime','Part-time').replace('both','Full-time & Part-time')}

🔔 You'll get instant alerts when matching jobs drop!

Use /mypreferences to view or update anytime.
Use /help for all commands.""", chat_id=cid)
        elif text.upper() == "RESTART":
            name = state.get("name", "there")
            await start_onboarding(cid, name)
        else:
            await tg_send("Please reply with <b>CONFIRM</b> or <b>RESTART</b>", chat_id=cid)

async def ask_radius(cid):
    await tg_send(f"""✅ Locations saved!

<b>Step 5 of 5 — Travel Radius</b>
How far can you travel from your location?

Reply with miles:
🚗 <b>10</b> — Very local
🚗 <b>20</b> — Nearby
🚗 <b>30</b> — Reasonable
🚗 <b>50</b> — Wide search""", chat_id=cid)

# ─── DISTANCE CALCULATION ─────────────────────────────────────────────────────
def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a    = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

async def get_postcode_coords_api(postcode):
    """Fetch coords from postcodes.io API as fallback."""
    try:
        clean = postcode.replace(" ", "").upper()
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.postcodes.io/postcodes/{clean}", timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                if data.get("status") == 200:
                    return data["result"]["latitude"], data["result"]["longitude"]
    except:
        pass
    return None, None

async def get_coords(postcode):
    """Get coords: cache first, then API."""
    clean = postcode.strip().upper()
    # Check hardcoded cache
    if clean in CITY_COORDS:
        return CITY_COORDS[clean]
    # Try API
    return await get_postcode_coords_api(clean)

async def job_distance_miles(job_postcode, location):
    """
    FIX: Returns distance or None. 
    If coords unavailable, returns None (not treated as too_far).
    """
    try:
        postcode = resolve_location(location)
        lat1, lon1 = await get_coords(postcode)
        lat2, lon2 = await get_coords(job_postcode)
        if all(x is not None for x in [lat1, lon1, lat2, lon2]):
            return round(haversine_miles(lat1, lon1, lat2, lon2), 1)
    except:
        pass
    return None

# ─── JOB TYPE FILTER ─────────────────────────────────────────────────────────
def job_matches_type(job, job_type):
    if job_type == "both":
        return True
    contract = job.get("contract", "").lower()
    if job_type == "fulltime":
        return "full" in contract
    if job_type == "parttime":
        return "part" in contract or "reduced" in contract
    return True

# ─── SHIFT UTILS ─────────────────────────────────────────────────────────────
def is_night_shift(schedule):
    if not schedule or schedule == "TBC":
        return False
    return any(t in str(schedule) for t in ["18:30","19:00","20:00","21:00","22:00","23:00","23:45","0:00","1:00","2:00","3:00"])

def shift_priority(text):
    if any(t in str(text) for t in ["18:30","19:00","20:00","21:00","22:00","23:00","23:45"]): return 1
    if any(t in str(text) for t in ["14:00","15:00","16:00"]): return 2
    return 3

# ─── ALERT ───────────────────────────────────────────────────────────────────
async def tg_alert(job, status="new", chat_id=None, distance=None, account_id=None):
    cid = chat_id or CHAT_ID

    if status == "new":
        header = "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>"
    elif status == "applying":
        acc    = f" (Account {account_id})" if account_id else ""
        header = f"🤖 <b>BOT AUTO-SUBMITTING{acc}...</b>"
    elif status == "applied":
        acc    = f" (Account {account_id})" if account_id else ""
        header = f"✅ <b>APPLIED FOR YOU{acc}!</b>"
    elif status == "navigating":
        header = "⚡ <b>BOT OPENING APPLICATION...</b>"
    elif status == "ready":
        header = "✅ <b>APPLICATION READY — LOG IN & SUBMIT!</b>"
    else:
        header = "⚠️ <b>OPEN MANUALLY!</b>"

    pay_str  = job.get("pay_display") or f"{job.get('pay', '?'):.2f}"
    dist_str = f"\n📏 Distance: <b>{distance} miles</b>" if distance else ""
    night    = " 🌙 NIGHT SHIFT" if is_night_shift(job.get("schedule", "")) else ""

    # ✅ FIX: Clean job link — no extra params that break it
    job_id   = job.get("id", "")
    job_link = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}" if job_id != "TEST-001" else job.get("link", "https://www.jobsatamazon.co.uk")

    text = f"""{header}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
📦 {job.get('title', 'Warehouse Operative')}{night}
💰 <b>£{pay_str}/hr</b>
📋 {job.get('contract', 'Seasonal')}
📅 First Day: <b>{job.get('firstDay', 'TBC')}</b>
🕘 Schedule: <b>{job.get('schedule', 'TBC')}</b>
🕐 Hours/Week: <b>{job.get('hours', 'TBC')}</b>{dist_str}
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "applied":
        text += "\n🎉 <b>Check your Amazon Jobs dashboard!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "ready":
        text += "\n👆 <b>TAP OPEN APPLICATION → Log in → Submit!</b>\n━━━━━━━━━━━━━━━━━━━━━"

    markup = {
        "inline_keyboard": [
            [{"text": "🚀 OPEN APPLICATION", "url": job_link}],
            [{"text": "✅ APPLIED",  "callback_data": f"applied_{job['id']}"},
             {"text": "⏭️ SKIP",    "callback_data": f"skip_{job['id']}"}]
        ]
    }
    await tg_send(text, markup, chat_id=cid)

# ─── SESSION BUILDER ─────────────────────────────────────────────────────────
async def build_session():
    global session_headers
    log.info("🔑 Building session...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-gpu"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}", lambda route: route.abort())
            page = await context.new_page()
            captured_headers = {}
            async def sniff(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        captured_headers.update(dict(response.request.headers))
                except:
                    pass
            page.on("response", sniff)
            await page.goto("https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR", wait_until="networkidle", timeout=45000)
            await page.wait_for_timeout(4000)
            cookies    = await context.cookies()
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            session_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Language": "en-GB,en;q=0.9",
                "country": "United Kingdom",
                "locale": "en-GB",
                "Origin": "https://www.jobsatamazon.co.uk",
                "Referer": "https://www.jobsatamazon.co.uk/app",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Cookie": cookie_str,
            }
            for key in ["authorization","x-amz-user-agent","x-csrf-token"]:
                if key in captured_headers:
                    session_headers[key] = captured_headers[key]
            await browser.close()
            log.info(f"✅ Session built! {len(cookies)} cookies")
            return True
    except Exception as e:
        log.error(f"Session error: {e}")
        return False

# ─── SCRAPER ─────────────────────────────────────────────────────────────────
async def fetch_jobs():
    """
    FIX: Two-stage scraping.
    Stage 1: Intercept GraphQL response (fast).
    Stage 2: If Stage 1 returns 0, scroll and trigger manually.
    Stage 3: If still 0, inject JS to extract from page state.
    """
    all_jobs = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox","--disable-blink-features=AutomationControlled","--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}", lambda route: route.abort())

            page     = await context.new_page()
            captured = []

            async def handle_response(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        data  = await response.json()
                        cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                        if cards:
                            log.info(f"🎯 Intercepted {len(cards)} jobs!")
                            captured.extend(cards)
                except:
                    pass

            page.on("response", handle_response)

            # Stage 1 — Load page
            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle", timeout=45000
            )
            await page.wait_for_timeout(4000)

            # Stage 2 — Scroll to trigger lazy loading if nothing captured
            if not captured:
                log.info("⚡ Stage 2: Scrolling to trigger GraphQL...")
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(3000)
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(2000)

            # Stage 3 — If still nothing, try clicking/filtering to force a new request
            if not captured:
                log.info("⚡ Stage 3: Trying to trigger search manually...")
                try:
                    # Try clicking any filter or search button
                    for sel in ["button[type='submit']", "[data-test='search-button']", "button:has-text('Search')"]:
                        btn = await page.query_selector(sel)
                        if btn:
                            await btn.click()
                            await page.wait_for_timeout(3000)
                            break
                except:
                    pass

            # Stage 4 — Extract from page JS state as last resort
            if not captured:
                log.info("⚡ Stage 4: Extracting from page JS state...")
                try:
                    js_data = await page.evaluate("""() => {
                        // Try Redux store or window state
                        const keys = Object.keys(window);
                        for (const key of keys) {
                            try {
                                const val = window[key];
                                if (val && typeof val === 'object') {
                                    const str = JSON.stringify(val);
                                    if (str.includes('jobId') && str.includes('postalCode')) {
                                        return str;
                                    }
                                }
                            } catch(e) {}
                        }
                        return null;
                    }""")
                    if js_data:
                        parsed = json.loads(js_data)
                        # Try to extract job cards from nested structure
                        def find_jobs(obj, depth=0):
                            if depth > 10: return []
                            if isinstance(obj, list):
                                if obj and isinstance(obj[0], dict) and "jobId" in obj[0]:
                                    return obj
                                results = []
                                for item in obj:
                                    results.extend(find_jobs(item, depth+1))
                                return results
                            if isinstance(obj, dict):
                                if "jobId" in obj:
                                    return [obj]
                                results = []
                                for v in obj.values():
                                    results.extend(find_jobs(v, depth+1))
                                return results
                            return []
                        found = find_jobs(parsed)
                        if found:
                            log.info(f"✅ Stage 4 found {len(found)} jobs from JS state")
                            captured.extend(found)
                except Exception as e:
                    log.warning(f"Stage 4 error: {e}")

            await browser.close()

            for card in captured:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job

    except Exception as e:
        log.error(f"Scraper error: {e}")

    log.info(f"👑 Total: {len(all_jobs)} jobs found")
    return list(all_jobs.values())

# ─── PARSE CARD ───────────────────────────────────────────────────────────────
def parse_card(card):
    try:
        job_id = str(card.get("jobId", ""))
        if not job_id:
            return None

        title      = card.get("jobTitle", "Warehouse Operative") or "Warehouse Operative"
        city       = card.get("city") or card.get("locationName") or ""
        state      = card.get("state") or "England"
        postcode   = card.get("postalCode") or ""
        geo        = card.get("geoClusterDescription") or ""
        pay        = float(card.get("totalPayRateMax") or card.get("totalPayRateMin") or 0)
        employment = card.get("employmentType") or ""
        job_type   = card.get("jobType") or ""
        contract   = employment if employment and employment.lower() not in ["seasonal","temporary"] else (job_type or employment or "Seasonal")
        hours      = str(int(card.get("hoursPerWeek"))) if card.get("hoursPerWeek") else "TBC"
        first_day  = card.get("firstDayOnSite") or "TBC"
        sched_count= card.get("scheduleCount", 0)
        shift_code = card.get("shiftCode") or ""
        schedule   = shift_code if shift_code else (f"{sched_count} schedule(s)" if sched_count else "TBC")

        skip = ["customer service","vcc","virtual","remote","manager","software","engineer"]
        if any(s in title.lower() for s in skip):
            return None

        parts = []
        if city: parts.append(city)
        if state and state != city: parts.append(state)
        if geo and postcode:    location = f"{', '.join(parts)} ({geo}) {postcode}".strip()
        elif geo:               location = f"{', '.join(parts)} ({geo})".strip()
        elif postcode:          location = f"{', '.join(parts)} {postcode}".strip()
        else:                   location = ", ".join(parts) or "Unknown UK Location"

        # ✅ FIX: Clean link — just jobId, no extra params
        link = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}"

        return {
            "id": job_id, "title": title, "location": location,
            "postcode": postcode, "pay": round(pay, 2), "pay_display": f"{pay:.2f}",
            "contract": contract, "firstDay": first_day, "schedule": schedule,
            "hours": hours, "link": link, "found_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None

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
            for ct in ["Full-time","Part-time","Reduced","Fixed-term"]:
                if ct.lower() in content.lower():
                    job["contract"] = ct
                    break
            await browser.close()
            log.info(f"✅ Details: {job.get('firstDay','?')} | {job.get('schedule','?')[:40]}")
    except Exception as e:
        log.warning(f"Detail fetch error: {e}")
    return job

# ─── AUTO SUBMIT ─────────────────────────────────────────────────────────────
async def auto_submit_account(job, account, chat_id=None):
    cid    = chat_id or CHAT_ID
    acc_id = account["id"]
    log.info(f"🤖 Auto-submitting Account {acc_id}: {job['location']}")
    await tg_alert(job, "applying", chat_id=cid, account_id=acc_id)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox","--disable-setuid-sandbox","--disable-gpu"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB", timezone_id="Europe/London",
            )
            if account["session"]:
                await context.add_cookies(account["session"])
            elif account["cookies"]:
                try:
                    await context.add_cookies(json.loads(account["cookies"]))
                except:
                    pass

            page = await context.new_page()
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            applied = False
            for sel in ["button:has-text('Apply')","a:has-text('Apply')","[data-test='apply-button']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        applied = True
                        break
                except:
                    pass

            if not applied:
                await tg_alert(job, "ready", chat_id=cid)
                await browser.close()
                return

            try:
                btn = await page.wait_for_selector("button:has-text('Continue')", timeout=3000)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(2000)
            except:
                pass

            for sel in ["button:has-text('Start Application')","[data-test='start-application']"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=5000)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(3000)
                        break
                except:
                    pass

            try:
                await page.wait_for_selector("button:has-text('Select this job')", timeout=8000)
                shift_buttons = await page.query_selector_all("button:has-text('Select this job')")
                if shift_buttons:
                    best_btn = shift_buttons[0]
                    best_pri = 999
                    cards = await page.query_selector_all("[class*='shift'],[class*='card']")
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
            except:
                pass

            try:
                btn = await page.wait_for_selector("button:has-text('Accept Offer')", timeout=5000)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(3000)
            except:
                pass

            content = await page.inner_text("body")
            if "thank you" in content.lower() or "applied" in content.lower() or "checklist" in page.url:
                log.info(f"🎉 Account {acc_id} applied for {job['location']}!")
                await tg_alert(job, "applied", chat_id=cid, account_id=acc_id)
                account["session"] = await context.cookies()
            else:
                job["link"] = page.url if page.url != "about:blank" else job["link"]
                await tg_alert(job, "ready", chat_id=cid)

            await browser.close()
    except Exception as e:
        log.error(f"Auto-submit error Account {acc_id}: {e}")
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
        if jid not in known_jobs:
            new_count += 1
            job_history.append(job)
            posting_times[job["location"][:20]].append(datetime.utcnow().hour)
            log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr")

            job = await fetch_job_details(job)
            known_jobs[jid] = job

            # Alert each subscriber
            for sub_cid, prefs in subscribers.items():
                if not prefs.get("setup_complete"):
                    continue
                if not job_matches_type(job, prefs.get("job_type", "both")):
                    continue

                job_postcode  = job.get("postcode", "")
                locations     = prefs.get("locations", [])
                radius        = prefs.get("radius", 50)
                too_far       = False  # ✅ FIX: Default to FALSE (send alert unless proven too far)
                best_distance = None

                if locations and job_postcode:
                    distances = []
                    for location in locations:
                        d = await job_distance_miles(job_postcode, location)
                        if d is not None:
                            distances.append(d)
                            if best_distance is None or d < best_distance:
                                best_distance = d

                    # ✅ FIX: Only block if we got actual distance data AND it's too far
                    if distances:
                        too_far = min(distances) > radius

                    # If no distances could be calculated (API failed), send anyway
                    # too_far stays False

                if too_far:
                    log.info(f"📍 Subscriber {sub_cid}: job too far ({best_distance}mi from {locations})")
                    continue

                await tg_alert(job, "new", chat_id=sub_cid, distance=best_distance)

                if prefs.get("auto_apply") and ACCOUNTS:
                    asyncio.create_task(auto_submit_account(job, ACCOUNTS[0], chat_id=sub_cid))
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
            for sel in ["button:has-text('Start Application')"]:
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
            today = [j for j in job_history if j.get("found_at", "")[:10] == now.strftime("%Y-%m-%d")]
            if today:
                best    = max(today, key=lambda x: x.get("pay", 0))
                avg_pay = sum(j.get("pay", 0) for j in today) / len(today)
                nights  = sum(1 for j in today if is_night_shift(j.get("schedule", "")))
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
        await asyncio.sleep(6 * 60 * 60)
        await build_session()

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def handle_updates():
    offset = 0
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout=10") as r:
                    data = await r.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        await process_update(update)
        except Exception as e:
            log.error(f"Update error: {e}")
        await asyncio.sleep(2)

async def process_update(update):
    global bot_paused

    if "callback_query" in update:
        cb = update["callback_query"]
        d  = cb.get("data", "")
        if d.startswith("applied_"): await tg_send("✅ Applied! Good luck! 💪🔥")
        elif d.startswith("skip_"):  await tg_send("⏭️ Skipped!")
        return

    msg     = update.get("message", {})
    text    = msg.get("text", "").strip()
    cid     = str(msg.get("chat", {}).get("id", CHAT_ID))
    name    = msg.get("chat", {}).get("first_name", "Friend")
    text_lw = text.lower()

    if text_lw.startswith("/verify_"):
        try:
            parts  = text.split(" ")
            acc_id = int(parts[0].replace("/verify_", ""))
            code   = parts[1] if len(parts) > 1 else ""
            if code and acc_id in verification_waiting:
                verification_codes[acc_id] = code
                verification_waiting[acc_id].set()
                await tg_send(f"✅ Code received for Account {acc_id}!", chat_id=cid)
        except:
            pass
        return

    if cid in onboarding:
        await handle_onboarding(cid, text)
        return

    if text_lw == "/start":
        if cid in subscribers and subscribers[cid].get("setup_complete"):
            sub  = subscribers[cid]
            locs = ", ".join(sub.get("locations", []))
            await tg_send(f"""👋 <b>Welcome back {name}!</b>

Your preferences:
📍 {locs}
🚗 {sub.get('radius', 30)} miles
📋 {sub.get('job_type', 'both')}

Use /help for all commands!""", chat_id=cid)
        else:
            await start_onboarding(cid, name)

    elif text_lw == "/setup":
        await start_onboarding(cid, name)

    elif text_lw == "/mypreferences":
        sub = subscribers.get(cid, {})
        if not sub:
            await tg_send("You haven't set up yet! Send /start", chat_id=cid)
            return
        locs      = sub.get("locations", [])
        job_type  = sub.get("job_type", "both")
        job_label = {"fulltime": "Full-time only", "parttime": "Part-time only", "both": "Full-time & Part-time"}.get(job_type, "Both")
        loc_text  = "\n".join([f"📍 {'⭐ ' if i==0 else ''}{loc}" for i, loc in enumerate(locs)])
        await tg_send(f"""📋 <b>Your Preferences</b>
━━━━━━━━━━━━━━━━━
{loc_text}
🚗 Radius: {sub.get('radius', 30)} miles
📋 Job type: {job_label}
🤖 Auto-submit: {'✅ ON' if sub.get('auto_apply') else '❌ OFF'}
━━━━━━━━━━━━━━━━━
Use /setup to update""", chat_id=cid)

    elif text_lw == "/status":
        now     = datetime.utcnow()
        h, m    = now.hour, now.minute
        am_peak = (h == 10 and m >= 55) or (h == 11 and m <= 25)
        pm_peak = (h == 22 and m >= 55) or (h == 23 and m <= 25)
        speed   = "1s ⚡ ULTRA BEAST" if (am_peak or pm_peak) else "30s 💤 Normal"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {"⏸️ PAUSED" if bot_paused else "✅ RUNNING"}
👥 Subscribers: {len(subscribers)}
🤖 Accounts: {len(ACCOUNTS)}
Jobs tracked: {len(known_jobs)}
History: {len(job_history)}
Speed: {speed}
━━━━━━━━━━━━━━━━━""", chat_id=cid)

    elif text_lw == "/subscribers" and cid == CHAT_ID:
        txt = f"👥 <b>{len(subscribers)} Subscribers:</b>\n━━━━━━━━━━━\n"
        for scid, sub in subscribers.items():
            locs = ", ".join(sub.get("locations", []))
            txt += f"• {sub.get('name','?')} | {locs} | {sub.get('radius',30)}mi\n"
        await tg_send(txt, chat_id=cid)

    elif text_lw == "/scrape":
        await tg_send("🔍 <b>Scanning ALL UK Amazon jobs...</b>", chat_id=cid)
        count = await check_jobs()
        await tg_send(f"✅ New: {count} | Tracked: {len(known_jobs)}\n{'🎉 Jobs found!' if count > 0 else '⏳ No new jobs found this scan!'}", chat_id=cid)

    elif text_lw == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet — send /scrape to scan!", chat_id=cid)
        else:
            txt = f"📋 <b>Last {min(5, len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                night = "🌙" if is_night_shift(job.get("schedule", "")) else "☀️"
                txt  += f"{night} {job.get('location')}\n💰 £{job.get('pay')}/hr | {job.get('contract')}\n📅 {job.get('firstDay','TBC')}\n\n"
            await tg_send(txt, chat_id=cid)

    elif text_lw == "/history":
        if not job_history:
            await tg_send("📭 No history yet!", chat_id=cid)
        else:
            total  = len(job_history)
            avg    = sum(j.get("pay", 0) for j in job_history) / total
            best   = max(job_history, key=lambda x: x.get("pay", 0))
            nights = sum(1 for j in job_history if is_night_shift(j.get("schedule", "")))
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

    elif text_lw == "/help":
        await tg_send("""👑 <b>King Bot v9 Commands</b>
━━━━━━━━━━━━━━━━━
/start          — Welcome & setup
/setup          — Update preferences
/mypreferences  — View your settings
/status         — Bot status
/scrape         — Scan now
/jobs           — Recent jobs
/history        — All time stats
/test           — Test alert
/subscribers    — View all users (admin)
━━━━━━━━━━━━━━━━━
🔗 Share bot: t.me/Jibhub_bot""", chat_id=cid)

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info(f"👑 Amazon KING BOT v9 Starting! {len(subscribers)} subscribers")

    await build_session()

    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())
    asyncio.create_task(session_refresh_loop())

    await asyncio.sleep(2)
    await tg_send(f"""👑 <b>Amazon KING BOT v9 ONLINE!</b>
✅ Subscriber system active
👥 {len(subscribers)} subscriber(s)
🤖 {len(ACCOUNTS)} Amazon account(s)
🌙 Night shift priority
📍 City-based location filter
━━━━━━━━━━━━━━━━━
Share your bot: t.me/Jibhub_bot
Send /scrape to check now!""")

    await check_jobs()

    while True:
        now     = datetime.utcnow()
        h, m    = now.hour, now.minute
        am_peak = (h == 10 and m >= 55) or (h == 11 and m <= 25)
        pm_peak = (h == 22 and m >= 55) or (h == 23 and m <= 25)
        if am_peak or pm_peak:
            await asyncio.sleep(1)
        else:
            await asyncio.sleep(30)
        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
