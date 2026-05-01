import asyncio
import os
import json
import logging
import aiohttp
import re
from datetime import datetime
from playwright.async_api import async_playwright
from collections import defaultdict

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
CHAT_ID     = os.environ.get("CHAT_ID", "1027065157")
DECODO_USER = os.environ.get("DECODO_USER", "")
DECODO_PASS = os.environ.get("DECODO_PASS", "")
DECODO_HOST = os.environ.get("DECODO_HOST", "gb.decodo.com")
DECODO_PORT = os.environ.get("DECODO_PORT", "30000")

PROXY_URL    = f"http://{DECODO_USER}:{DECODO_PASS}@{DECODO_HOST}:{DECODO_PORT}"
PROXY_SERVER = f"http://{DECODO_HOST}:{DECODO_PORT}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs    = {}
bot_paused    = False
job_history   = []
posting_times = defaultdict(list)
session_headers = {}   # Real headers from Amazon session
session_cookies_str = ""  # Cookie string for API calls

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
async def tg_send(text, reply_markup=None):
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ─── ALERT ───────────────────────────────────────────────────────────────────
async def tg_alert(job, status="new"):
    if status == "new":
        header = "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>"
    elif status == "navigating":
        header = "⚡ <b>BOT OPENING APPLICATION...</b>"
    elif status == "ready":
        header = "✅ <b>APPLICATION READY — LOG IN & SUBMIT!</b>"
    else:
        header = "⚠️ <b>OPEN MANUALLY!</b>"

    pay_str  = job.get("pay_display") or f"{job.get('pay', '?'):.2f}"
    contract = job.get("contract", "Seasonal")
    hours    = job.get("hours", "TBC")
    schedule = job.get("schedule", "TBC")
    first_day = job.get("firstDay", "TBC")

    text = f"""{header}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
📦 {job.get('title', 'Warehouse Operative')}
💰 <b>£{pay_str}/hr</b>
📋 {contract}
📅 First Day: <b>{first_day}</b>
🕘 Schedule: <b>{schedule}</b>
🕐 Hours/Week: <b>{hours}</b>
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "ready":
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
    await tg_send(text, markup)

# ─── SESSION BUILDER (runs ONCE — no proxy needed) ────────────────────────────
async def build_session():
    """
    Visit Amazon ONCE without proxy to get real headers/cookies.
    Then use those headers for all future API calls through proxy.
    This saves massive amounts of proxy data!
    """
    global session_headers, session_cookies_str
    log.info("🔑 Building session (one-time)...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
                viewport={"width": 1280, "height": 800},
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            page = await context.new_page()
            captured_headers = {}

            async def sniff_headers(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        captured_headers.update(dict(response.request.headers))
                except: pass

            page.on("response", sniff_headers)

            # Visit Amazon job search — NO proxy, just to get session
            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle",
                timeout=45000
            )
            await page.wait_for_timeout(4000)

            # Grab cookies
            cookies = await context.cookies()
            session_cookies_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

            # Build headers from captured + defaults
            session_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Language": "en-GB,en;q=0.9",
                "country": "United Kingdom",
                "locale": "en-GB",
                "Origin": "https://www.jobsatamazon.co.uk",
                "Referer": "https://www.jobsatamazon.co.uk/app",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Cookie": session_cookies_str,
            }

            # Add any extra headers captured from real GraphQL calls
            for key in ["x-amz-user-agent", "authorization", "x-csrf-token"]:
                if key in captured_headers:
                    session_headers[key] = captured_headers[key]

            await browser.close()
            log.info(f"✅ Session built! {len(cookies)} cookies, {len(session_headers)} headers")
            return True

    except Exception as e:
        log.error(f"Session build error: {e}")
        return False

# ─── ULTRA LEAN SCRAPER — API ONLY, TINY DATA USAGE ──────────────────────────
async def fetch_jobs():
    """
    Sends a tiny GraphQL API call (~5KB) through proxy.
    No full page loads. No browser through proxy.
    Data usage: ~1-2GB/month instead of 10GB/day!
    """
    global session_headers, session_cookies_str
    all_jobs = {}

    if not session_headers:
        log.warning("No session — rebuilding...")
        await build_session()
        if not session_headers:
            return []

    variables_list = [
        {"locale": "en-GB", "country": "United Kingdom", "keyWords": "warehouse", "equalFilters": [], "containFilters": [], "pageSize": 100},
        {"locale": "en-GB", "country": "United Kingdom", "keyWords": "", "equalFilters": [], "containFilters": [], "pageSize": 100},
        {"locale": "en-GB", "country": "United Kingdom", "keyWords": "warehouse operative", "equalFilters": [], "containFilters": [], "pageSize": 100},
    ]

    # Run all 3 searches in parallel — 3x faster!
    async def search(variables):
        try:
            payload = {
                "operationName": "searchJobCardsByLocation",
                "query": GRAPHQL_QUERY,
                "variables": {"searchJobRequest": variables}
            }
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    "https://www.jobsatamazon.co.uk/graphql",
                    json=payload,
                    headers=session_headers,
                    proxy=PROXY_URL,
                    timeout=aiohttp.ClientTimeout(total=20),
                    ssl=False
                ) as resp:
                    if resp.status == 200:
                        data  = await resp.json()
                        cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                        log.info(f"✅ Search '{variables['keyWords']}': {len(cards)} jobs")
                        return cards
                    else:
                        log.warning(f"API status {resp.status} for '{variables['keyWords']}'")
                        return []
        except Exception as e:
            log.warning(f"Search error: {e}")
            return []

    # Run all 3 searches simultaneously — parallel scraping!
    results = await asyncio.gather(*[search(v) for v in variables_list])

    for cards in results:
        for card in cards:
            job = parse_card(card)
            if job and job["id"] not in all_jobs:
                all_jobs[job["id"]] = job

    log.info(f"👑 Total unique jobs: {len(all_jobs)}")
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

        # Contract type
        if employment and employment.lower() not in ["seasonal", "temporary"]:
            contract = employment
        else:
            contract = job_type or employment or "Seasonal"

        hours      = str(int(card.get("hoursPerWeek"))) if card.get("hoursPerWeek") else "TBC"
        first_day  = card.get("firstDayOnSite") or "TBC"
        sched_count = card.get("scheduleCount", 0)
        shift_code  = card.get("shiftCode") or ""
        schedule    = shift_code if shift_code else (f"{sched_count} schedule(s)" if sched_count else "TBC")

        # Skip non-warehouse jobs
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

# ─── AUTO NAVIGATION (separate browser, not through proxy) ───────────────────
async def auto_navigate(job):
    """Opens application page — no proxy needed, saves data"""
    log.info(f"🤖 Navigating: {job['location']}")
    await tg_alert(job, "navigating")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            page = await context.new_page()
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            for sel in ["button:has-text('Apply')", "a:has-text('Apply')", "[data-test='apply-button']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except: pass

            for sel in ["button:has-text('Start Application')", "[data-test='start-application']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        break
                except: pass

            job["link"] = page.url if page.url != "about:blank" else job["link"]
            await browser.close()
            await tg_alert(job, "ready")
    except Exception as e:
        log.error(f"Navigation error: {e}")
        await tg_alert(job, "failed")

# ─── SESSION REFRESH (every 6 hours) ─────────────────────────────────────────
async def session_refresh_loop():
    while True:
        await asyncio.sleep(6 * 60 * 60)  # Every 6 hours
        log.info("🔄 Refreshing session...")
        await build_session()

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
            known_jobs[jid] = job
            new_count += 1
            job_history.append(job)
            posting_times[job["location"][:20]].append(datetime.utcnow().hour)
            log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr")
            await tg_alert(job, "new")
            asyncio.create_task(auto_navigate(job))

    if new_count == 0:
        log.info(f"👑 No new jobs — {len(known_jobs)} tracked")
    return new_count

# ─── DAILY SUMMARY ───────────────────────────────────────────────────────────
async def send_daily_summary():
    while True:
        now = datetime.utcnow()
        if now.hour == 7 and now.minute == 0:
            today = [j for j in job_history if j.get("found_at","")[:10] == now.strftime("%Y-%m-%d")]
            if today:
                best    = max(today, key=lambda x: x.get("pay", 0))
                avg_pay = sum(j.get("pay", 0) for j in today) / len(today)
                await tg_send(f"""📊 <b>Daily Summary</b>
━━━━━━━━━━━━━━━━━
📅 {now.strftime('%Y-%m-%d')}
🆕 Jobs found: {len(today)}
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
    global bot_paused
    if "callback_query" in update:
        cb = update["callback_query"]
        d  = cb.get("data", "")
        if d.startswith("applied_"): await tg_send("✅ Applied! Good luck Yonas! 💪🔥")
        elif d.startswith("skip_"):  await tg_send("⏭️ Skipped! 👀")
        return

    msg  = update.get("message", {})
    text = msg.get("text", "").strip().lower()

    if text == "/start":
        await tg_send("""👑 <b>Amazon KING BOT v6!</b>
⚡ Ultra lean — ~1GB/month only
🔥 Parallel x3 scraping
🌍 ALL UK warehouse jobs
🤖 Auto-navigates application
👆 Log in & Submit!
Send /scrape to check now!""")

    elif text == "/status":
        status  = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        session = "✅ Active" if session_headers else "⚠️ No session"
        now     = datetime.utcnow()
        h, m    = now.hour, now.minute
        am_peak = (h == 10 and m >= 55) or (h == 11 and m <= 25)
        pm_peak = (h == 22 and m >= 55) or (h == 23 and m <= 25)
        speed   = "1s ⚡ ULTRA BEAST" if (am_peak or pm_peak) else "3s 🔥 BEAST MODE"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {status}
Session: {session}
Proxy: ✅ Decodo GB 🇬🇧
Mode: Lean API — ~1GB/month
Jobs tracked: {len(known_jobs)}
History: {len(job_history)}
Speed: {speed}
━━━━━━━━━━━━━━━━━""")

    elif text == "/scrape":
        await tg_send("🔍 <b>Scanning ALL UK Amazon jobs...</b>")
        count = await check_jobs()
        await tg_send(f"✅ New: {count} | Tracked: {len(known_jobs)}\n{'🎉 Alerts sent!' if count > 0 else '⏳ No new jobs!'}")

    elif text == "/session":
        await tg_send("🔄 Rebuilding session...")
        ok = await build_session()
        await tg_send(f"{'✅ Session ready!' if ok else '❌ Session failed'}")

    elif text == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet!")
        else:
            txt = f"📋 <b>Last {min(5,len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                txt += f"📍 {job.get('location')}\n💰 £{job.get('pay')}/hr | {job.get('contract')}\n\n"
            await tg_send(txt)

    elif text == "/history":
        if not job_history:
            await tg_send("📭 No history!")
        else:
            total = len(job_history)
            avg   = sum(j.get("pay",0) for j in job_history) / total
            best  = max(job_history, key=lambda x: x.get("pay",0))
            await tg_send(f"""📊 <b>Job History</b>
━━━━━━━━━━━━━━━━━
Total found: {total}
Avg pay: £{avg:.2f}/hr
Best: {best.get('location','?')} £{best.get('pay','?')}/hr
━━━━━━━━━━━━━━━━━""")

    elif text == "/predict":
        if not posting_times:
            await tg_send("📭 Not enough data yet!")
        else:
            txt = "🧠 <b>Posting Patterns</b>\n━━━━━━━━━━━━━━━\n"
            for loc, times in list(posting_times.items())[:5]:
                if times:
                    common = max(set(times), key=times.count)
                    txt += f"📍 {loc}\n⏰ Usually {common}:00 UTC\n\n"
            await tg_send(txt)

    elif text == "/test":
        await tg_alert({
            "id": "JOB-UK-TEST-001",
            "title": "Warehouse Operative",
            "location": "Enfield, England (North-East London) EN3 7PZ",
            "pay": 15.30,
            "pay_display": "15.30",
            "contract": "Reduced",
            "firstDay": "2026-05-14",
            "schedule": "Thu, Fri, Sat 23:45-10:15",
            "hours": "30",
            "link": "https://www.jobsatamazon.co.uk",
        }, "new")

    elif text == "/pause":
        bot_paused = True
        await tg_send("⏸️ Paused.")

    elif text == "/resume":
        bot_paused = False
        await tg_send("▶️ Resumed! 🔥")

    elif text == "/help":
        await tg_send("""👑 <b>King Bot Commands</b>
/scrape   — Scan now
/status   — Bot status
/session  — Rebuild session
/jobs     — Recent jobs
/history  — All time stats
/predict  — Posting patterns
/test     — Test alert
/pause    — Pause
/resume   — Resume""")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("👑 Amazon KING BOT v6 — Ultra Lean Edition!")

    # Build session ONCE without proxy
    await build_session()

    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())
    asyncio.create_task(session_refresh_loop())

    await asyncio.sleep(2)
    await tg_send(f"""👑 <b>Amazon KING BOT v6 ONLINE!</b>
⚡ Ultra Lean Mode — ~1GB/month
🔥 Parallel x3 scraping
✅ Session: {'Ready' if session_headers else 'Building...'}
🌍 ALL UK warehouse jobs
👆 Log in & Submit!
Send /scrape to check now!""")

    await check_jobs()

    while True:
        now     = datetime.utcnow()
        h, m    = now.hour, now.minute
        am_peak = (h == 10 and m >= 55) or (h == 11 and m <= 25)
        pm_peak = (h == 22 and m >= 55) or (h == 23 and m <= 25)

        if am_peak or pm_peak:
            await asyncio.sleep(1)   # ⚡ ULTRA BEAST — peak windows
        else:
            await asyncio.sleep(3)   # 🔥 BEAST MODE — all other times

        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
