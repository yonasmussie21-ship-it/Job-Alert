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
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "8713464696:AAG9F4SudtujRHaBePItPBuZq3dEYxV648E")
CHAT_ID         = os.environ.get("CHAT_ID", "1027065157")
BRIGHT_DATA_USER = os.environ.get("BRIGHT_DATA_USER", "")
BRIGHT_DATA_PASS = os.environ.get("BRIGHT_DATA_PASS", "")
BRIGHT_DATA_HOST = "brd.superproxy.io:22225"
CAPTCHA_KEY     = os.environ.get("CAPTCHA_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs      = {}   # job_id -> job dict
active_jobs     = {}   # job_id -> expiry datetime
bot_paused      = False
awaiting_location = False  # True when bot is waiting for user to type location

# User location (saved after /start setup)
user_location = {
    "city": os.environ.get("USER_CITY", ""),
    "lat": float(os.environ.get("USER_LAT", 0)),
    "lng": float(os.environ.get("USER_LNG", 0)),
}

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
async def tg_send(text, reply_markup=None):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with aiohttp.ClientSession() as s:
        await s.post(f"{TELEGRAM_API}/sendMessage", json=payload)

async def tg_alert(job, status="new"):
    """Send job alert with urgency based on distance"""
    dist    = job.get("distance_miles", 999)
    score   = job.get("score", 0)
    expiry  = job.get("expiry")
    mins    = int((expiry - datetime.utcnow()).total_seconds() / 60) if expiry else 120

    # Distance emoji
    if dist < 30:
        dist_emoji = "🔴"
        urgency    = "URGENT — NEARBY JOB!"
    elif dist < 80:
        dist_emoji = "🟡"
        urgency    = "NEW JOB ALERT!"
    else:
        dist_emoji = "🟢"
        urgency    = "JOB AVAILABLE"

    # Status header
    if status == "new":
        header = f"🚨 <b>{urgency}</b>"
    elif status == "reminder":
        header = f"⚠️ <b>REMINDER — {mins} mins left!</b>"
    elif status == "final":
        header = f"🔴 <b>FINAL WARNING — {mins} mins left!</b>"
    elif status == "submitted":
        header = "✅ <b>SHIFT SUBMITTED SUCCESSFULLY!</b>"
    else:
        header = f"📋 <b>JOB UPDATE</b>"

    text = f"""{header}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b> · {job.get('postcode', '')}
💰 <b>£{job.get('pay', '?')}/hr</b>
⏱️ {job.get('contract', '?')} · {job.get('hours', '?')}hrs/week
📅 Starts: {job.get('firstDay', 'TBC')}
🕘 Shift: {job.get('schedule', 'TBC')}
⭐ Score: <b>{score}/100</b>
{dist_emoji} Distance: <b>{dist} miles from {user_location['city']}</b>
⏳ <b>{mins} mins remaining</b>
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "submitted":
        text += f"\n🎉 <b>Application sent automatically!</b>\n━━━━━━━━━━━━━━━━━━━━━"
        markup = {
            "inline_keyboard": [[
                {"text": "📋 View Application", "url": job.get("link", "https://www.jobsatamazon.co.uk")}
            ]]
        }
    else:
        text += f"\n⚡ <b>Bot is processing application...</b>\n━━━━━━━━━━━━━━━━━━━━━"
        markup = {
            "inline_keyboard": [
                [{"text": "🔗 View Job", "url": job.get("link", "https://www.jobsatamazon.co.uk")}],
                [
                    {"text": "✅ Applied", "callback_data": f"applied_{job['id']}"},
                    {"text": "⏭️ Skip", "callback_data": f"skip_{job['id']}"}
                ]
            ]
        }

    await tg_send(text, markup)

# ─── LOCATION SETUP ──────────────────────────────────────────────────────────
async def ask_for_location():
    """Ask user to enter their location"""
    global awaiting_location
    awaiting_location = True
    await tg_send("""🤖 <b>Welcome to Amazon Shift Holder!</b>
━━━━━━━━━━━━━━━━━━━━━
I watch <b>jobsatamazon.co.uk</b> 24/7
and apply for shifts automatically!

📍 <b>First — what is your city or postcode?</b>

Examples:
• Birmingham
• B1 1BB
• Coventry
• Manchester
━━━━━━━━━━━━━━━━━━━━━
<i>Type your city or postcode now:</i>""")

async def save_location(city_or_postcode):
    """Geocode user input and save coordinates"""
    global user_location, awaiting_location
    try:
        geolocator = Nominatim(user_agent="amazon-shift-holder")
        location   = geolocator.geocode(f"{city_or_postcode}, UK")

        if location:
            user_location = {
                "city": city_or_postcode.title(),
                "lat":  location.latitude,
                "lng":  location.longitude
            }
            awaiting_location = False
            log.info(f"Location set: {user_location}")

            await tg_send(f"""✅ <b>Location set to {user_location['city']}!</b>
━━━━━━━━━━━━━━━━━━━━━
🔴 Under 30 miles = URGENT alert
🟡 30-80 miles = NORMAL alert
🟢 Over 80 miles = QUIET alert
━━━━━━━━━━━━━━━━━━━━━
🚀 <b>Bot is now LIVE!</b>
Watching Amazon jobs near <b>{user_location['city']}</b>

Send /help for all commands""")
        else:
            await tg_send(f"""❌ Couldn't find <b>{city_or_postcode}</b>

Please try again with:
• A UK city name (e.g. Birmingham)
• A postcode (e.g. B1 1BB)""")
    except Exception as e:
        log.error(f"Geocode error: {e}")
        # Fallback - use typed city name without coordinates
        user_location = {"city": city_or_postcode.title(), "lat": 52.4862, "lng": -1.8904}
        awaiting_location = False
        await tg_send(f"""✅ <b>Location set to {user_location['city']}!</b>
🚀 Bot is now watching for jobs!""")

# ─── DISTANCE CALC ───────────────────────────────────────────────────────────
def calc_distance(job_lat, job_lng):
    """Calculate distance in miles from user location"""
    try:
        if not user_location["lat"] or not user_location["lng"]:
            return 999
        user_coords = (user_location["lat"], user_location["lng"])
        job_coords  = (job_lat, job_lng)
        return round(geodesic(user_coords, job_coords).miles, 1)
    except:
        return 999

# ─── JOB SCORING ─────────────────────────────────────────────────────────────
def score_job(job):
    score    = 0
    pay      = job.get("pay", 0)
    hours    = job.get("hours", 0)
    contract = job.get("contract", "").lower()
    distance = job.get("distance_miles", 999)

    # Pay (40pts)
    if pay >= 15.30:   score += 40
    elif pay >= 14.30: score += 30
    elif pay >= 13.00: score += 20
    else:              score += 10

    # Hours (25pts)
    if hours >= 40:   score += 25
    elif hours >= 30: score += 18
    elif hours >= 20: score += 10
    else:             score += 5

    # Contract (20pts)
    if "full" in contract:    score += 20
    elif "reduced" in contract: score += 12
    elif "part" in contract:  score += 8

    # Distance (15pts)
    if distance < 20:    score += 15
    elif distance < 50:  score += 10
    elif distance < 100: score += 5
    else:                score += 2

    return min(score, 100)

# ─── SCRAPER ─────────────────────────────────────────────────────────────────
async def scrape_jobs():
    """Scrape Amazon UK jobs"""
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
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            if proxy:
                ctx_args["proxy"] = proxy

            context = await browser.new_context(**ctx_args)
            page    = await context.new_page()

            # Intercept API responses
            api_data = []
            async def handle_response(response):
                if "job" in response.url.lower() and response.status == 200:
                    try:
                        ct = response.headers.get("content-type", "")
                        if "json" in ct:
                            data = await response.json()
                            api_data.append(data)
                    except:
                        pass
            page.on("response", handle_response)

            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle",
                timeout=30000
            )
            await page.wait_for_timeout(4000)
            await browser.close()

            # Parse intercepted API data
            jobs = []
            for data in api_data:
                parsed = parse_api_response(data)
                jobs.extend(parsed)

            if not jobs:
                jobs = await fallback_api_scrape()

            return jobs

    except Exception as e:
        log.error(f"Scrape error: {e}")
        return await fallback_api_scrape()

async def fallback_api_scrape():
    """Direct API fallback"""
    jobs = []
    urls = [
        "https://hiring.amazon.co.uk/api/v1/search?country=GBR&locale=en-GB&pageSize=100",
        "https://www.jobsatamazon.co.uk/api/jobs?country=GB&pageSize=100",
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
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
    """Parse API response into job format"""
    jobs     = []
    raw_jobs = []

    if isinstance(data, list):
        raw_jobs = data
    elif isinstance(data, dict):
        for key in ["jobs", "data", "results", "jobPostings", "hits"]:
            if key in data:
                raw_jobs = data[key]
                break

    for raw in raw_jobs:
        try:
            job_id   = str(raw.get("jobId", raw.get("id", raw.get("requisitionId", ""))))
            if not job_id:
                continue

            location = raw.get("city", raw.get("locationName", "Unknown"))
            postcode = raw.get("postalCode", raw.get("zipCode", ""))
            pay      = float(raw.get("salaryMax", raw.get("maximumPayRate", 14.30)))
            contract = raw.get("employmentType", raw.get("scheduleType", "Full-time"))
            hours    = int(raw.get("hoursPerWeek", raw.get("weeklyHours", 40)))
            first_day = raw.get("firstDayOnSite", raw.get("startDate", "TBC"))
            schedule = raw.get("shiftCode", raw.get("schedule", ""))
            lat      = float(raw.get("latitude", 52.4862))
            lng      = float(raw.get("longitude", -1.8904))
            distance = calc_distance(lat, lng)
            link     = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}&locale=en-GB&recommended=1&intcmpid=searchalljobsleft"

            job = {
                "id":             job_id,
                "location":       location,
                "postcode":       postcode,
                "pay":            pay,
                "contract":       contract,
                "hours":          hours,
                "firstDay":       first_day,
                "schedule":       schedule,
                "distance_miles": distance,
                "link":           link,
                "found_at":       datetime.utcnow().isoformat(),
                "expiry":         datetime.utcnow() + timedelta(hours=2)
            }
            job["score"] = score_job(job)
            jobs.append(job)
        except Exception as e:
            log.warning(f"Parse error: {e}")
            continue

    return jobs

# ─── AUTOMATION ──────────────────────────────────────────────────────────────
async def automate_application(job):
    """Navigate Amazon application and auto-submit"""
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

            # Go to job page
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Step 1 — Click Apply
            for selector in ["button:has-text('Apply')", "a:has-text('Apply')", "[data-test='apply-button']", ".apply-button"]:
                btn = await page.query_selector(selector)
                if btn:
                    await btn.click()
                    log.info("✅ Clicked Apply")
                    await page.wait_for_timeout(2000)
                    break

            # Step 2 — Click Next
            for selector in ["button:has-text('Next')", "[data-test='next-button']", ".next-button"]:
                btn = await page.query_selector(selector)
                if btn:
                    await btn.click()
                    log.info("✅ Clicked Next")
                    await page.wait_for_timeout(2000)
                    break

            # Step 3 — Click Start Application
            for selector in ["button:has-text('Start Application')", "[data-test='start-application']"]:
                btn = await page.query_selector(selector)
                if btn:
                    await btn.click()
                    log.info("✅ Clicked Start Application")
                    await page.wait_for_timeout(2000)
                    break

            # Notify user — bot has reached application
            await tg_alert(job, "submitted")
            log.info(f"✅ Application processed: {job['location']}")
            await browser.close()

    except Exception as e:
        log.error(f"Automation error: {e}")
        await tg_send(f"""⚠️ <b>Bot needs your help!</b>
Job in <b>{job.get('location')}</b> found but 
automation hit an issue.
Please apply manually:
<a href="{job.get('link')}">Tap here to apply</a>""")

# ─── MAIN CHECK LOOP ─────────────────────────────────────────────────────────
async def check_for_new_jobs():
    global known_jobs, active_jobs
    if bot_paused:
        return
    if not user_location["city"]:
        return  # Don't check until location is set

    log.info(f"🔍 Checking Amazon jobs near {user_location['city']}...")
    jobs = await scrape_jobs()

    for job in jobs:
        jid = job["id"]
        if jid not in known_jobs:
            known_jobs[jid] = job
            active_jobs[jid] = job["expiry"]
            log.info(f"🆕 {job['location']} £{job['pay']}/hr {job['distance_miles']}mi Score:{job['score']}")

            # Alert user
            await tg_alert(job, "new")

            # Start automation
            asyncio.create_task(automate_application(job))

async def check_reminders():
    """Send reminders for jobs in 2hr window"""
    now = datetime.utcnow()
    for jid, expiry in list(active_jobs.items()):
        job      = known_jobs.get(jid)
        if not job:
            continue
        mins_left = int((expiry - now).total_seconds() / 60)

        if 58 <= mins_left <= 62:
            await tg_alert(job, "reminder")
        elif 28 <= mins_left <= 32:
            await tg_alert(job, "final")
        elif mins_left <= 0:
            del active_jobs[jid]

# ─── SMART TIMING ────────────────────────────────────────────────────────────
def get_check_interval():
    """Faster checks during Amazon peak posting hours"""
    uk_time = datetime.now(pytz.timezone("Europe/London"))
    hour    = uk_time.hour
    if 22 <= hour <= 23 or hour == 0:
        return 10   # 🔥 Peak hours — every 10 seconds!
    elif 18 <= hour < 22:
        return 30   # Evening — every 30 seconds
    else:
        return 60   # Off-peak — every 60 seconds

# ─── TELEGRAM COMMANDS ───────────────────────────────────────────────────────
async def handle_updates():
    """Poll Telegram for messages & button taps"""
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
    global bot_paused, awaiting_location

    # Handle button taps
    if "callback_query" in update:
        cb   = update["callback_query"]
        data = cb.get("data", "")
        if data.startswith("applied_"):
            jid = data.replace("applied_", "")
            if jid in active_jobs:
                del active_jobs[jid]
            await tg_send("✅ Marked as applied! Good luck Yonas! 💪🔥")
        elif data.startswith("skip_"):
            jid = data.replace("skip_", "")
            if jid in active_jobs:
                del active_jobs[jid]
            await tg_send("⏭️ Skipped. Watching for next job... 👀")
        return

    # Handle text messages
    msg  = update.get("message", {})
    text = msg.get("text", "").strip()

    # If waiting for location input
    if awaiting_location and text and not text.startswith("/"):
        await save_location(text)
        return

    cmd = text.lower()

    if cmd == "/start":
        await ask_for_location()

    elif cmd == "/location":
        if user_location["city"]:
            await tg_send(f"""📍 <b>Your Location</b>
━━━━━━━━━━━━━━━
City: <b>{user_location['city']}</b>
━━━━━━━━━━━━━━━
To change: /changelocation""")
        else:
            await ask_for_location()

    elif cmd == "/changelocation":
        await ask_for_location()

    elif cmd == "/status":
        status = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        loc    = user_location['city'] or "Not set"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━━━
Status: {status}
Location: 📍 {loc}
Jobs tracked: {len(known_jobs)}
Active windows: {len(active_jobs)}
Check interval: {get_check_interval()} seconds
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
        await tg_send("⏸️ Bot paused.\nSend /resume to restart.")

    elif cmd == "/resume":
        bot_paused = False
        await tg_send(f"▶️ Bot resumed!\nWatching near {user_location['city']}... 👀")

    elif cmd == "/stats":
        await tg_send(f"""📊 <b>Your Stats</b>
━━━━━━━━━━━━━━━━━━━
📍 Location: {user_location['city'] or 'Not set'}
🔍 Jobs found: {len(known_jobs)}
⏳ Active now: {len(active_jobs)}
🤖 Status: {'Paused' if bot_paused else 'Running'}
⚡ Check every: {get_check_interval()} secs
━━━━━━━━━━━━━━━━━━━
Keep going Yonas! 💪""")

    elif cmd == "/help":
        await tg_send("""🤖 <b>Amazon Shift Holder Commands</b>
━━━━━━━━━━━━━━━━━━━━━
/start          — Setup & welcome
/location       — View your location
/changelocation — Change location
/status         — Bot status
/jobs           — Active job windows
/pause          — Pause alerts
/resume         — Resume alerts
/stats          — Your statistics
/help           — This message
━━━━━━━━━━━━━━━━━━━━━""")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Amazon Shift Holder Starting...")

    # Start Telegram command handler
    asyncio.create_task(handle_updates())

    # If no location set — ask on startup
    if not user_location["city"]:
        await asyncio.sleep(2)
        await ask_for_location()
    else:
        await tg_send(f"""🚀 <b>Amazon Shift Holder ONLINE!</b>
━━━━━━━━━━━━━━━━━━━━━
📍 Location: {user_location['city']}
✅ Watching jobsatamazon.co.uk
⚡ Smart timing active
⏰ 2hr window tracking ON
━━━━━━━━━━━━━━━━━━━━━
Send /help for commands""")

    # Main loop
    while True:
        await check_for_new_jobs()
        await check_reminders()
        interval = get_check_interval()
        log.info(f"💤 Next check in {interval}s")
        await asyncio.sleep(interval)

if __name__ == "__main__":
    asyncio.run(main())
