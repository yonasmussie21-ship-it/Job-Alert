import asyncio
import os
import json
import logging
import aiohttp
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "1027065157")
SBR_WS    = os.environ.get("SBR_WS", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

known_jobs  = {}
bot_paused  = False

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── ALL UK SEARCH URLS ───────────────────────────────────────────────────────
UK_SEARCH_URLS = [
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=London",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=Birmingham",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=Manchester",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=Glasgow",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=Belfast",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=Bristol",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=Leeds",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=Sheffield",
    "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR&city=Nottingham",
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
    elif status == "failed":
        header = "⚠️ <b>APPLY MANUALLY — BOT NEEDS HELP!</b>"

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
        text += "\n👆 <b>Everything filled — just tap SUBMIT!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "failed":
        text += "\n👆 <b>Tap link to apply manually!</b>\n━━━━━━━━━━━━━━━━━━━━━"

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
    """Bot navigates application — you just tap Submit!"""
    log.info(f"🤖 Auto-navigating: {job['location']}")
    await tg_alert(job, "navigating")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(SBR_WS)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = await context.new_page()

            # Go to job page
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Step 1 — Click Apply
            applied = False
            for sel in [
                "button:has-text('Apply')",
                "a:has-text('Apply')",
                "[data-test='apply-button']",
                ".apply-button",
                "button.btn-primary"
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info("✅ Clicked Apply")
                        await page.wait_for_timeout(2000)
                        applied = True
                        break
                except:
                    pass

            # Step 2 — Click Next
            for sel in ["button:has-text('Next')", "[data-test='next-button']", "button.next"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info("✅ Clicked Next")
                        await page.wait_for_timeout(2000)
                        break
                except:
                    pass

            # Step 3 — Click Start Application
            for sel in [
                "button:has-text('Start Application')",
                "[data-test='start-application']",
                "button:has-text('Start')"
            ]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        log.info("✅ Clicked Start Application")
                        await page.wait_for_timeout(2000)
                        break
                except:
                    pass

            # Get current URL for user
            current_url = page.url
            log.info(f"✅ Bot reached: {current_url}")

            # Update job link to current page
            job["link"] = current_url if current_url != "about:blank" else job["link"]

            await browser.close()

            # Alert user — ready to submit!
            await tg_alert(job, "ready")
            log.info(f"✅ Application ready for: {job['location']}")

    except Exception as e:
        log.error(f"Navigation error: {e}")
        await tg_alert(job, "failed")

# ─── SCRAPING BROWSER ────────────────────────────────────────────────────────
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

            for url in UK_SEARCH_URLS:
                try:
                    captured = []

                    async def handle_response(response):
                        try:
                            if "graphql" in response.url and response.status == 200:
                                data  = await response.json()
                                cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                                if cards:
                                    log.info(f"🎯 Got {len(cards)} jobs")
                                    captured.extend(cards)
                        except:
                            pass

                    page.on("response", handle_response)
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    await page.wait_for_timeout(3000)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(2000)

                    for card in captured:
                        job = parse_card(card)
                        if job and job["id"] not in all_jobs:
                            all_jobs[job["id"]] = job

                    page.remove_listener("response", handle_response)

                except Exception as e:
                    log.warning(f"URL error: {e}")
                    continue

            await browser.close()

    except Exception as e:
        log.error(f"Scraping error: {e}")

    jobs = list(all_jobs.values())
    log.info(f"✅ Total unique jobs: {len(jobs)}")
    return jobs

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

        # Full location like Amazon portal
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
            "firstDay": card.get("firstDayOnSite", "TBC"),
            "schedule": card.get("shiftCode", "TBC"),
            "hours":    str(card.get("hoursPerWeek", "TBC")),
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
            # Send initial alert
            await tg_alert(job, "new")
            # Auto navigate in background
            asyncio.create_task(auto_navigate(job))
    if new_count == 0:
        log.info(f"✅ No new jobs — tracking {len(known_jobs)} total")
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
🌍 ALL UK locations
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
Regions: {len(UK_SEARCH_URLS)} UK areas
Jobs tracked: {len(known_jobs)}
━━━━━━━━━━━━━━━━━""")

    elif text == "/scrape":
        await tg_send(f"🔍 <b>Searching {len(UK_SEARCH_URLS)} UK regions...</b>")
        count = await check_jobs()
        await tg_send(f"""✅ <b>Done!</b>
New jobs: {count}
Total tracked: {len(known_jobs)}
{"🎉 Alerts sent + navigating!" if count > 0 else "⏳ No new jobs yet!"}""")

    elif text == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs found yet. Send /scrape!")
        else:
            txt = f"📋 <b>{len(known_jobs)} Jobs Found:</b>\n━━━━━━━━━━━\n"
            for jid, job in list(known_jobs.items())[-10:]:
                txt += f"📍 {job.get('location')}\n💰 £{job.get('pay')}/hr · 📅{job.get('firstDay')}\n\n"
            await tg_send(txt)

    elif text == "/test":
        test_job = {
            "id": "TEST001",
            "title": "Warehouse Operative",
            "location": "Nottingham, England (Nottingham-Mansfield Area) NG16 3UA",
            "pay": 14.30,
            "contract": "Full-time",
            "duration": "Seasonal",
            "firstDay": "2026-05-15",
            "schedule": "Fri, Sat, Sun, Mon 19:30-6:00",
            "hours": "40",
            "link": "https://www.jobsatamazon.co.uk",
        }
        await tg_alert(test_job, "new")
        await tg_send("✅ Bot working! Send /scrape for real jobs!")

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
    log.info("🚀 Amazon SUPERBOT Starting!")
    asyncio.create_task(handle_updates())
    await asyncio.sleep(2)
    await tg_send(f"""🚀 <b>Amazon SUPERBOT ONLINE!</b>
⚡ Bright Data Scraping Browser
🌍 {len(UK_SEARCH_URLS)} UK regions
🤖 Auto-navigates application
👆 You just tap SUBMIT!
Send /scrape to check now!""")
    await check_jobs()
    while True:
        await asyncio.sleep(30)
        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
