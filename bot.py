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
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
CHAT_ID          = os.environ.get("CHAT_ID", "1027065157")
AMAZON_EMAIL     = os.environ.get("AMAZON_EMAIL", "")
AMAZON_PIN       = os.environ.get("AMAZON_PIN", "")
AMAZON_COOKIES   = os.environ.get("AMAZON_COOKIES", "")  # JSON string of cookies

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs      = {}
bot_paused      = False
job_history     = []
posting_times   = defaultdict(list)
session_headers = {}

# ─── SUBSCRIBER PREFERENCES ──────────────────────────────────────────────────
# Each subscriber has: chat_id, postcode, radius_miles, shift_prefs, auto_apply
subscribers = {
    CHAT_ID: {
        "postcode": os.environ.get("MY_POSTCODE", "B1 1BB"),
        "radius":   int(os.environ.get("MY_RADIUS", "30")),
        "shifts":   ["night", "any"],  # night first, then any
        "auto_apply": True,
    }
}

# Postcodes waiting for input
awaiting_postcode = {}
awaiting_radius   = {}

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── GRAPHQL QUERY ────────────────────────────────────────────────────────────
GRAPHQL_QUERY = """
query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {
  searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {
    nextToken
    jobCards {
      jobId jobTitle jobType employmentType city state
      postalCode locationName geoClusterDescription
      totalPayRateMin totalPayRateMax firstDayOnSite
      hoursPerWeek shiftCode scheduleCount currencyCode __typename
    }
    __typename
  }
}
"""

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

# ─── POSTCODE DISTANCE ────────────────────────────────────────────────────────
async def get_postcode_coords(postcode):
    """Get lat/lng for a UK postcode using free postcodes.io API"""
    try:
        clean = postcode.replace(" ", "").upper()
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.postcodes.io/postcodes/{clean}") as r:
                data = await r.json()
                if data.get("status") == 200:
                    result = data["result"]
                    return result["latitude"], result["longitude"]
    except Exception as e:
        log.warning(f"Postcode lookup error: {e}")
    return None, None

def haversine_miles(lat1, lon1, lat2, lon2):
    """Calculate distance in miles between two coordinates"""
    R = 3958.8  # Earth radius in miles
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

async def job_distance_miles(job_postcode, subscriber_postcode):
    """Calculate distance between job and subscriber in miles"""
    try:
        lat1, lon1 = await get_postcode_coords(subscriber_postcode)
        lat2, lon2 = await get_postcode_coords(job_postcode)
        if all([lat1, lon1, lat2, lon2]):
            return round(haversine_miles(lat1, lon1, lat2, lon2), 1)
    except Exception as e:
        log.warning(f"Distance calc error: {e}")
    return None

# ─── SHIFT PRIORITY ──────────────────────────────────────────────────────────
def is_night_shift(schedule):
    """Check if schedule is a night shift"""
    if not schedule or schedule == "TBC":
        return False
    night_indicators = ["18:30", "19:00", "20:00", "21:00", "22:00", "23:00", "23:45", "0:00", "1:00", "2:00", "3:00"]
    return any(t in schedule for t in night_indicators)

def shift_priority(schedule):
    """Return priority score — lower is better"""
    if is_night_shift(schedule):
        return 1  # Night shift — highest priority
    if any(t in str(schedule) for t in ["14:00", "15:00", "16:00"]):
        return 2  # Evening
    return 3  # Day shift

# ─── ALERT ───────────────────────────────────────────────────────────────────
async def tg_alert(job, status="new", chat_id=None, distance=None):
    cid = chat_id or CHAT_ID

    if status == "new":
        header = "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>"
    elif status == "navigating":
        header = "⚡ <b>BOT OPENING APPLICATION...</b>"
    elif status == "applying":
        header = "🤖 <b>BOT AUTO-SUBMITTING...</b>"
    elif status == "applied":
        header = "✅ <b>APPLIED FOR YOU AUTOMATICALLY!</b>"
    elif status == "ready":
        header = "✅ <b>APPLICATION READY — LOG IN & SUBMIT!</b>"
    else:
        header = "⚠️ <b>OPEN MANUALLY!</b>"

    pay_str  = job.get("pay_display") or f"{job.get('pay', '?'):.2f}"
    dist_str = f"\n📏 Distance: <b>{distance} miles</b>" if distance else ""
    night    = "🌙 NIGHT SHIFT" if is_night_shift(job.get("schedule","")) else ""

    text = f"""{header}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
📦 {job.get('title', 'Warehouse Operative')} {night}
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
            [{"text": "🚀 OPEN APPLICATION", "url": job.get("link", "https://www.jobsatamazon.co.uk")}],
            [
                {"text": "✅ APPLIED", "callback_data": f"applied_{job['id']}"},
                {"text": "⏭️ SKIP",   "callback_data": f"skip_{job['id']}"}
            ]
        ]
    }
    await tg_send(text, markup, chat_id=cid)

# ─── SESSION BUILDER ─────────────────────────────────────────────────────────
async def build_session():
    global session_headers
    log.info("🔑 Building session...")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}", lambda route: route.abort())

            # Inject saved Amazon cookies if available
            if AMAZON_COOKIES:
                try:
                    cookies = json.loads(AMAZON_COOKIES)
                    await context.add_cookies(cookies)
                    log.info(f"🍪 Injected {len(cookies)} Amazon cookies")
                except Exception as e:
                    log.warning(f"Cookie inject error: {e}")

            page = await context.new_page()
            captured_headers = {}

            async def sniff(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        captured_headers.update(dict(response.request.headers))
                except: pass

            page.on("response", sniff)

            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle", timeout=45000
            )
            await page.wait_for_timeout(4000)

            cookies = await context.cookies()
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
            for key in ["authorization", "x-amz-user-agent", "x-csrf-token"]:
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
    all_jobs = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
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
                except: pass

            page.on("response", handle_response)
            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle", timeout=45000
            )
            await page.wait_for_timeout(3000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            await browser.close()

            for card in captured:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job

    except Exception as e:
        log.error(f"Scraper error: {e}")

    log.info(f"👑 Total: {len(all_jobs)} jobs")
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

        if employment and employment.lower() not in ["seasonal", "temporary"]:
            contract = employment
        else:
            contract = job_type or employment or "Seasonal"

        hours       = str(int(card.get("hoursPerWeek"))) if card.get("hoursPerWeek") else "TBC"
        first_day   = card.get("firstDayOnSite") or "TBC"
        sched_count = card.get("scheduleCount", 0)
        shift_code  = card.get("shiftCode") or ""
        schedule    = shift_code if shift_code else (f"{sched_count} schedule(s)" if sched_count else "TBC")

        skip = ["customer service", "vcc", "virtual", "remote", "manager", "software", "engineer"]
        if any(s in title.lower() for s in skip):
            return None

        parts = []
        if city: parts.append(city)
        if state and state != city: parts.append(state)

        if geo and postcode:
            location = f"{', '.join(parts)} ({geo}) {postcode}".strip()
        elif geo:
            location = f"{', '.join(parts)} ({geo})".strip()
        elif postcode:
            location = f"{', '.join(parts)} {postcode}".strip()
        else:
            location = ", ".join(parts) or "Unknown UK Location"

        link = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}&locale=en-GB&recommended=1&intcmpid=searchalljobsleft"

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
            "link":        link,
            "found_at":    datetime.utcnow().isoformat(),
        }
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None

# ─── FETCH FULL JOB DETAILS ──────────────────────────────────────────────────
async def fetch_job_details(job):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}", lambda route: route.abort())
            page = await context.new_page()
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
            content = await page.inner_text("body")

            # First Day
            m = re.search(r'(?:Start [Dd]ate|Tentative start date)[:\s]+([A-Za-z]+,?\s+\d+\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+ \d+, \d{4})', content)
            if m: job["firstDay"] = m.group(1).strip()

            # Schedule
            m = re.search(r'Shift timing[:\s]+([A-Za-z,\s]+\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})', content)
            if m:
                job["schedule"] = m.group(1).strip()
            else:
                m = re.search(r'Shift[:\s]+([A-Za-z,\s]+\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})', content)
                if m: job["schedule"] = m.group(1).strip()

            # Hours
            m = re.search(r'(\d+)\s*hrs?\s*per\s*week', content, re.IGNORECASE)
            if m: job["hours"] = m.group(1)

            # Contract
            for ct in ["Full-time", "Part-time", "Reduced", "Fixed-term", "Fixed Term"]:
                if ct.lower() in content.lower():
                    job["contract"] = ct
                    break

            await browser.close()
            log.info(f"✅ Details: {job.get('firstDay','?')} | {job.get('schedule','?')[:40]}")
    except Exception as e:
        log.warning(f"Detail fetch error: {e}")
    return job

# ─── AUTO SUBMIT ─────────────────────────────────────────────────────────────
async def auto_submit(job, chat_id=None):
    """Full auto-submit using stored Amazon session cookies"""
    cid = chat_id or CHAT_ID
    log.info(f"🤖 Auto-submitting: {job['location']}")
    await tg_alert(job, "applying", chat_id=cid)

    if not AMAZON_COOKIES:
        log.warning("No Amazon cookies — falling back to navigate only")
        await auto_navigate(job, chat_id=cid)
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
            )

            # Inject Amazon session cookies
            cookies = json.loads(AMAZON_COOKIES)
            await context.add_cookies(cookies)
            log.info(f"🍪 Injected {len(cookies)} Amazon session cookies")

            page = await context.new_page()

            # Step 1 — Go to job page
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Step 2 — Click Apply button
            applied = False
            for sel in ["button:has-text('Apply')", "a:has-text('Apply')", "[data-test='apply-button']", "button:has-text('Apply now')"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        log.info("✅ Clicked Apply")
                        applied = True
                        break
                except: pass

            if not applied:
                log.warning("⚠️ Apply button not found — may need login")
                await tg_alert(job, "ready", chat_id=cid)
                await browser.close()
                return

            # Step 3 — Handle active application popup
            try:
                continue_btn = await page.wait_for_selector("button:has-text('Continue')", timeout=3000)
                if continue_btn:
                    await continue_btn.click()
                    await page.wait_for_timeout(2000)
                    log.info("✅ Dismissed active app popup")
            except: pass

            # Step 4 — Click Start Application on T&Cs page
            for sel in ["button:has-text('Start Application')", "[data-test='start-application']"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=5000)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(3000)
                        log.info("✅ Clicked Start Application")
                        break
                except: pass

            # Step 5 — Select best shift (prioritise night shifts)
            try:
                # Wait for shift selection page
                await page.wait_for_selector("button:has-text('Select this job')", timeout=8000)
                shift_buttons = await page.query_selector_all("button:has-text('Select this job')")

                if shift_buttons:
                    # Get all shift cards and find night shift
                    best_btn = shift_buttons[0]  # Default to first
                    best_priority = 999

                    shift_cards = await page.query_selector_all(".shift-card, [class*='shift'], [class*='job-card']")
                    for i, card in enumerate(shift_cards[:len(shift_buttons)]):
                        try:
                            card_text = await card.inner_text()
                            priority  = shift_priority(card_text)
                            if priority < best_priority:
                                best_priority = priority
                                if i < len(shift_buttons):
                                    best_btn = shift_buttons[i]
                        except: pass

                    await best_btn.click()
                    await page.wait_for_timeout(3000)
                    log.info(f"✅ Selected shift (priority: {best_priority})")

            except Exception as e:
                log.warning(f"Shift selection: {e}")

            # Step 6 — Accept Offer if shown
            try:
                accept_btn = await page.wait_for_selector("button:has-text('Accept Offer')", timeout=5000)
                if accept_btn:
                    await accept_btn.click()
                    await page.wait_for_timeout(3000)
                    log.info("✅ Clicked Accept Offer")
            except: pass

            # Step 7 — Check success
            current_url = page.url
            content     = await page.inner_text("body")

            if "thank you" in content.lower() or "applied" in content.lower() or "checklist" in current_url:
                log.info(f"🎉 Successfully applied for {job['location']}!")
                await tg_alert(job, "applied", chat_id=cid)
            else:
                log.warning("⚠️ Not sure if applied — sending ready alert")
                job["link"] = current_url if current_url != "about:blank" else job["link"]
                await tg_alert(job, "ready", chat_id=cid)

            await browser.close()

    except Exception as e:
        log.error(f"Auto-submit error: {e}")
        await tg_alert(job, "ready", chat_id=cid)

# ─── AUTO NAVIGATE (fallback for subscribers) ────────────────────────────────
async def auto_navigate(job, chat_id=None):
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

            for sel in ["button:has-text('Apply')", "a:has-text('Apply')"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except: pass

            for sel in ["button:has-text('Start Application')"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except: pass

            job["link"] = page.url if page.url != "about:blank" else job["link"]
            await browser.close()
            await tg_alert(job, "ready", chat_id=cid)
    except Exception as e:
        log.error(f"Navigation error: {e}")
        await tg_alert(job, "failed", chat_id=cid)

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

            # Fetch full details
            job = await fetch_job_details(job)
            known_jobs[jid] = job

            # Alert each subscriber based on their location preference
            for sub_chat_id, prefs in subscribers.items():
                sub_postcodes = prefs.get("postcode", "").split(",")
                sub_radius    = prefs.get("radius", 30)
                sub_auto      = prefs.get("auto_apply", False)
                job_postcode  = job.get("postcode", "")
                distance      = None
                too_far       = True

                # Check distance from ALL subscriber postcodes
                for sub_postcode in sub_postcodes:
                    sub_postcode = sub_postcode.strip()
                    if sub_postcode and job_postcode:
                        d = await job_distance_miles(job_postcode, sub_postcode)
                        if d is not None:
                            if distance is None or d < distance:
                                distance = d  # Keep closest distance
                            if d <= sub_radius:
                                too_far = False
                                break
                    else:
                        too_far = False  # No postcode set = alert everything
                        break

                if too_far and distance:
                    log.info(f"📍 Job too far ({distance}mi > {sub_radius}mi) for {sub_chat_id}")
                    continue  # Skip — too far from all locations

                # Send alert
                await tg_alert(job, "new", chat_id=sub_chat_id, distance=distance)

                # Auto-submit or navigate
                if sub_auto and AMAZON_COOKIES:
                    asyncio.create_task(auto_submit(job, chat_id=sub_chat_id))
                else:
                    asyncio.create_task(auto_navigate(job, chat_id=sub_chat_id))

    if new_count == 0:
        log.info(f"👑 No new jobs — {len(known_jobs)} tracked")
    return new_count

# ─── SESSION REFRESH ─────────────────────────────────────────────────────────
async def session_refresh_loop():
    while True:
        await asyncio.sleep(6 * 60 * 60)
        log.info("🔄 Refreshing session...")
        await build_session()

# ─── DAILY SUMMARY ───────────────────────────────────────────────────────────
async def send_daily_summary():
    while True:
        now = datetime.utcnow()
        if now.hour == 7 and now.minute == 0:
            today = [j for j in job_history if j.get("found_at","")[:10] == now.strftime("%Y-%m-%d")]
            if today:
                best    = max(today, key=lambda x: x.get("pay", 0))
                avg_pay = sum(j.get("pay", 0) for j in today) / len(today)
                nights  = sum(1 for j in today if is_night_shift(j.get("schedule","")))
                await tg_send(f"""📊 <b>Daily Summary</b>
━━━━━━━━━━━━━━━━━
📅 {now.strftime('%Y-%m-%d')}
🆕 Jobs found: {len(today)}
🌙 Night shifts: {nights}
💰 Avg pay: £{avg_pay:.2f}/hr
⭐ Best: {best.get('location','?')} £{best.get('pay','?')}/hr
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
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        await process_update(update)
        except Exception as e:
            log.error(f"Update error: {e}")
        await asyncio.sleep(2)

async def process_update(update):
    global bot_paused, awaiting_postcode, awaiting_radius

    if "callback_query" in update:
        cb = update["callback_query"]
        d  = cb.get("data", "")
        if d.startswith("applied_"): await tg_send("✅ Applied! Good luck! 💪🔥")
        elif d.startswith("skip_"):  await tg_send("⏭️ Skipped! 👀")
        return

    msg     = update.get("message", {})
    text    = msg.get("text", "").strip()
    cid     = str(msg.get("chat", {}).get("id", CHAT_ID))
    text_lw = text.lower()

    # Handle postcode input
    if cid in awaiting_postcode:
        postcode = text.upper().strip()
        if cid not in subscribers:
            subscribers[cid] = {}
        subscribers[cid]["postcode"] = postcode
        awaiting_postcode.pop(cid)
        awaiting_radius[cid] = True
        await tg_send(f"📍 Postcode set to <b>{postcode}</b>\n\nNow enter your max travel radius in miles (e.g. 10, 20, 30, 50):", chat_id=cid)
        return

    # Handle radius input
    if cid in awaiting_radius:
        try:
            radius = int(re.search(r'\d+', text).group())
            subscribers[cid]["radius"] = radius
            awaiting_radius.pop(cid)
            postcode = subscribers[cid].get("postcode", "?")
            await tg_send(f"✅ <b>Location preference saved!</b>\n📍 Postcode: {postcode}\n🚗 Radius: {radius} miles\n\nYou'll only get alerts for jobs within {radius} miles of {postcode}! 👑", chat_id=cid)
        except:
            await tg_send("❌ Please enter a number e.g. 20", chat_id=cid)
        return

    if text_lw == "/start":
        await tg_send("""👑 <b>Amazon KING BOT v7!</b>
✅ Direct — No proxy cost
🌙 Night shift priority
📍 Location filter by postcode
🤖 Auto-submit (your account)
🌍 ALL UK warehouse jobs
Send /help for all commands!""", chat_id=cid)

    elif text_lw == "/status":
        sub      = subscribers.get(cid, {})
        postcode = sub.get("postcode", "Not set")
        radius   = sub.get("radius", "Not set")
        auto     = "✅ ON" if sub.get("auto_apply") and AMAZON_COOKIES else "❌ OFF"
        now      = datetime.utcnow()
        h, m     = now.hour, now.minute
        am_peak  = (h == 10 and m >= 55) or (h == 11 and m <= 25)
        pm_peak  = (h == 22 and m >= 55) or (h == 23 and m <= 25)
        speed    = "1s ⚡ ULTRA BEAST" if (am_peak or pm_peak) else "30s 💤 Normal"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {"⏸️ PAUSED" if bot_paused else "✅ RUNNING"}
Auto-submit: {auto}
📍 Your postcode: {postcode}
🚗 Radius: {radius} miles
Jobs tracked: {len(known_jobs)}
History: {len(job_history)}
Speed: {speed}
━━━━━━━━━━━━━━━━━""", chat_id=cid)

    elif text_lw.startswith("/prefer location") or text_lw == "/prefer":
        awaiting_postcode[cid] = True
        await tg_send("📍 <b>Set your location</b>\n\nEnter your postcode (e.g. B1 1BB, LS9 0DZ):", chat_id=cid)

    elif text_lw == "/mylocation":
        sub = subscribers.get(cid, {})
        await tg_send(f"""📍 <b>Your Location Settings</b>
━━━━━━━━━━━━━━━━━
Postcode: {sub.get('postcode', 'Not set')}
Radius: {sub.get('radius', 'Not set')} miles
━━━━━━━━━━━━━━━━━
Use /prefer to update""", chat_id=cid)

    elif text_lw == "/autoon":
        if cid not in subscribers:
            subscribers[cid] = {}
        subscribers[cid]["auto_apply"] = True
        await tg_send("🤖 Auto-submit <b>ON</b> — Bot will apply automatically!", chat_id=cid)

    elif text_lw == "/autooff":
        if cid not in subscribers:
            subscribers[cid] = {}
        subscribers[cid]["auto_apply"] = False
        await tg_send("⏸️ Auto-submit <b>OFF</b> — Alerts only mode", chat_id=cid)

    elif text_lw == "/scrape":
        await tg_send("🔍 <b>Scanning ALL UK Amazon jobs...</b>", chat_id=cid)
        count = await check_jobs()
        await tg_send(f"✅ New: {count} | Tracked: {len(known_jobs)}\n{'🎉 Alerts sent!' if count > 0 else '⏳ No new jobs!'}", chat_id=cid)

    elif text_lw == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet!", chat_id=cid)
        else:
            txt = f"📋 <b>Last {min(5,len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                night = "🌙" if is_night_shift(job.get("schedule","")) else "☀️"
                txt += f"{night} {job.get('location')}\n💰 £{job.get('pay')}/hr | {job.get('contract')}\n📅 {job.get('firstDay','TBC')}\n\n"
            await tg_send(txt, chat_id=cid)

    elif text_lw == "/history":
        if not job_history:
            await tg_send("📭 No history!", chat_id=cid)
        else:
            total  = len(job_history)
            avg    = sum(j.get("pay",0) for j in job_history) / total
            best   = max(job_history, key=lambda x: x.get("pay",0))
            nights = sum(1 for j in job_history if is_night_shift(j.get("schedule","")))
            await tg_send(f"""📊 <b>Job History</b>
━━━━━━━━━━━━━━━━━
Total found: {total}
🌙 Night shifts: {nights}
Avg pay: £{avg:.2f}/hr
Best: {best.get('location','?')} £{best.get('pay','?')}/hr
━━━━━━━━━━━━━━━━━""", chat_id=cid)

    elif text_lw == "/test":
        await tg_alert({
            "id": "JOB-UK-TEST-001",
            "title": "Warehouse Operative",
            "location": "Enfield, England (North-East London) EN3 7PZ",
            "postcode": "EN3 7PZ",
            "pay": 15.30, "pay_display": "15.30",
            "contract": "Reduced",
            "firstDay": "2026-05-14",
            "schedule": "Thu, Fri, Sat 23:45-10:15",
            "hours": "30",
            "link": "https://www.jobsatamazon.co.uk",
        }, "new", chat_id=cid, distance=12.5)

    elif text_lw == "/pause":
        bot_paused = True
        await tg_send("⏸️ Paused.", chat_id=cid)

    elif text_lw == "/resume":
        bot_paused = False
        await tg_send("▶️ Resumed! 🔥", chat_id=cid)

    elif text_lw == "/help":
        await tg_send("""👑 <b>King Bot Commands</b>
━━━━━━━━━━━━━━━━━
/scrape        — Scan now
/status        — Bot status
/prefer        — Set your location
/mylocation    — View your settings
/autoon        — Enable auto-submit
/autooff       — Disable auto-submit
/jobs          — Recent jobs
/history       — All time stats
/test          — Test alert
/pause         — Pause bot
/resume        — Resume bot
━━━━━━━━━━━━━━━━━""", chat_id=cid)

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("👑 Amazon KING BOT v7 Starting!")

    await build_session()

    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())
    asyncio.create_task(session_refresh_loop())

    await asyncio.sleep(2)

    sub     = subscribers.get(CHAT_ID, {})
    cookies = "✅ Ready" if AMAZON_COOKIES else "❌ Not set — add AMAZON_COOKIES to Render"
    await tg_send(f"""👑 <b>Amazon KING BOT v7 ONLINE!</b>
✅ Direct — No proxy cost
🌙 Night shift priority
📍 Location: {sub.get('postcode','Not set')} ({sub.get('radius',30)}mi radius)
🤖 Auto-submit: {cookies}
🌍 ALL UK warehouse jobs
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
