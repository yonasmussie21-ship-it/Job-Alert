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

PROXY_SERVER = f"http://{DECODO_HOST}:{DECODO_PORT}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs      = {}
bot_paused      = False
job_history     = []
posting_times   = defaultdict(list)
session_cookies = []
auth_token      = ""

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── GRAPHQL QUERY ────────────────────────────────────────────────────────────
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
      geoClusterDescription
      totalPayRateMin
      totalPayRateMax
      firstDayOnSite
      hoursPerWeek
      shiftCode
      scheduleCount
      currencyCode
      __typename
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

# ─── JOB SCORING ─────────────────────────────────────────────────────────────
def score_job(job):
    score    = 0
    pay      = job.get("pay", 0)
    contract = job.get("contract", "").lower()
    hours    = int(job.get("hours", 0)) if str(job.get("hours", "0")).isdigit() else 0

    if pay >= 15.30:   score += 40
    elif pay >= 14.30: score += 30
    else:              score += 15

    if "full" in contract:      score += 35
    elif "reduced" in contract: score += 25
    elif "part" in contract:    score += 15

    if hours >= 40:   score += 25
    elif hours >= 30: score += 18
    elif hours >= 20: score += 10

    return min(score, 100)

def get_star_rating(score):
    if score >= 85:   return "⭐⭐⭐ EXCELLENT"
    elif score >= 65: return "⭐⭐ GOOD"
    else:             return "⭐ OK"

# ─── ALERT ───────────────────────────────────────────────────────────────────
async def tg_alert(job, status="new"):
    score = job.get("score", 0)
    stars = get_star_rating(score)

    if status == "new":
        header = f"🚨 <b>NEW AMAZON JOB — ACT NOW!</b>\n{stars} | Score: {score}/100"
    elif status == "navigating":
        header = "⚡ <b>BOT NAVIGATING APPLICATION...</b>"
    elif status == "ready":
        header = "✅ <b>READY — TAP SUBMIT NOW!</b>"
    else:
        header = "⚠️ <b>APPLY MANUALLY!</b>"

    pay_str = job.get("pay_display") or f"{job.get('pay', '?'):.2f}"
    text = f"""{header}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
📦 {job.get('title', 'Warehouse Operative')}
💰 <b>£{pay_str}/hr</b>
⏱️ {job.get('duration', 'Seasonal')} | {job.get('contract', '?')}
💼 Pick, pack and ship parcels
📅 First Day: <b>{job.get('firstDay', 'TBC')}</b>
🕘 Schedule: <b>{job.get('schedule', 'TBC')}</b>
🕐 Hours/Week: <b>{job.get('hours', 'TBC')}</b>
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "ready":
        text += "\n👆 <b>TAP SUBMIT to complete!</b>\n━━━━━━━━━━━━━━━━━━━━━"

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

# ─── SMART SESSION BUILDER ────────────────────────────────────────────────────
async def build_session():
    """
    Visit Amazon like a real human first.
    Collect real cookies + auth tokens.
    King move — real session = real data.
    """
    global session_cookies, auth_token
    log.info("👑 Building real Amazon session...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            context = await browser.new_context(
                proxy={"server": PROXY_SERVER, "username": DECODO_USER, "password": DECODO_PASS},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"}
            )

            # Hide automation
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            """)

            page = await context.new_page()

            # Step 1: Visit homepage like human
            log.info("🏠 Visiting homepage...")
            await page.goto("https://www.jobsatamazon.co.uk", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            await page.evaluate("window.scrollTo(0, 300)")
            await page.wait_for_timeout(1000)

            # Step 2: Navigate to job search
            log.info("🔍 Navigating to job search...")
            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle",
                timeout=45000
            )
            await page.wait_for_timeout(4000)

            # Step 3: Collect cookies
            cookies = await context.cookies()
            session_cookies = cookies
            log.info(f"✅ Session built! {len(session_cookies)} cookies collected")
            await browser.close()
            return True

    except Exception as e:
        log.error(f"Session build error: {e}")
        return False

# ─── KING SCRAPER ─────────────────────────────────────────────────────────────
async def fetch_jobs():
    global session_cookies
    all_jobs = {}

    if not DECODO_USER or not DECODO_PASS:
        log.error("❌ Decodo credentials not configured!")
        return []

    # Build session if not exists
    if not session_cookies:
        await build_session()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            context = await browser.new_context(
                proxy={"server": PROXY_SERVER, "username": DECODO_USER, "password": DECODO_PASS},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
                viewport={"width": 1280, "height": 800},
            )

            # Hide automation
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            """)

            # Inject saved session cookies
            if session_cookies:
                await context.add_cookies(session_cookies)
                log.info(f"🍪 Injected {len(session_cookies)} cookies")

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

            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle",
                timeout=45000
            )
            await page.wait_for_timeout(5000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(2000)

            if captured:
                log.info(f"✅ Intercept got {len(captured)} jobs!")
            else:
                # Smart injection using real session
                log.info("💉 Smart injection with real session cookies...")
                for variables in [
                    {"locale": "en-GB", "country": "United Kingdom", "keyWords": "warehouse", "equalFilters": [], "containFilters": [], "pageSize": 100},
                    {"locale": "en-GB", "country": "United Kingdom", "keyWords": "", "equalFilters": [], "containFilters": [], "pageSize": 100},
                ]:
                    try:
                        result = await page.evaluate("""
                            async (vars) => {
                                const query = `query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {
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
                                }`;
                                try {
                                    const r = await fetch('/graphql', {
                                        method: 'POST',
                                        credentials: 'include',
                                        headers: {
                                            'Content-Type': 'application/json',
                                            'country': 'United Kingdom',
                                            'locale': 'en-GB',
                                            'accept': 'application/json',
                                        },
                                        body: JSON.stringify({
                                            operationName: 'searchJobCardsByLocation',
                                            query: query,
                                            variables: {searchJobRequest: vars}
                                        })
                                    });
                                    if (!r.ok) return {error: r.status};
                                    return await r.json();
                                } catch(e) {
                                    return {error: e.toString()};
                                }
                            }
                        """, variables)

                        if result and "error" not in result:
                            cards = result.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                            if cards:
                                log.info(f"💉 Smart injection got {len(cards)} jobs!")
                                captured.extend(cards)
                                session_cookies = await context.cookies()
                                break
                            else:
                                log.info(f"💉 Empty response: {str(result)[:150]}")
                        else:
                            log.warning(f"💉 Blocked: {result}")
                    except Exception as e:
                        log.warning(f"Injection error: {e}")

            await browser.close()

            for card in captured:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job

    except Exception as e:
        log.error(f"Scraper error: {e}")
        session_cookies = []  # Reset on error

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
            duration = job_type or "Seasonal"
        else:
            contract = employment or job_type or "Full-time"
            duration = "Seasonal"

        hours     = str(int(card.get("hoursPerWeek") or 0)) if card.get("hoursPerWeek") else "TBC"
        first_day = card.get("firstDayOnSite") or "TBC"
        schedule  = card.get("shiftCode") or "TBC"

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

        job = {
            "id":          job_id,
            "title":       title,
            "location":    location,
            "pay":         round(pay, 2),
            "pay_display": f"{pay:.2f}",
            "contract":    contract,
            "duration":    duration,
            "firstDay":    first_day,
            "schedule":    schedule,
            "hours":       hours,
            "link":        link,
            "found_at":    datetime.utcnow().isoformat(),
        }
        job["score"] = score_job(job)
        return job
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None

# ─── FETCH JOB DETAILS ───────────────────────────────────────────────────────
async def fetch_job_details(job):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                proxy={"server": PROXY_SERVER, "username": DECODO_USER, "password": DECODO_PASS},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            if session_cookies:
                await context.add_cookies(session_cookies)
            page = await context.new_page()
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
            content = await page.inner_text("body")

            m = re.search(r'First Day[: ]+([0-9]{4}-[0-9]{2}-[0-9]{2})', content)
            if m: job["firstDay"] = m.group(1)

            m = re.search(r'Schedule[: ]+([A-Za-z, ]+[0-9]{1,2}:[0-9]{2}[^\n]+)', content)
            if m: job["schedule"] = m.group(1).strip()[:60]

            m = re.search(r'Hours/Week[: ]+([0-9]+)', content)
            if m: job["hours"] = m.group(1)

            for ct in ["Full-time", "Part-time", "Reduced", "Flex"]:
                if ct.lower() in content.lower():
                    job["contract"] = ct
                    break

            await browser.close()
    except Exception as e:
        log.warning(f"Detail fetch error: {e}")
    return job

# ─── SESSION REFRESH ─────────────────────────────────────────────────────────
async def session_refresh_loop():
    while True:
        await asyncio.sleep(25 * 60)
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
            log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr Score:{job['score']}")
            job = await fetch_job_details(job)
            known_jobs[jid] = job
            await tg_alert(job, "new")
            asyncio.create_task(auto_navigate(job))

    if new_count == 0:
        log.info(f"👑 No new jobs — {len(known_jobs)} tracked")
    return new_count

# ─── AUTO NAVIGATION ─────────────────────────────────────────────────────────
async def auto_navigate(job):
    log.info(f"🤖 Navigating: {job['location']}")
    await tg_alert(job, "navigating")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                proxy={"server": PROXY_SERVER, "username": DECODO_USER, "password": DECODO_PASS},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            if session_cookies:
                await context.add_cookies(session_cookies)
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

            for sel in ["button:has-text('Next')", "[data-test='next-button']"]:
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

# ─── DAILY SUMMARY ───────────────────────────────────────────────────────────
async def send_daily_summary():
    while True:
        now = datetime.utcnow()
        if now.hour == 7 and now.minute == 0:
            today = [j for j in job_history if j.get("found_at","")[:10] == now.strftime("%Y-%m-%d")]
            if today:
                best    = max(today, key=lambda x: x.get("score", 0))
                avg_pay = sum(j.get("pay", 0) for j in today) / len(today)
                await tg_send(f"""📊 <b>Daily Summary</b>
━━━━━━━━━━━━━━━━━
📅 {now.strftime('%Y-%m-%d')}
🆕 Jobs: {len(today)}
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
        await tg_send("""👑 <b>Amazon KING BOT v4!</b>
⚡ Decodo Residential Proxies 🇬🇧
🍪 Real session + cookie auth
🌍 ALL UK warehouse jobs
⭐ Smart scoring
🤖 Auto-navigates application
👆 You just tap SUBMIT!
Send /scrape to check now!""")

    elif text == "/status":
        status  = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        session  = f"🍪 {len(session_cookies)} cookies" if session_cookies else "⚠️ No session"
        now      = datetime.utcnow()
        h, m     = now.hour, now.minute
        am_peak  = (h == 10 and m >= 55) or (h == 11 and m <= 25)
        pm_peak  = (h == 22 and m >= 55) or (h == 23 and m <= 25)
        if am_peak or pm_peak:
            speed = "1s ⚡ ULTRA BEAST — Peak window!"
        else:
            speed = "3s 🔥 BEAST MODE"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {status}
Proxy: ✅ Decodo GB 🇬🇧
Session: {session}
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
        await tg_send(f"{'✅ ' + str(len(session_cookies)) + ' cookies loaded!' if ok else '❌ Failed'}")

    elif text == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet!")
        else:
            txt = f"📋 <b>Last {min(5,len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                txt += f"{get_star_rating(job.get('score',0))}\n📍 {job.get('location')}\n💰 £{job.get('pay')}/hr\n\n"
            await tg_send(txt)

    elif text == "/history":
        if not job_history:
            await tg_send("📭 No history!")
        else:
            total = len(job_history)
            avg   = sum(j.get("pay",0) for j in job_history) / total
            best  = max(job_history, key=lambda x: x.get("score",0))
            await tg_send(f"📊 Total: {total} | Avg: £{avg:.2f}/hr | Best: {best.get('location','?')}")

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
            "pay": 15.30, "contract": "Reduced", "duration": "Seasonal",
            "firstDay": "2026-05-14", "schedule": "Thu, Fri, Sat 23:45-10:15",
            "hours": "30", "score": 90,
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
    log.info("👑 Amazon KING BOT v4 Starting!")

    # Build real session first — king move
    await build_session()

    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())
    asyncio.create_task(session_refresh_loop())

    await asyncio.sleep(2)
    await tg_send(f"""👑 <b>Amazon KING BOT v4 ONLINE!</b>
⚡ Decodo Residential Proxies 🇬🇧
🍪 Session: {len(session_cookies)} cookies loaded
🌍 ALL UK warehouse jobs
🤖 Auto-navigates application
👆 You just tap SUBMIT!
Send /scrape to check now!""")

    await check_jobs()

    while True:
        # 👑 KING BOT — precision timing
        now        = datetime.utcnow()
        now_hour   = now.hour
        now_minute = now.minute

        # ⚡ ULTRA BEAST: 11am UK (10:55-11:25 UTC) = 1 second
        am_peak = (now_hour == 10 and now_minute >= 55) or \
                  (now_hour == 11 and now_minute <= 25)

        # ⚡ ULTRA BEAST: 11pm UK (22:55-23:25 UTC) = 1 second
        pm_peak = (now_hour == 22 and now_minute >= 55) or \
                  (now_hour == 23 and now_minute <= 25)

        if am_peak or pm_peak:
            await asyncio.sleep(1)   # ⚡ 1 SECOND — peak windows!
        else:
            await asyncio.sleep(3)   # 🔥 3 SECONDS — all other times

        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
