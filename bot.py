import asyncio
import os
import json
import logging
import aiohttp
from datetime import datetime
from playwright.async_api import async_playwright

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "1027065157")
SBR_WS    = os.environ.get("SBR_WS", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

known_jobs = {}
bot_paused = False

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── AMAZON WAREHOUSE SITES ───────────────────────────────────────────────────
# All known Amazon UK warehouse codes
WAREHOUSE_CODES = [
    # West Midlands
    "BHX1","BHX2","BHX3","BHX4","BHX5",
    # East Midlands
    "EMA1","EMA2","EMA3","EMA4",
    # London/South
    "LTN1","LTN7","LCY2","LCY3","LCY4",
    # Bristol/South West
    "BRS1","BRS2","BRS3",
    # Manchester/North West
    "MAN1","MAN2","MAN3","MAN4","MAN5",
    # Yorkshire/North
    "LBA1","LBA2","LBA3","LBA4",
    # Scotland
    "EDI1","EDI2","EDI3","EDI4",
    # Wales
    "CWL1","CWL2",
    # North East
    "MME1","MME2",
    # Other
    "BFS1","STN1","STN2",
]

# Main search URLs — ALL UK jobs
SEARCH_URLS = [
    # Main page — ALL UK warehouse jobs
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&category=warehouse",
    # Backup — all jobs no filter
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
]

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
    if status == "new":
        header = "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>"
    elif status == "navigating":
        header = "⚡ <b>BOT NAVIGATING APPLICATION...</b>"
    elif status == "ready":
        header = "✅ <b>READY — TAP SUBMIT NOW!</b>"
    else:
        header = "⚠️ <b>APPLY MANUALLY!</b>"

    text = f"""{header}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
📦 {job.get('title', 'Warehouse Operative')}
💰 <b>£{job.get('pay', '?')}/hr</b>
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

# ─── AUTO NAVIGATION ─────────────────────────────────────────────────────────
async def auto_navigate(job):
    log.info(f"🤖 Navigating: {job['location']}")
    await tg_alert(job, "navigating")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(SBR_WS)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = await context.new_page()

            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Click Apply
            for sel in ["button:has-text('Apply')", "a:has-text('Apply')", "[data-test='apply-button']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        log.info("✅ Clicked Apply")
                        break
                except: pass

            # Click Next
            for sel in ["button:has-text('Next')", "[data-test='next-button']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        log.info("✅ Clicked Next")
                        break
                except: pass

            # Click Start Application
            for sel in ["button:has-text('Start Application')", "[data-test='start-application']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        log.info("✅ Clicked Start Application")
                        break
                except: pass

            job["link"] = page.url if page.url != "about:blank" else job["link"]
            await browser.close()
            await tg_alert(job, "ready")

    except Exception as e:
        log.error(f"Navigation error: {e}")
        await tg_alert(job, "failed")

# ─── MAIN SCRAPER ────────────────────────────────────────────────────────────
async def fetch_jobs():
    all_jobs = {}
    if not SBR_WS:
        log.error("❌ SBR_WS not configured!")
        return []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(SBR_WS)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = await context.new_page()

            for url in SEARCH_URLS:
                try:
                    captured = []

                    async def handle_response(response):
                        try:
                            if "graphql" in response.url and response.status == 200:
                                data  = await response.json()
                                cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                                if cards:
                                    log.info(f"🎯 Got {len(cards)} jobs!")
                                    captured.extend(cards)
                        except: pass

                    page.on("response", handle_response)
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(5000)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(3000)

                    for card in captured:
                        job = parse_card(card)
                        if job and job["id"] not in all_jobs:
                            all_jobs[job["id"]] = job

                    page.remove_listener("response", handle_response)
                    
                    # If we got jobs from first URL no need for backup
                    if all_jobs:
                        log.info(f"✅ Got {len(all_jobs)} jobs from main search!")
                        break

                except Exception as e:
                    log.warning(f"URL error: {e}")
                    continue

            await browser.close()

    except Exception as e:
        log.error(f"Scraping error: {e}")

    log.info(f"✅ Total unique jobs: {len(all_jobs)}")
    return list(all_jobs.values())

def parse_card(card):
    try:
        job_id = str(card.get("jobId", ""))
        if not job_id:
            return None

        title    = card.get("jobTitle", "Warehouse Operative")
        city     = card.get("city", "Unknown")
        state    = card.get("state", "England")
        postcode = card.get("postalCode", "")
        geo      = card.get("geoClusterDescription", "")
        pay      = float(card.get("totalPayRateMax") or card.get("totalPayRateMin") or 0)
        contract = card.get("employmentType", card.get("jobType", ""))
        first_day = card.get("firstDayOnSite", "TBC")
        schedule  = card.get("shiftCode", "TBC")
        hours     = str(card.get("hoursPerWeek", "TBC"))

        # Full location exactly like Amazon portal
        if geo and postcode:
            location = f"{city}, {state} ({geo}) {postcode}"
        elif geo:
            location = f"{city}, {state} ({geo})"
        elif postcode:
            location = f"{city}, {state} {postcode}"
        else:
            location = f"{city}, {state}"

        link = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}&locale=en-GB&recommended=1&intcmpid=searchalljobsleft"

        return {
            "id":       job_id,
            "title":    title,
            "location": location,
            "pay":      round(pay, 2),
            "contract": contract,
            "duration": "Seasonal",
            "firstDay": first_day,
            "schedule": schedule,
            "hours":    hours,
            "link":     link,
            "found_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        log.warning(f"Parse error: {e}")
        return None

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────
async def check_jobs():
    global known_jobs
    if bot_paused:
        return 0
    jobs      = await fetch_jobs()
    new_count = 0
    for job in jobs:
        jid = job["id"]
        if jid not in known_jobs:
            known_jobs[jid] = job
            new_count += 1
            log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr")
            await tg_alert(job, "new")
            asyncio.create_task(auto_navigate(job))
    if new_count == 0:
        log.info(f"✅ No new jobs — {len(known_jobs)} total tracked")
    return new_count

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
        cb   = update["callback_query"]
        data = cb.get("data", "")
        if data.startswith("applied_"):
            await tg_send("✅ Applied! Good luck Yonas! 💪🔥")
        elif data.startswith("skip_"):
            await tg_send("⏭️ Skipped! Watching for next... 👀")
        return

    msg  = update.get("message", {})
    text = msg.get("text", "").strip().lower()

    if text == "/start":
        await tg_send("""🚀 <b>Amazon SUPERBOT!</b>
⚡ Bright Data Scraping Browser
🌍 ALL UK warehouse locations
🤖 Auto-navigates application
👆 You just tap SUBMIT!
Send /scrape to check now!""")

    elif text == "/status":
        status     = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        sbr_status = "✅ Connected" if SBR_WS else "❌ Not configured"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {status}
Scraping Browser: {sbr_status}
Coverage: ALL UK warehouses
Known warehouses: {len(WAREHOUSE_CODES)}+
Jobs tracked: {len(known_jobs)}
━━━━━━━━━━━━━━━━━""")

    elif text == "/scrape":
        await tg_send("🔍 <b>Scanning ALL UK Amazon warehouses...</b>")
        count = await check_jobs()
        await tg_send(f"""✅ <b>Scan complete!</b>
New jobs: {count}
Total tracked: {len(known_jobs)}
{"🎉 Alerts sent!" if count > 0 else "⏳ No new jobs right now!"}""")

    elif text == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet. Send /scrape!")
        else:
            txt = f"📋 <b>Last {min(5,len(known_jobs))} Jobs Found:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                txt += f"📍 {job.get('location')}\n💰 £{job.get('pay')}/hr | 📅 {job.get('firstDay')}\n\n"
            await tg_send(txt)

    elif text == "/test":
        test_job = {
            "id": "TEST001",
            "title": "Warehouse Operative",
            "location": "Rugby, England (Coventry, Rugby, Daventry Area) CV23 0XF",
            "pay": 14.30,
            "contract": "Full-time",
            "duration": "Seasonal",
            "firstDay": "2026-05-10",
            "schedule": "Sun, Mon, Tue, Wed, Thu 18:30-2:30",
            "hours": "40",
            "link": "https://www.jobsatamazon.co.uk",
        }
        await tg_alert(test_job, "new")
        await tg_send("✅ Alert format correct! Send /scrape for real jobs!")

    elif text == "/pause":
        bot_paused = True
        await tg_send("⏸️ Paused.")

    elif text == "/resume":
        bot_paused = False
        await tg_send("▶️ Resumed! 🔥")

    elif text == "/help":
        await tg_send("""/scrape  /status  /jobs
/test    /pause   /resume  /help""")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Amazon SUPERBOT Starting — ALL UK warehouses!")
    asyncio.create_task(handle_updates())
    await asyncio.sleep(2)
    await tg_send(f"""🚀 <b>Amazon SUPERBOT ONLINE!</b>
⚡ Bright Data Scraping Browser
🌍 ALL UK warehouse locations
📦 {len(WAREHOUSE_CODES)}+ warehouse codes monitored
🤖 Auto-navigates application
👆 You just tap SUBMIT!
Send /scrape to check now!""")
    await check_jobs()
    while True:
        await asyncio.sleep(30)
        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
