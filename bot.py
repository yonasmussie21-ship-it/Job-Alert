import asyncio
import os
import json
import logging
import aiohttp
import re
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
CHAT_ID          = os.environ.get("CHAT_ID", "1027065157")
BRIGHT_DATA_USER = os.environ.get("BRIGHT_DATA_USER", "")
BRIGHT_DATA_PASS = os.environ.get("BRIGHT_DATA_PASS", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs  = {}
active_jobs = {}
bot_paused  = False

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

async def tg_alert(job):
    expiry = job.get("expiry")
    mins   = int((expiry - datetime.utcnow()).total_seconds() / 60) if expiry else 120

    text = f"""🚨 <b>NEW AMAZON JOB — ACT NOW!</b>
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
💰 <b>£{job.get('pay', '?')}/hr</b>
⏱️ {job.get('contract', '?')}
📅 Starts: {job.get('firstDay', 'TBC')}
🕘 Shift: {job.get('schedule', 'TBC')}
⏳ <b>{mins} mins remaining</b>
━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Tap APPLY NOW instantly!</b>
━━━━━━━━━━━━━━━━━━━━━"""

    markup = {
        "inline_keyboard": [
            [{"text": "🚀 APPLY NOW", "url": job.get("link", "https://www.jobsatamazon.co.uk")}],
            [
                {"text": "✅ APPLIED", "callback_data": f"applied_{job['id']}"},
                {"text": "⏭️ SKIP",   "callback_data": f"skip_{job['id']}"}
            ]
        ]
    }
    await tg_send(text, markup)

# ─── PLAYWRIGHT SCRAPER ───────────────────────────────────────────────────────
async def fetch_jobs():
    """Scrape Amazon jobs page using real browser"""
    jobs = []
    try:
        proxy = None
        if BRIGHT_DATA_USER and BRIGHT_DATA_PASS:
            proxy = {
                "server":   f"http://brd.superproxy.io:33335",
                "username": BRIGHT_DATA_USER,
                "password": BRIGHT_DATA_PASS
            }

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )

            ctx_args = {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                "locale": "en-GB",
            }
            if proxy:
                ctx_args["proxy"] = proxy

            context = await browser.new_context(**ctx_args)
            page    = await context.new_page()

            # Intercept GraphQL responses
            captured_jobs = []

            async def handle_response(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        data      = await response.json()
                        job_cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                        if job_cards:
                            log.info(f"✅ Intercepted {len(job_cards)} jobs from GraphQL!")
                            captured_jobs.extend(job_cards)
                except:
                    pass

            page.on("response", handle_response)

            # Visit Amazon jobs search page
            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle",
                timeout=30000
            )
            await page.wait_for_timeout(5000)

            # Try clicking "Search" or scrolling to trigger more loads
            try:
                await page.keyboard.press("End")
                await page.wait_for_timeout(2000)
            except:
                pass

            await browser.close()

            # Parse captured jobs
            for card in captured_jobs:
                job = parse_card(card)
                if job:
                    jobs.append(job)

            # If no jobs from interception try direct API with session
            if not jobs:
                jobs = await fetch_via_api()

            log.info(f"📦 Total jobs found: {len(jobs)}")

    except Exception as e:
        log.error(f"Scraper error: {e}")
        jobs = await fetch_via_api()

    return jobs

async def fetch_via_api():
    """Try Amazon GraphQL API with different search terms"""
    jobs = []
    search_terms = ["warehouse operative", "warehouse", "pick pack ship", ""]

    for term in search_terms:
        try:
            payload = {
                "operationName": "searchJobCardsByLocation",
                "query": """query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {
                  searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {
                    nextToken
                    jobCards {
                      jobId jobTitle jobType employmentType
                      city state postalCode locationName
                      totalPayRateMin totalPayRateMax
                      __typename
                    }
                    __typename
                  }
                }""",
                "variables": {
                    "searchJobRequest": {
                        "locale": "en-GB",
                        "country": "United Kingdom",
                        "keyWords": term,
                        "equalFilters": [],
                        "containFilters": [],
                        "pageSize": 100,
                        "sortBy": "RELEVANCE",
                        "descending": True
                    }
                }
            }

            headers = {
                "accept":         "*/*",
                "accept-language": "en-GB,en;q=0.9",
                "content-type":   "application/json",
                "country":        "United Kingdom",
                "origin":         "https://www.jobsatamazon.co.uk",
                "referer":        "https://www.jobsatamazon.co.uk/",
                "user-agent":     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://www.jobsatamazon.co.uk/graphql",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                    ssl=False
                ) as resp:
                    if resp.status == 200:
                        data      = await resp.json()
                        job_cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                        log.info(f"API '{term}': {len(job_cards)} jobs")
                        for card in job_cards:
                            job = parse_card(card)
                            if job and job["id"] not in [j["id"] for j in jobs]:
                                jobs.append(job)
                        if jobs:
                            break
        except Exception as e:
            log.warning(f"API error for '{term}': {e}")
            continue

    return jobs

def parse_card(card):
    try:
        job_id = str(card.get("jobId", ""))
        if not job_id:
            return None

        city     = card.get("city", card.get("locationName", "Unknown"))
        postcode = card.get("postalCode", "")
        pay      = float(card.get("totalPayRateMax") or card.get("totalPayRateMin") or 0)
        contract = card.get("employmentType", card.get("jobType", ""))
        location = f"{city} {postcode}".strip()
        link     = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}&locale=en-GB&recommended=1&intcmpid=searchalljobsleft"

        return {
            "id":       job_id,
            "location": location,
            "pay":      round(pay, 2),
            "contract": contract,
            "firstDay": "TBC",
            "schedule": "TBC",
            "link":     link,
            "found_at": datetime.utcnow().isoformat(),
            "expiry":   datetime.utcnow() + timedelta(hours=2)
        }
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────
async def check_jobs():
    global known_jobs, active_jobs
    if bot_paused:
        return

    jobs = await fetch_jobs()
    new_count = 0
    for job in jobs:
        jid = job["id"]
        if jid not in known_jobs:
            known_jobs[jid] = job
            active_jobs[jid] = job["expiry"]
            new_count += 1
            log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr")
            await tg_alert(job)

    if new_count == 0:
        log.info(f"✅ No new jobs — tracking {len(known_jobs)} total")

async def check_reminders():
    now = datetime.utcnow()
    for jid, expiry in list(active_jobs.items()):
        job  = known_jobs.get(jid)
        if not job: continue
        mins = int((expiry - now).total_seconds() / 60)
        if 28 <= mins <= 32:
            await tg_send(f"🚨 <b>FINAL WARNING — 30 mins left!</b>\n📍 {job['location']}\n💰 £{job['pay']}/hr\n<a href='{job['link']}'>APPLY NOW →</a>")
        elif mins <= 0:
            del active_jobs[jid]

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
    global bot_paused

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
            await tg_send("⏭️ Skipped! Watching for next... 👀")
        return

    msg  = update.get("message", {})
    text = msg.get("text", "").strip().lower()

    if text == "/start":
        await tg_send("""🚀 <b>Amazon Shift Holder SUPERBOT!</b>
━━━━━━━━━━━━━━━━━━━━━
⚡ Real browser scraping
🌍 ALL UK warehouse jobs
🚨 Every alert = URGENT
🔄 Checking every 30 seconds
━━━━━━━━━━━━━━━━━━━━━
Send /test to verify!""")

    elif text == "/status":
        status = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━━━
Status: {status}
Watching: ALL UK jobs 🌍
Jobs found: {len(known_jobs)}
Active: {len(active_jobs)}
━━━━━━━━━━━━━━━━━━━""")

    elif text == "/jobs":
        if not active_jobs:
            await tg_send("📭 No active jobs right now. Bot watching... 👀")
        else:
            txt = f"📋 <b>{len(active_jobs)} Active Jobs:</b>\n"
            for jid in active_jobs:
                job  = known_jobs.get(jid, {})
                mins = int((active_jobs[jid] - datetime.utcnow()).total_seconds() / 60)
                txt += f"📍 {job.get('location')} · £{job.get('pay')}/hr · ⏳{mins}mins\n"
            await tg_send(txt)

    elif text == "/pause":
        bot_paused = True
        await tg_send("⏸️ Bot paused. Send /resume to restart.")

    elif text == "/resume":
        bot_paused = False
        await tg_send("▶️ Bot resumed! 🔥")

    elif text == "/test":
        await tg_alert({
            "id": "TEST001",
            "location": "Test Location UK",
            "pay": 15.30,
            "contract": "Full Time",
            "firstDay": "2026-05-08",
            "schedule": "Mon-Fri 9:00-17:00",
            "link": "https://www.jobsatamazon.co.uk",
            "expiry": datetime.utcnow() + timedelta(hours=2)
        })
        await tg_send("✅ <b>Test complete — Bot is working!</b>")

    elif text == "/scrape":
        await tg_send("🔍 <b>Forcing scrape NOW...</b>")
        await check_jobs()
        await tg_send(f"✅ Scrape done! Jobs found: {len(known_jobs)}")

    elif text == "/help":
        await tg_send("""🤖 <b>Commands</b>
/start   /status  /jobs
/test    /scrape  /pause   /resume  /help""")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Amazon SUPERBOT Starting!")
    asyncio.create_task(handle_updates())

    await asyncio.sleep(2)
    await tg_send("""🚀 <b>Amazon SUPERBOT ONLINE!</b>
━━━━━━━━━━━━━━━━━━━━━
⚡ Real browser scraping
🌍 ALL UK warehouse jobs
🚨 Instant alerts
━━━━━━━━━━━━━━━━━━━━━
Send /test to verify!""")

    # First run immediately
    await check_jobs()

    # Then check every 30 seconds
    # (Playwright browser takes ~10-15 secs per check)
    while True:
        await check_reminders()
        await asyncio.sleep(30)
        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
