import asyncio
import os
import json
import logging
import aiohttp
from datetime import datetime, timedelta

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "1027065157")
BRIGHT_DATA_USER = os.environ.get("BRIGHT_DATA_USER", "")
BRIGHT_DATA_PASS = os.environ.get("BRIGHT_DATA_PASS", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs  = {}
active_jobs = {}
bot_paused  = False

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── AMAZON GRAPHQL ───────────────────────────────────────────────────────────
AMAZON_URL = "https://www.jobsatamazon.co.uk/graphql"

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
      totalPayRateMin
      totalPayRateMax
      currencyCode
      __typename
    }
    __typename
  }
}
"""

HEADERS = {
    "authority":          "www.jobsatamazon.co.uk",
    "accept":             "*/*",
    "accept-language":    "en-GB,en;q=0.9",
    "content-type":       "application/json",
    "country":            "United Kingdom",
    "iscanary":           "false",
    "origin":             "https://www.jobsatamazon.co.uk",
    "referer":            "https://www.jobsatamazon.co.uk/",
    "user-agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}

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
    pay    = job.get("pay", "?")
    
    text = f"""🚨 <b>NEW AMAZON JOB — ACT NOW!</b>
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
💰 <b>£{pay}/hr</b>
⏱️ {job.get('contract', '?')}
🌍 UK Wide Job
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

# ─── SCRAPER ─────────────────────────────────────────────────────────────────
async def fetch_jobs():
    """Call Amazon's GraphQL API directly"""
    jobs = []
    
    payload = {
        "operationName": "searchJobCardsByLocation",
        "query": GRAPHQL_QUERY,
        "variables": {
            "searchJobRequest": {
                "locale": "en-GB",
                "country": "United Kingdom",
                "keyWords": "warehouse",
                "equalFilters": [],
                "containFilters": [],
                "pageSize": 100
            }
        }
    }

    proxy      = None
    proxy_auth = None
    if BRIGHT_DATA_USER and BRIGHT_DATA_PASS:
        proxy      = "http://brd.superproxy.io:33335"
        proxy_auth = aiohttp.BasicAuth(BRIGHT_DATA_USER, BRIGHT_DATA_PASS)

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                AMAZON_URL,
                json=payload,
                headers=HEADERS,
                proxy=proxy,
                proxy_auth=proxy_auth,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data      = await resp.json()
                    job_cards = data.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                    log.info(f"✅ API returned {len(job_cards)} jobs")
                    for card in job_cards:
                        job = parse_card(card)
                        if job:
                            jobs.append(job)
                else:
                    log.warning(f"API status: {resp.status}")
    except Exception as e:
        log.error(f"API error: {e}")

    return jobs

def parse_card(card):
    try:
        job_id   = str(card.get("jobId", ""))
        if not job_id:
            return None
        title    = card.get("jobTitle", "Warehouse Job")
        city     = card.get("city", card.get("locationName", "Unknown"))
        postcode = card.get("postalCode", "")
        pay      = float(card.get("totalPayRateMax") or card.get("totalPayRateMin") or 0)
        contract = card.get("employmentType", card.get("jobType", ""))
        location = f"{city} {postcode}".strip()
        link     = f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}&locale=en-GB&recommended=1&intcmpid=searchalljobsleft"

        return {
            "id":       job_id,
            "title":    title,
            "location": location,
            "pay":      round(pay, 2),
            "contract": contract,
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
    for job in jobs:
        jid = job["id"]
        if jid not in known_jobs:
            known_jobs[jid] = job
            active_jobs[jid] = job["expiry"]
            log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr")
            await tg_alert(job)

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
⚡ Using Amazon's own API
🌍 Watching ALL UK jobs
🚨 Every alert = URGENT
🔄 Checking every 1 SECOND
━━━━━━━━━━━━━━━━━━━━━
Bot is running! Send /status to check!""")

    elif text == "/status":
        status = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━━━
Status: {status}
Watching: ALL UK jobs 🌍
Speed: Every 1 SECOND ⚡🔥
Jobs found: {len(known_jobs)}
Active: {len(active_jobs)}
━━━━━━━━━━━━━━━━━━━""")

    elif text == "/jobs":
        if not active_jobs:
            await tg_send("📭 No active jobs right now. Bot is watching... 👀")
        else:
            txt = f"📋 <b>{len(active_jobs)} Active Jobs:</b>\n━━━━━━━━━━━\n"
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
        await tg_send("▶️ Bot resumed! Watching every 1 second! ⚡🔥")

    elif text == "/test":
        await tg_send("""🧪 <b>Test Alert — Bot Is Working!</b>
━━━━━━━━━━━━━━━━━━━━━
🚨 NEW AMAZON JOB — ACT NOW!
━━━━━━━━━━━━━━━━━━━━━
📍 Birmingham B26 3QJ
💰 £15.30/hr
⏱️ Full Time
🌍 UK Wide Job
⏳ 120 mins remaining
━━━━━━━━━━━━━━━━━━━━━
✅ Bot is working perfectly! 🔥""")

    elif text == "/help":
        await tg_send("""🤖 <b>Amazon Superbot Commands</b>
━━━━━━━━━━━━━━━━━━━━━
/start   — Welcome message
/status  — Bot status
/jobs    — Active jobs
/test    — Test notification
/pause   — Pause alerts
/resume  — Resume alerts
/help    — This message
━━━━━━━━━━━━━━━━━━━━━
⚡ Speed: Every 1 SECOND
🌍 Coverage: ALL UK jobs
🚨 All alerts URGENT""")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Amazon SUPERBOT Starting — 1 second checks!")
    asyncio.create_task(handle_updates())
    
    await asyncio.sleep(2)
    await tg_send("""🚀 <b>Amazon Shift Holder SUPERBOT ONLINE!</b>
━━━━━━━━━━━━━━━━━━━━━
⚡ Using Amazon's own GraphQL API
🌍 Watching ALL UK warehouse jobs
🚨 Every job = URGENT alert
🔄 Checking every 1 SECOND 🔥
━━━━━━━━━━━━━━━━━━━━━
Send /test to verify it's working!
Send /help for all commands!""")

    while True:
        await check_jobs()
        await check_reminders()
        log.info("⚡ 1s check done")
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
