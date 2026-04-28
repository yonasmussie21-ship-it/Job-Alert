import asyncio
import os
import json
import logging
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
import aiohttp
import pytz
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
CHAT_ID          = os.environ.get("CHAT_ID", "1027065157")
BRIGHT_DATA_USER = os.environ.get("BRIGHT_DATA_USER", "")
BRIGHT_DATA_PASS = os.environ.get("BRIGHT_DATA_PASS", "")
BRIGHT_DATA_HOST = "brd.superproxy.io:33335"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs   = {}
active_jobs  = {}
bot_paused   = False
awaiting_location = False
awaiting_loc_num  = 0  # which location slot we're filling

# Three location priorities
user_locations = {
    1: {"city": "", "lat": 0.0, "lng": 0.0},
    2: {"city": "", "lat": 0.0, "lng": 0.0},
    3: {"city": "", "lat": 0.0, "lng": 0.0},
}

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

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
    loc_num  = job.get("loc_priority", 0)
    dist     = job.get("distance_miles", 999)
    score    = job.get("score", 0)
    expiry   = job.get("expiry")
    mins     = int((expiry - datetime.utcnow()).total_seconds() / 60) if expiry else 120
    city_name = user_locations.get(loc_num, {}).get("city", "your area") if loc_num else "UK"

    if loc_num == 1:
        urgency = "🔴 1ST CHOICE — URGENT!"
    elif loc_num == 2:
        urgency = "🟡 2ND CHOICE — ACT FAST!"
    elif loc_num == 3:
        urgency = "🟢 3RD CHOICE — AVAILABLE!"
    else:
        urgency = "📋 NEW JOB FOUND!"

    if status == "reminder":
        urgency = f"⚠️ REMINDER — {mins} mins left!"
    elif status == "final":
        urgency = f"🚨 FINAL WARNING — {mins} mins left!"
    elif status == "submitted":
        urgency = "✅ APPLICATION SUBMITTED!"

    text = f"""🚨 <b>{urgency}</b>
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
💰 <b>£{job.get('pay', '?')}/hr</b>
⏱️ {job.get('contract', '?')} · {job.get('hours', '?')}hrs/week
📅 Starts: {job.get('firstDay', 'TBC')}
🕘 Shift: {job.get('schedule', 'TBC')}
⭐ Score: <b>{score}/100</b>
📏 <b>{dist} miles</b> from {city_name}
⏳ <b>{mins} mins remaining</b>
━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Bot navigating application...</b>
━━━━━━━━━━━━━━━━━━━━━"""

    markup = {
        "inline_keyboard": [
            [{"text": "🚀 OPEN APPLICATION", "url": job.get("link", "https://www.jobsatamazon.co.uk")}],
            [
                {"text": "✅ APPLIED", "callback_data": f"applied_{job['id']}"},
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
    slot_names = {1: "1ST CHOICE 🔴", 2: "2ND CHOICE 🟡", 3: "3RD CHOICE 🟢"}
    await tg_send(f"""📍 <b>Enter your {slot_names[slot_num]} location:</b>

Type a UK city or postcode:
• Birmingham
• Coventry  
• B1 1BB
• Leicester""")

async def save_location(slot_num, city_input):
    global awaiting_location, awaiting_loc_num
    try:
        geolocator = Nominatim(user_agent="amazon-shift-holder")
        location   = geolocator.geocode(f"{city_input}, UK")
        if location:
            user_locations[slot_num] = {
                "city": city_input.title(),
                "lat":  location.latitude,
                "lng":  location.longitude
            }
            awaiting_location = False

            # Check if we need more locations
            if slot_num < 3:
                filled = sum(1 for l in user_locations.values() if l["city"])
                await tg_send(f"✅ <b>{city_input.title()}</b> saved as choice {slot_num}!")
                await ask_location(slot_num + 1)
            else:
                awaiting_location = False
                locs = "\n".join([
                    f"{'🔴' if i==1 else '🟡' if i==2 else '🟢'} Choice {i}: <b>{user_locations[i]['city']}</b>"
                    for i in range(1,4) if user_locations[i]['city']
                ])
                await tg_send(f"""✅ <b>All locations saved!</b>
━━━━━━━━━━━━━━━━━━━
{locs}
━━━━━━━━━━━━━━━━━━━
🚀 <b>Bot is now watching ALL UK jobs!</b>
Alerting for your 3 priority areas!""")
        else:
            await tg_send(f"❌ Couldn't find <b>{city_input}</b>. Please try again!")
    except Exception as e:
        log.error(f"Geocode error: {e}")
        user_locations[slot_num] = {"city": city_input.title(), "lat": 52.4862, "lng": -1.8904}
        awaiting_location = False
        await tg_send(f"✅ <b>{city_input.title()}</b> saved!")
        if slot_num < 3:
            await ask_location(slot_num + 1)

# ─── DISTANCE & PRIORITY ─────────────────────────────────────────────────────
def get_location_priority(job_lat, job_lng):
    """Returns (priority_number, distance_miles) for closest user location"""
    best_priority = 0
    best_distance = 9999
    job_coords = (job_lat, job_lng)

    for num in range(1, 4):
        loc = user_locations[num]
        if not loc["city"]:
            continue
        try:
            user_coords = (loc["lat"], loc["lng"])
            dist = round(geodesic(user_coords, job_coords).miles, 1)
            if dist < best_distance:
                best_distance = dist
                best_priority = num
        except:
            continue

    return best_priority, best_distance

# ─── JOB SCORING ─────────────────────────────────────────────────────────────
def score_job(job):
    score    = 0
    pay      = job.get("pay", 0)
    hours    = job.get("hours", 0)
    contract = job.get("contract", "").lower()
    distance = job.get("distance_miles", 999)

    if pay >= 15.30:   score += 40
    elif pay >= 14.30: score += 30
    elif pay >= 13.00: score += 20
    else:              score += 10

    if hours >= 40:   score += 25
    elif hours >= 30: score += 18
    elif hours >= 20: score += 10

    if "full" in contract:    score += 20
    elif "reduced" in contract: score += 12
    elif "part" in contract:  score += 8

    if distance < 20:    score += 15
    elif distance < 50:  score += 10
    elif distance < 100: score += 5
    else:                score += 2

    return min(score, 100)

# ─── SCRAPER ─────────────────────────────────────────────────────────────────
AMAZON_SEARCH_URL = "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&category=warehouse&jobType=all"

async def scrape_jobs():
    jobs = []
    try:
        proxy = None
        if BRIGHT_DATA_USER and BRIGHT_DATA_PASS:
            proxy = {
                "server":   f"http://{BRIGHT_DATA_HOST}",
                "username": BRIGHT_DATA_USER,
                "password": BRIGHT_DATA_PASS
            }

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            ctx_args = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "viewport": {"width": 1280, "height": 800}
            }
            if proxy:
                ctx_args["proxy"] = proxy

            context = await browser.new_context(**ctx_args)
            page    = await context.new_page()

            # Intercept API calls
            api_jobs = []
            async def handle_response(response):
                try:
                    if response.status == 200 and any(x in response.url for x in ["job", "search", "posting"]):
                        ct = response.headers.get("content-type", "")
                        if "json" in ct:
                            data = await response.json()
                            parsed = parse_api_response(data)
                            api_jobs.extend(parsed)
                except:
                    pass
            page.on("response", handle_response)

            await page.goto(AMAZON_SEARCH_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(5000)

            if api_jobs:
                jobs = api_jobs
            else:
                jobs = await fallback_scrape()

            await browser.close()
    except Exception as e:
        log.error(f"Scrape error: {e}")
        jobs = await fallback_scrape()

    # Filter ONLY Warehouse Operative
    warehouse_jobs = [j for j in jobs if is_warehouse_operative(j)]
    log.info(f"Found {len(jobs)} total jobs, {len(warehouse_jobs)} Warehouse Operative")
    return warehouse_jobs

def is_warehouse_operative(job):
    """Only return true for Warehouse Operative roles"""
    title = job.get("title", "").lower()
    role  = job.get("role", "").lower()
    combined = title + " " + role
    
    # Must contain warehouse operative
    if "warehouse operative" in combined:
        return True
    if "warehouse" in combined and "operative" in combined:
        return True
    return False

async def fallback_scrape():
    """Direct API fallback"""
    jobs = []
    urls = [
        "https://hiring.amazon.co.uk/api/v1/search?country=GBR&locale=en-GB&pageSize=100&jobType=warehouse",
        "https://www.jobsatamazon.co.uk/api/jobs?country=GB&pageSize=100",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        "Accept": "application/json"
    }
    async with aiohttp.ClientSession() as session:
        for url in urls:
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        data = await r.json()
                        jobs = parse_api_response(data)
                        if jobs:
                            break
            except:
                continue
    return jobs

def parse_api_response(data):
    jobs = []
    raw_jobs = []

    if isinstance(data, list):
        raw_jobs = data
    elif isinstance(data, dict):
        for key in ["jobs", "data", "results", "jobPostings", "hits", "items"]:
            if key in data:
                raw_jobs = data[key]
                break

    for raw in raw_jobs:
        try:
            # Handle multiple ID formats
            job_id = str(
                raw.get("jobId") or
                raw.get("id") or
                raw.get("requisitionId") or
                raw.get("jobReqId") or
                raw.get("externalJobId") or
                ""
            )
            if not job_id:
                continue

            title    = raw.get("title", raw.get("jobTitle", raw.get("positionTitle", "Warehouse Operative")))
            location = raw.get("city", raw.get("locationName", raw.get("location", {}).get("city", "Unknown")))
            postcode = raw.get("postalCode", raw.get("zipCode", raw.get("location", {}).get("postalCode", "")))
            pay      = float(raw.get("salaryMax", raw.get("maximumPayRate", raw.get("payRateMax", 14.30))))
            contract = raw.get("employmentType", raw.get("scheduleType", raw.get("jobType", "Full-time")))
            hours    = int(raw.get("hoursPerWeek", raw.get("weeklyHours", raw.get("hoursPerWeek", 40))))
            first_day = raw.get("firstDayOnSite", raw.get("startDate", raw.get("targetHireDate", "TBC")))
            schedule = raw.get("shiftCode", raw.get("schedule", raw.get("shiftDescription", "")))
            lat      = float(raw.get("latitude", raw.get("lat", raw.get("location", {}).get("latitude", 52.4862))))
            lng      = float(raw.get("longitude", raw.get("lng", raw.get("location", {}).get("longitude", -1.8904))))

            # Build proper link
            link = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}&locale=en-GB&recommended=1&intcmpid=searchalljobsleft"

            # Get priority based on user locations
            priority, distance = get_location_priority(lat, lng)

            job = {
                "id":           job_id,
                "title":        title,
                "role":         title,
                "location":     f"{location} {postcode}".strip(),
                "pay":          pay,
                "contract":     contract,
                "hours":        hours,
                "firstDay":     first_day,
                "schedule":     schedule,
                "distance_miles": distance,
                "loc_priority": priority,
                "link":         link,
                "found_at":     datetime.utcnow().isoformat(),
                "expiry":       datetime.utcnow() + timedelta(hours=2)
            }
            job["score"] = score_job(job)
            jobs.append(job)
        except Exception as e:
            log.warning(f"Parse error: {e}")
            continue

    return jobs

# ─── AUTOMATION ──────────────────────────────────────────────────────────────
async def automate_application(job):
    log.info(f"🤖 Automating: {job['location']} £{job['pay']}/hr")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Click Apply
            for sel in ["button:has-text('Apply')", "a:has-text('Apply')", ".apply-button", "[data-test='apply-button']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info("✅ Clicked Apply")
                        await page.wait_for_timeout(2000)
                        break
                except: pass

            # Click Next
            for sel in ["button:has-text('Next')", "[data-test='next-button']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info("✅ Clicked Next")
                        await page.wait_for_timeout(2000)
                        break
                except: pass

            # Click Start Application
            for sel in ["button:has-text('Start Application')", "[data-test='start-application']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info("✅ Clicked Start Application")
                        await page.wait_for_timeout(2000)
                        break
                except: pass

            current_url = page.url
            log.info(f"✅ Reached: {current_url}")

            # Notify user
            await tg_send(f"""✅ <b>Application Ready!</b>
━━━━━━━━━━━━━━━━━━━
📍 {job['location']}
💰 £{job['pay']}/hr
━━━━━━━━━━━━━━━━━━━
👆 <b>YOUR TURN — Fill details & SUBMIT!</b>
⏳ {int((job['expiry'] - datetime.utcnow()).total_seconds() / 60)} mins remaining""",
            {"inline_keyboard": [[
                {"text": "🚀 OPEN & SUBMIT", "url": job["link"]}
            ]]})

            await browser.close()
    except Exception as e:
        log.error(f"Automation error: {e}")
        await tg_send(f"""⚠️ <b>Apply manually!</b>
📍 {job.get('location')}
💰 £{job.get('pay')}/hr
<a href="{job.get('link')}">TAP TO APPLY →</a>""")

# ─── MAIN CHECK LOOP ─────────────────────────────────────────────────────────
async def check_for_new_jobs():
    global known_jobs, active_jobs
    if bot_paused:
        return

    # Need at least one location set
    has_location = any(l["city"] for l in user_locations.values())
    if not has_location:
        return

    log.info("🔍 Checking Amazon Warehouse Operative jobs...")
    jobs = await scrape_jobs()

    for job in jobs:
        jid = job["id"]
        if jid not in known_jobs:
            known_jobs[jid] = job
            active_jobs[jid] = job["expiry"]
            log.info(f"🆕 {job['title']} | {job['location']} | £{job['pay']}/hr | Priority:{job['loc_priority']} | {job['distance_miles']}mi")
            await tg_alert(job, "new")
            asyncio.create_task(automate_application(job))

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
                async with s.get(f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout=10") as r:
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
            await tg_send("⏭️ Skipped. Watching for next one... 👀")
        return

    msg  = update.get("message", {})
    text = msg.get("text", "").strip()

    # Handle location input
    if awaiting_location and text and not text.startswith("/"):
        await save_location(awaiting_loc_num, text)
        return

    cmd = text.lower()

    if cmd == "/start":
        await tg_send("""🤖 <b>Welcome to Amazon Shift Holder!</b>
━━━━━━━━━━━━━━━━━━━━━
I watch <b>jobsatamazon.co.uk</b> 24/7
for <b>Warehouse Operative</b> jobs only!

Let's set up your 3 priority locations!
━━━━━━━━━━━━━━━━━━━━━""")
        await ask_location(1)

    elif cmd == "/locations":
        locs = ""
        for i in range(1, 4):
            loc = user_locations[i]
            emoji = "🔴" if i==1 else "🟡" if i==2 else "🟢"
            city = loc["city"] if loc["city"] else "Not set"
            locs += f"{emoji} Choice {i}: <b>{city}</b>\n"
        await tg_send(f"""📍 <b>Your Location Priorities</b>
━━━━━━━━━━━━━━━
{locs}━━━━━━━━━━━━━━━
Use /change1, /change2, /change3 to update""")

    elif cmd == "/change1":
        await ask_location(1)
    elif cmd == "/change2":
        await ask_location(2)
    elif cmd == "/change3":
        await ask_location(3)

    elif cmd == "/status":
        status = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        locs = " | ".join([user_locations[i]["city"] for i in range(1,4) if user_locations[i]["city"]]) or "Not set"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━━━
Status: {status}
Watching: Warehouse Operative only
Locations: {locs}
Jobs found: {len(known_jobs)}
Active: {len(active_jobs)}
Check: every 10 seconds 🔥
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
        await tg_send("▶️ Bot resumed! Watching for Warehouse Operative jobs... 👀")

    elif cmd == "/stats":
        locs = "\n".join([f"{'🔴' if i==1 else '🟡' if i==2 else '🟢'} {user_locations[i]['city']}" for i in range(1,4) if user_locations[i]["city"]])
        await tg_send(f"""📊 <b>Your Stats</b>
━━━━━━━━━━━━━━━━━━━
{locs}
Jobs found: {len(known_jobs)}
Active now: {len(active_jobs)}
Status: {'Paused' if bot_paused else 'Running'}
━━━━━━━━━━━━━━━━━━━
Keep going Yonas! 💪""")

    elif cmd == "/help":
        await tg_send("""🤖 <b>Amazon Shift Holder Commands</b>
━━━━━━━━━━━━━━━━━━━━━
/start      — Setup locations
/locations  — View your 3 cities
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
Watching: <b>Warehouse Operative only</b>
Checking: <b>Every 10 seconds</b> 🔥""")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Amazon Shift Holder v2 Starting...")
    asyncio.create_task(handle_updates())

    has_location = any(l["city"] for l in user_locations.values())
    if not has_location:
        await asyncio.sleep(2)
        await tg_send("""🚀 <b>Amazon Shift Holder ONLINE!</b>
━━━━━━━━━━━━━━━━━━━━━
🏭 Watching: Warehouse Operative ONLY
⚡ Checking: Every 10 seconds
🌍 Coverage: All UK
━━━━━━━━━━━━━━━━━━━━━""")
        await ask_location(1)
    else:
        locs = " | ".join([user_locations[i]["city"] for i in range(1,4) if user_locations[i]["city"]])
        await tg_send(f"""🚀 <b>Amazon Shift Holder ONLINE!</b>
━━━━━━━━━━━━━━━━━━━━━
🏭 Watching: Warehouse Operative only
⚡ Checking: Every 10 seconds
📍 Locations: {locs}
━━━━━━━━━━━━━━━━━━━━━""")

    # ALWAYS check every 10 seconds!
    while True:
        await check_for_new_jobs()
        await check_reminders()
        log.info("💤 Next check in 10s")
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
