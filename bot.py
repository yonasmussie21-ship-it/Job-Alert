import asyncio
import os
import json
import logging
import aiohttp
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
from collections import defaultdict

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
CHAT_ID      = os.environ.get("CHAT_ID", "1027065157")
DECODO_USER  = os.environ.get("DECODO_USER", "")
DECODO_PASS  = os.environ.get("DECODO_PASS", "")
DECODO_HOST  = os.environ.get("DECODO_HOST", "gb.decodo.com")
DECODO_PORT  = os.environ.get("DECODO_PORT", "30000")

PROXY_SERVER = f"http://{DECODO_HOST}:{DECODO_PORT}"
PROXY_AUTH   = {"username": DECODO_USER, "password": DECODO_PASS}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── STATE ───────────────────────────────────────────────────────────────────
known_jobs    = {}
bot_paused    = False
job_history   = []
posting_times = defaultdict(list)

TELEGRAM_API  = f"https://api.telegram.org/bot{BOT_TOKEN}"

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
    score = 0
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

    pay_str = job.get('pay_display') or f"{job.get('pay', '?'):.2f}"
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

# ─── SCRAPER (Decodo Residential Proxy) ──────────────────────────────────────
async def fetch_jobs():
    all_jobs = {}

    if not DECODO_USER or not DECODO_PASS:
        log.error("❌ Decodo credentials not configured!")
        return []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(
                proxy={
                    "server": PROXY_SERVER,
                    "username": DECODO_USER,
                    "password": DECODO_PASS,
                },
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
            )
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
                timeout=60000
            )
            await page.wait_for_timeout(5000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)

            # If no intercept, try GraphQL injection
            if not captured:
                log.info("💉 Injecting GraphQL call...")
                for variables in [
                    {"locale": "en-GB", "country": "United Kingdom", "keyWords": "warehouse operative", "equalFilters": [], "containFilters": [], "pageSize": 100},
                    {"locale": "en-GB", "country": "United Kingdom", "keyWords": "", "equalFilters": [], "containFilters": [], "pageSize": 100}
                ]:
                    try:
                        result = await page.evaluate(f"""
                            async () => {{
                                const r = await fetch('/graphql', {{
                                    method: 'POST',
                                    headers: {{'Content-Type': 'application/json', 'country': 'United Kingdom', 'accept': '*/*'}},
                                    body: JSON.stringify({{
                                        operationName: 'searchJobCardsByLocation',
                                        query: `{GRAPHQL_QUERY.replace('`', '')}`,
                                        variables: {{searchJobRequest: {json.dumps(variables)}}}
                                    }})
                                }});
                                return await r.json();
                            }}
                        """)
                        cards = result.get("data", {}).get("searchJobCardsByLocation", {}).get("jobCards", [])
                        if cards:
                            log.info(f"💉 Got {len(cards)} jobs via injection!")
                            captured.extend(cards)
                            break
                    except Exception as e:
                        log.warning(f"Injection error: {e}")

            await browser.close()

            for card in captured:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job

    except Exception as e:
        log.error(f"Scraper error: {e}")

    log.info(f"✅ Total jobs: {len(all_jobs)}")
    return list(all_jobs.values())

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

        pay_display = f"{pay:.2f}"
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
            "pay_display": pay_display,
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

# ─── FETCH FULL JOB DETAILS ──────────────────────────────────────────────────
async def fetch_job_details(job):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(
                proxy={
                    "server": PROXY_SERVER,
                    "username": DECODO_USER,
                    "password": DECODO_PASS,
                },
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
            )
            page = await context.new_page()

            async def handle_response(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        data = await response.json()
                        schedules = (data.get("data", {}).get("getSchedules") or
                                    data.get("data", {}).get("jobSchedules") or
                                    data.get("data", {}).get("schedules"))
                        if schedules:
                            pass
                        detail = (data.get("data", {}).get("getJobDetails") or
                                 data.get("data", {}).get("jobDetail"))
                        if detail:
                            pass
                except:
                    pass

            page.on("response", handle_response)
            await page.goto(job["link"], wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(4000)

            import re
            content_text = await page.inner_text("body")

            day_match = re.search(r'First Day[: ]+([0-9]{4}-[0-9]{2}-[0-9]{2})', content_text)
            if day_match:
                job["firstDay"] = day_match.group(1)

            sched_match = re.search(r'Schedule[: ]+([A-Za-z, ]+[0-9]{1,2}:[0-9]{2}[^\n]+)', content_text)
            if sched_match:
                job["schedule"] = sched_match.group(1).strip()[:60]

            hours_match = re.search(r'Hours/Week[: ]+([0-9]+)', content_text)
            if hours_match:
                job["hours"] = hours_match.group(1)

            for ct in ["Full-time", "Part-time", "Reduced", "Flex"]:
                if ct.lower() in content_text.lower():
                    job["contract"] = ct
                    break

            await browser.close()
            log.info(f"✅ Details fetched: {job.get('firstDay')} | {job.get('schedule','TBC')[:30]}")

    except Exception as e:
        log.warning(f"Detail fetch error: {e}")

    return job

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────
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
            hour = datetime.utcnow().hour
            posting_times[job["location"][:20]].append(hour)

            log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr Score:{job['score']}")

            job = await fetch_job_details(job)
            known_jobs[jid] = job

            await tg_alert(job, "new")
            asyncio.create_task(auto_navigate(job))

    if new_count == 0:
        log.info(f"✅ No new jobs — {len(known_jobs)} tracked")
    return new_count

# ─── AUTO NAVIGATION ─────────────────────────────────────────────────────────
async def auto_navigate(job):
    log.info(f"🤖 Navigating: {job['location']}")
    await tg_alert(job, "navigating")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(
                proxy={
                    "server": PROXY_SERVER,
                    "username": DECODO_USER,
                    "password": DECODO_PASS,
                },
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
                timezone_id="Europe/London",
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
                        log.info("✅ Clicked Apply")
                        break
                except: pass

            for sel in ["button:has-text('Next')", "[data-test='next-button']"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(2000)
                        log.info("✅ Clicked Next")
                        break
                except: pass

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

# ─── DAILY SUMMARY ───────────────────────────────────────────────────────────
async def send_daily_summary():
    while True:
        now = datetime.utcnow()
        if now.hour == 7 and now.minute == 0:
            today_jobs = [j for j in job_history
                         if j.get("found_at", "")[:10] == now.strftime("%Y-%m-%d")]
            if today_jobs:
                best    = max(today_jobs, key=lambda x: x.get("score", 0))
                avg_pay = sum(j.get("pay", 0) for j in today_jobs) / len(today_jobs)
                await tg_send(f"""📊 <b>Daily Summary</b>
━━━━━━━━━━━━━━━━━
📅 {now.strftime('%Y-%m-%d')}
🆕 Jobs found: {len(today_jobs)}
💰 Avg pay: £{avg_pay:.2f}/hr
⭐ Best job: {best.get('location', 'Unknown')} £{best.get('pay', '?')}/hr
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
        cb   = update["callback_query"]
        data = cb.get("data", "")
        if data.startswith("applied_"):
            await tg_send("✅ Applied! Good luck Yonas! 💪🔥")
        elif data.startswith("skip_"):
            await tg_send("⏭️ Skipped! 👀")
        return

    msg  = update.get("message", {})
    text = msg.get("text", "").strip().lower()

    if text == "/start":
        await tg_send("""🚀 <b>Amazon SUPERBOT v3 — Decodo Edition!</b>
⚡ Decodo Residential Proxies
🌍 ALL UK warehouse jobs
⭐ Smart job scoring
📊 Daily summaries
🤖 Auto-navigates application
👆 You just tap SUBMIT!
Send /scrape to check now!""")

    elif text == "/status":
        status       = "⏸️ PAUSED" if bot_paused else "✅ RUNNING"
        proxy_status = "✅ Decodo Connected" if DECODO_USER else "❌ Not configured"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {status}
Proxy: {proxy_status}
Provider: 🔵 Decodo Residential
Location: Great Britain 🇬🇧
Jobs tracked: {len(known_jobs)}
Total history: {len(job_history)}
Check speed: every 3 seconds ⚡
━━━━━━━━━━━━━━━━━""")

    elif text == "/scrape":
        await tg_send("🔍 <b>Scanning ALL UK Amazon jobs...</b>")
        count = await check_jobs()
        await tg_send(f"""✅ <b>Scan complete!</b>
New jobs: {count}
Total tracked: {len(known_jobs)}
{"🎉 Alerts sent!" if count > 0 else "⏳ No new jobs right now!"}""")

    elif text == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet. Send /scrape!")
        else:
            txt = f"📋 <b>Last {min(5,len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                stars = get_star_rating(job.get("score", 0))
                txt += f"{stars}\n📍 {job.get('location')}\n💰 £{job.get('pay')}/hr | 📅{job.get('firstDay')}\n\n"
            await tg_send(txt)

    elif text == "/history":
        if not job_history:
            await tg_send("📭 No history yet!")
        else:
            total = len(job_history)
            avg   = sum(j.get("pay", 0) for j in job_history) / total
            best  = max(job_history, key=lambda x: x.get("score", 0))
            await tg_send(f"""📊 <b>Job History</b>
━━━━━━━━━━━━━━━━━
Total found: {total}
Avg pay: £{avg:.2f}/hr
Best: {best.get('location', '?')} £{best.get('pay', '?')}/hr
━━━━━━━━━━━━━━━━━""")

    elif text == "/predict":
        if not posting_times:
            await tg_send("📭 Not enough data yet!\nKeep bot running to learn patterns!")
        else:
            txt = "🧠 <b>Posting Patterns</b>\n━━━━━━━━━━━━━━━\n"
            for loc, times in list(posting_times.items())[:5]:
                if times:
                    common = max(set(times), key=times.count)
                    txt += f"📍 {loc}\n⏰ Usually posts at {common}:00 UTC\n\n"
            await tg_send(txt)

    elif text == "/test":
        await tg_alert({
            "id": "JOB-UK-0000000214",
            "title": "Warehouse Operative",
            "location": "Rugby, England (Coventry, Rugby, Daventry Area) CV23 0XF",
            "pay": 14.30,
            "contract": "Full-time",
            "duration": "Seasonal",
            "firstDay": "2026-05-10",
            "schedule": "Sun, Mon, Tue, Wed, Thu 18:30-2:30",
            "hours": "40",
            "score": 85,
            "link": "https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000214&locale=en-GB",
        }, "new")

    elif text == "/pause":
        bot_paused = True
        await tg_send("⏸️ Paused.")

    elif text == "/resume":
        bot_paused = False
        await tg_send("▶️ Resumed! 🔥")

    elif text == "/help":
        await tg_send("""🤖 <b>Commands</b>
/scrape   — Scan now
/status   — Bot status
/jobs     — Recent jobs
/history  — All time stats
/predict  — Posting patterns
/test     — Test alert
/pause    — Pause
/resume   — Resume
/help     — This message""")

# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Amazon SUPERBOT v3 — Decodo Edition Starting!")
    asyncio.create_task(handle_updates())
    asyncio.create_task(send_daily_summary())

    await asyncio.sleep(2)
    await tg_send("""🚀 <b>Amazon SUPERBOT v3 ONLINE!</b>
⚡ Decodo Residential Proxies 🔵
🌍 ALL UK warehouse jobs
⭐ Smart scoring system
📊 Daily summaries at 8am
🤖 Auto-navigates application
👆 You just tap SUBMIT!
Send /scrape to check now!""")

    await check_jobs()

    # Check every 3 seconds — beast mode! 🔥
    while True:
        await asyncio.sleep(3)
        await check_jobs()

if __name__ == "__main__":
    asyncio.run(main())
