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
SBR_WS    = os.environ.get("SBR_WS", "")  # Scraping Browser WebSocket URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

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
    text   = f"""🚨 <b>NEW AMAZON JOB — ACT NOW!</b>
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

# ─── SCRAPING BROWSER ────────────────────────────────────────────────────────
async def fetch_jobs():
    """Use Bright Data Scraping Browser — most powerful method!"""
    jobs = []

    if not SBR_WS:
        log.error("❌ SBR_WS not set! Add Scraping Browser URL to environment!")
        return []

    try:
        log.info("🌐 Connecting to Bright Data Scraping Browser...")
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(SBR_WS)
            log.info("✅ Connected to Scraping Browser!")

            context  = browser.contexts[0] if browser.contexts else await browser.new_context()
            page     = await context.new_page()
            captured = []

            # Intercept GraphQL responses
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

            # Navigate to Amazon Jobs
            log.info("📡 Loading Amazon Jobs page...")
            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="networkidle",
                timeout=60000
            )
            await page.wait_for_timeout(5000)

            # Scroll to trigger more jobs loading
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)

            log.info(f"📦 Captured {len(captured)} job cards")
            await browser.close()

            for card in captured:
                job = parse_card(card)
                if job:
                    jobs.append(job)

    except Exception as e:
        log.error(f"Scraping Browser error: {e}")

    log.info(f"✅ Total jobs found: {len(jobs)}")
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
    except:
        return None

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────
async def check_jobs():
    global known_jobs, active_jobs
    if bot_paused:
        return 0
    jobs      = await fetch_jobs()
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
    return len(jobs)

async def check_reminders():
    now = datetime.utcnow()
    for jid, expiry in list(active_jobs.items()):
        job  = known_jobs.get(jid)
        if not job: continue
        mins = int((expiry - now).total_seconds() / 60)
        if 28 <= mins <= 32:
            await tg_send(f"🚨 <b>FINAL WARNING!</b>\n📍 {job['location']}\n💰 £{job['pay']}/hr\n<a href='{job['link']}'>APPLY NOW →</a>")
        elif mins <= 0:
            del active_jobs[jid]

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
            jid = data.replace("applied_", "")
            if jid in active_jobs: del active_jobs[jid]
            await tg_send("✅ Applied! Good luck Yonas! 💪🔥")
        elif data.startswith("skip_"):
            jid = data.replace("skip_", "")
            if jid in active_jobs: del active_jobs[jid]
            await tg_send("⏭️ Skipped! 👀")
        return

    msg  = update.get("message", {})
    text = msg.get("text", "").strip().lower()

    if text == "/start":
        await tg_send("""🚀 <b>Amazon SUPERBOT!</b>
⚡ Bright Data Scraping Browser
🌍 ALL UK warehouse jobs
Send /scrape to check now!""")

    elif text == "/status":
        status  = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        sbr_status = "✅ Connected" if SBR_WS else "❌ Not configured"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {status}
Scraping Browser: {sbr_status}
Jobs found: {len(known_jobs)}
Active: {len(active_jobs)}
━━━━━━━━━━━━━━━━━""")

    elif text == "/scrape":
        await tg_send("🔍 <b>Scraping via Bright Data...</b>")
        count = await check_jobs()
        await tg_send(f"""✅ <b>Scrape complete!</b>
Jobs found: {count}
Total tracked: {len(known_jobs)}
{"🎉 New alerts sent!" if count > 0 else "⏳ No new jobs yet!"}""")

    elif text == "/jobs":
        if not active_jobs:
            await tg_send("📭 No active jobs. Send /scrape!")
        else:
            txt = f"📋 <b>{len(active_jobs)} Jobs:</b>\n"
            for jid in active_jobs:
                job  = known_jobs.get(jid, {})
                mins = int((active_jobs[jid] - datetime.utcnow()).total_seconds() / 60)
                txt += f"📍 {job.get('location')} · £{job.get('pay')}/hr · ⏳{mins}m\n"
            await tg_send(txt)

    elif text == "/test":
        await tg_alert({
            "id": "TEST001", "location": "Test Location UK",
            "pay": 15.30, "contract": "Full Time",
            "firstDay": "2026-05-08", "schedule": "Mon-Fri 9:00-17:00",
            "link": "https://www.jobsatamazon.co.uk",
            "expiry": datetime.utcnow() + timedelta(hours=2)
        })
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
    log.info("🚀 Amazon SUPERBOT with Scraping Browser Starting!")
    asyncio.create_task(handle_updates())
    await asyncio.sleep(2)
    await tg_send("""🚀 <b>Amazon SUPERBOT ONLINE!</b>
⚡ Bright Data Scraping Browser
🌍 ALL UK warehouse jobs
Send /scrape to check right now!""")
    await check_jobs()
    while True:
        await asyncio.sleep(30)
        await check_jobs()
        await check_reminders()

if __name__ == "__main__":
    asyncio.run(main())
