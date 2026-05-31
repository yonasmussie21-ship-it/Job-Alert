#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║        @Amazonjobs100_bot — Complete Bot FINAL               ║
║        Amazon UK Warehouse Job Alerts + Auto-Submit          ║
║                                                              ║
║  All endpoints confirmed from HAR analysis                   ║
║                                                              ║
║  Key discoveries:                                            ║
║  1. Job search uses geoQueryClause (NOT top-level lat/lng)   ║
║  2. Submit = update-workflow-step-name → "thank-you"         ║
║  3. No separate submit-application endpoint                  ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import aiohttp
import aiosqlite
import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("amazonjobs")

BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
DB_PATH      = "/tmp/amazonjobs.db"

BASE_URL     = "https://www.jobsatamazon.co.uk"
GRAPHQL_URL  = f"{BASE_URL}/candidate/graphql"

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin":          BASE_URL,
    "Referer":         f"{BASE_URL}/",
    "bb-ui-version":   "bb-ui-v2",
}

UK_CITIES = {
    "birmingham":    (52.4862, -1.8904),
    "london":        (51.5074, -0.1278),
    "manchester":    (53.4808, -2.2426),
    "coventry":      (52.4068, -1.5197),
    "wolverhampton": (52.5847, -2.1269),
    "derby":         (52.9225, -1.4746),
    "nottingham":    (52.9548, -1.1581),
    "leicester":     (52.6369, -1.1398),
    "bristol":       (51.4545, -2.5879),
    "leeds":         (53.8008, -1.5491),
    "sheffield":     (53.3811, -1.4701),
    "liverpool":     (53.4084, -2.9916),
    "newcastle":     (54.9783, -1.6178),
    "glasgow":       (55.8642, -4.2518),
    "edinburgh":     (55.9533, -3.1883),
    "cardiff":       (51.4816, -3.1791),
    "exeter":        (50.7236, -3.5275),
    "rotherham":     (53.4326, -1.3635),
    "swindon":       (51.5558, -1.7797),
    "luton":         (51.8787, -0.4200),
    "reading":       (51.4543, -0.9781),
    "milton keynes": (52.0406, -0.7594),
    "doncaster":     (53.5228, -1.1289),
    "sunderland":    (54.9069, -1.3838),
    "sthelens":      (53.4500, -2.7333),
    "plymouth":      (50.3755, -4.1427),
}

TIERS = {
    "alerts":      {"price": 5,  "emoji": "🔔", "label": "Alerts Only"},
    "instant":     {"price": 10, "emoji": "⚡", "label": "Instant Alerts"},
    "auto_submit": {"price": 20, "emoji": "🤖", "label": "Auto-Submit"},
}

# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id TEXT PRIMARY KEY,
                tier TEXT DEFAULT 'alerts',
                active INTEGER DEFAULT 0,
                locations TEXT DEFAULT '[]',
                radius INTEGER DEFAULT 25,
                job_type TEXT DEFAULT 'fulltime',
                candidate_id TEXT DEFAULT '',
                amazon_cookies TEXT DEFAULT '{}',
                csrf_token TEXT DEFAULT '',
                joined TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_jobs (
                fingerprint TEXT PRIMARY KEY,
                job_id TEXT,
                seen_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS submit_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                chat_id TEXT,
                candidate_id TEXT,
                application_id TEXT,
                job_id TEXT,
                job_title TEXT,
                city TEXT,
                pay REAL,
                state_before TEXT,
                state_after TEXT,
                submitted INTEGER DEFAULT 0,
                success INTEGER DEFAULT 0,
                error TEXT
            )
        """)
        await db.commit()
    logger.info("✅ Database ready")


async def get_subscriber(chat_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM subscribers WHERE chat_id = ?",
            (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                d = dict(row)
                d["locations"] = json.loads(d["locations"])
                d["amazon_cookies"] = json.loads(d["amazon_cookies"])
                return d
    return None


async def save_subscriber(sub: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO subscribers (
                chat_id, tier, active, locations, radius,
                job_type, candidate_id, amazon_cookies,
                csrf_token, joined
            ) VALUES (
                :chat_id, :tier, :active, :locations, :radius,
                :job_type, :candidate_id, :amazon_cookies,
                :csrf_token, :joined
            )
        """, {
            **sub,
            "locations": json.dumps(sub.get("locations", [])),
            "amazon_cookies": json.dumps(sub.get("amazon_cookies", {}))
        })
        await db.commit()


async def get_all_active_subscribers() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM subscribers WHERE active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["locations"] = json.loads(d["locations"])
                d["amazon_cookies"] = json.loads(d["amazon_cookies"])
                result.append(d)
            return result


async def is_job_seen(fingerprint: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM seen_jobs WHERE fingerprint = ?",
            (fingerprint,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_job_seen(fingerprint: str, job_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_jobs VALUES (?, ?, ?)",
            (fingerprint, job_id, datetime.now().isoformat())
        )
        await db.commit()


async def save_audit(log: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO submit_audit (
                timestamp, chat_id, candidate_id, application_id,
                job_id, job_title, city, pay,
                state_before, state_after, submitted, success, error
            ) VALUES (
                :timestamp, :chat_id, :candidate_id, :application_id,
                :job_id, :job_title, :city, :pay,
                :state_before, :state_after, :submitted, :success, :error
            )
        """, log)
        await db.commit()


# ═══════════════════════════════════════════════════════════
# AMAZON API — AUTH
# ═══════════════════════════════════════════════════════════

async def get_csrf_token(
    session: aiohttp.ClientSession,
    cookies: dict
) -> str:
    async with session.get(
        f"{BASE_URL}/authorize/api/csrf?countryCode=UK",
        headers=HEADERS_BASE,
        cookies=cookies
    ) as resp:
        resp.raise_for_status()
        return (await resp.json()).get("token", "")


async def authorize(
    session: aiohttp.ClientSession,
    amazon_token: str,
    cookies: dict
) -> dict:
    async with session.post(
        f"{BASE_URL}/authorize/api/authorize?countryCode=UK",
        headers={**HEADERS_BASE, "Content-Type": "application/json"},
        json={"redirectUrl": "www.jobsatamazon.co.uk", "token": amazon_token},
        cookies=cookies
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


# ═══════════════════════════════════════════════════════════
# AMAZON API — JOB SEARCH (CONFIRMED geoQueryClause)
# ═══════════════════════════════════════════════════════════

SEARCH_QUERY = """
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
      totalPayRateMinL10N
      totalPayRateMaxL10N
      distance
      distanceL10N
      featuredJob
      bonusJob
      bonusPay
      scheduleCount
      currencyCode
      surgePay
      employmentTypeL10N
      payFrequency
      jobLocationType
      internalStaffingOrgId
      virtualLocation
      tagLine
      poolingEnabled
    }
  }
}
"""


async def search_jobs(
    lat: float,
    lng: float,
    distance: int = 30
) -> list:
    """
    Search Amazon jobs using CONFIRMED geoQueryClause structure.
    NOT top-level lat/lng — must be nested in geoQueryClause.
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            GRAPHQL_URL,
            headers={**HEADERS_BASE, "Content-Type": "application/json"},
            json={
                "operationName": "searchJobCardsByLocation",
                "variables": {
                    "searchJobRequest": {
                        "locale": "en-GB",
                        "country": "United Kingdom",
                        "keyWords": "",
                        "equalFilters": [],
                        "containFilters": [
                            {
                                "key": "isPrivateSchedule",
                                "val": ["true", "false"]
                            }
                        ],
                        "rangeFilters": [],
                        "orFilters": [
                            {"key": "bonusJob", "val": ["true"]},
                            {"key": "featuredJob", "val": ["true"]}
                        ],
                        "dateFilters": [],
                        "sorters": [],
                        "pageSize": 100,
                        "geoQueryClause": {
                            "lat": lat,
                            "lng": lng,
                            "unit": "mi",
                            "distance": distance
                        },
                        "consolidateSchedule": True
                    }
                },
                "query": SEARCH_QUERY
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            jobs = (
                data.get("data", {})
                .get("searchJobCardsByLocation", {})
                .get("jobCards", [])
            )
            logger.info(
                f"Search ({lat:.4f},{lng:.4f}) "
                f"d={distance}mi → {len(jobs)} jobs"
            )
            return jobs


def parse_job(job: dict) -> Optional[dict]:
    if job.get("virtualLocation"):
        return None
    if job.get("jobLocationType") not in ("Physical", None, ""):
        return None
    return {
        "jobId":         job.get("jobId", ""),
        "title":         job.get("jobTitle", "Warehouse Operative"),
        "jobType":       job.get("jobType", ""),
        "employmentType":job.get("employmentType", ""),
        "city":          job.get("city", ""),
        "postalCode":    job.get("postalCode", ""),
        "locationName":  job.get("locationName", ""),
        "payMin":        job.get("totalPayRateMin", 0),
        "payMax":        job.get("totalPayRateMax", 0),
        "payMinStr":     job.get("totalPayRateMinL10N", ""),
        "payMaxStr":     job.get("totalPayRateMaxL10N", ""),
        "distance":      job.get("distance", 0),
        "distanceStr":   job.get("distanceL10N", ""),
        "scheduleCount": job.get("scheduleCount", 0),
        "bonusPay":      job.get("bonusPay", 0),
        "surgePay":      job.get("surgePay", 0),
        "tagLine":       job.get("tagLine", ""),
        "featuredJob":   job.get("featuredJob", False),
        "bonusJob":      job.get("bonusJob", False),
        "fingerprint":   hashlib.md5(
                             job.get("jobId", "").encode()
                         ).hexdigest()
    }


def format_job_alert(job: dict, tier: str = "alerts") -> str:
    location  = job.get("locationName") or job.get("city", "")
    pay_min   = job.get("payMinStr", "")
    pay_max   = job.get("payMaxStr", "")
    pay_str   = pay_min if pay_min == pay_max else f"{pay_min}–{pay_max}"
    distance  = job.get("distanceStr", "")
    schedules = job.get("scheduleCount", 0)
    surge     = job.get("surgePay", 0)
    bonus     = job.get("bonusPay", 0)
    tagline   = job.get("tagLine", "")
    job_id    = job.get("jobId", "")

    header = "⭐ *FEATURED*" if job.get("featuredJob") else \
             "🎁 *BONUS JOB*" if job.get("bonusJob") else \
             "🚨 *NEW JOB ALERT*"

    lines = [header, "",
             f"📦 *{job.get('title', 'Warehouse Operative')}*",
             f"📍 {location}",
             f"💰 {pay_str}/hr"]

    if job.get("employmentType"):
        lines.append(f"📋 {job['employmentType']}")
    if distance:
        lines.append(f"📏 {distance} miles away")
    if schedules:
        lines.append(f"🕐 {schedules} shift(s) available")
    if surge and surge > 0:
        lines.append(f"⚡ Surge: +£{surge:.2f}/hr")
    if bonus and bonus > 0:
        lines.append(f"🎁 Bonus: £{bonus:.2f}")
    if tagline:
        lines.append(f"💡 _{tagline}_")

    lines += ["",
              f"🔗 [Apply Now](https://www.jobsatamazon.co.uk"
              f"/en-GB/search?jobId={job_id})"]

    if tier == "auto_submit":
        lines.append("\n🤖 _Auto-submitting for you..._")
    elif tier == "instant":
        lines.append("\n⚡ _Be first to apply!_")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# AMAZON API — SUBMIT (CONFIRMED FLOW)
# ═══════════════════════════════════════════════════════════

async def update_workflow_step(
    session: aiohttp.ClientSession,
    application_id: str,
    step_name: str,
    csrf_token: str,
    cookies: dict
) -> dict:
    """
    Confirmed steps:
    additional-information → nhe → review-submit → thank-you
    "thank-you" = APPLICATION SUBMITTED
    """
    async with session.put(
        f"{BASE_URL}/application/api/candidate-application"
        f"/update-workflow-step-name",
        headers={
            **HEADERS_BASE,
            "Content-Type": "application/json;charset=UTF-8",
            "x-csrf-token": csrf_token,
        },
        json={
            "applicationId": application_id,
            "workflowStepName": step_name
        },
        cookies=cookies
    ) as resp:
        data = await resp.json()
        state = data.get("data", {}).get("currentState", "")
        logger.info(f"Step '{step_name}' → state={state}")
        return data.get("data", {})


async def check_assessment_eligibility(
    session: aiohttp.ClientSession,
    application_id: str,
    candidate_id: str,
    job_id: str,
    csrf_token: str,
    cookies: dict
) -> bool:
    async with session.post(
        f"{BASE_URL}/application/api/candidate-application"
        f"/assessment-eligibility",
        headers={
            **HEADERS_BASE,
            "Content-Type": "application/json;charset=UTF-8",
            "x-csrf-token": csrf_token,
        },
        json={
            "applicationId": application_id,
            "candidateId": candidate_id,
            "jobId": job_id
        },
        cookies=cookies
    ) as resp:
        data = await resp.json()
        return data.get("data", {}).get("assessmentEligibility", False)


async def get_application_state(
    session: aiohttp.ClientSession,
    application_id: str,
    cookies: dict
) -> dict:
    async with session.get(
        f"{BASE_URL}/application/api/candidate-application"
        f"/applications/{application_id}",
        headers=HEADERS_BASE,
        cookies=cookies
    ) as resp:
        return (await resp.json()).get("data", {})


async def submit_application(
    session: aiohttp.ClientSession,
    application_id: str,
    candidate_id: str,
    job_id: str,
    csrf_token: str,
    cookies: dict
) -> dict:
    """
    CONFIRMED submit flow from HAR12/HAR13.
    Submit = update-workflow-step-name → "thank-you"
    """
    # Capture BEFORE state
    before = await get_application_state(
        session, application_id, cookies
    )
    state_before = before.get("currentState", "")

    logger.info(f"BEFORE: {application_id} state={state_before}")

    if before.get("submitted"):
        return {"success": True, "state_before": state_before,
                "state_after": state_before, "submitted": True}

    await asyncio.sleep(0.5)

    # Assessment eligibility check
    await check_assessment_eligibility(
        session, application_id,
        candidate_id, job_id,
        csrf_token, cookies
    )
    await asyncio.sleep(0.5)

    # Move to review-submit
    await update_workflow_step(
        session, application_id,
        "review-submit", csrf_token, cookies
    )
    await asyncio.sleep(0.8)

    # THE SUBMIT — move to thank-you
    logger.info(f"🚀 SUBMITTING: {application_id}")
    after_data = await update_workflow_step(
        session, application_id,
        "thank-you", csrf_token, cookies
    )
    await asyncio.sleep(1)

    # Verify
    after = await get_application_state(
        session, application_id, cookies
    )
    state_after = after.get("currentState", "")
    submitted = after.get("submitted", False)

    success = (
        submitted is True
        or state_after == "APPLICATION_SUBMITTED"
        or after_data.get("workflowStepName") == "thank-you"
    )

    logger.info(
        f"AFTER: state={state_after} "
        f"submitted={submitted} success={success}"
    )

    return {
        "success": success,
        "state_before": state_before,
        "state_after": state_after,
        "submitted": submitted
    }


# ═══════════════════════════════════════════════════════════
# TELEGRAM BOT — COMMANDS
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    sub = await get_subscriber(chat_id)
    if not sub:
        await save_subscriber({
            "chat_id": chat_id, "tier": "alerts",
            "active": False, "locations": [],
            "radius": 25, "job_type": "fulltime",
            "candidate_id": "", "amazon_cookies": {},
            "csrf_token": "",
            "joined": datetime.now().isoformat()
        })

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Set Location", callback_data="set_location"),
         InlineKeyboardButton("📊 My Status", callback_data="status")],
        [InlineKeyboardButton("💎 Upgrade Tier", callback_data="upgrade")]
    ])

    await update.message.reply_text(
        "👋 *Welcome to @Amazonjobs100_bot!*\n\n"
        "🏭 I find Amazon UK warehouse jobs and alert you instantly.\n\n"
        "🚀 *Get started:* Set your location with /location\n"
        "📖 Type /help for all commands.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "/location [city] — Set your city\n"
        "/radius [miles] — Search radius (default 25)\n"
        "/status — Your settings\n"
        "/jobs — Check jobs now\n"
        "/pause — Pause alerts\n"
        "/resume — Resume alerts\n"
        "/tier — View subscription tiers\n"
        "/history — Submission history\n\n"
        "💬 Support: @Amazonjobs100_support",
        parse_mode="Markdown"
    )


async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not context.args:
        cities = list(UK_CITIES.keys())[:16]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(c.title(), callback_data=f"loc_{c}")
             for c in cities[i:i+2]]
            for i in range(0, len(cities), 2)
        ])
        await update.message.reply_text(
            "📍 *Choose your location:*\nOr: `/location birmingham`",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    city = " ".join(context.args).lower().strip()
    if city not in UK_CITIES:
        await update.message.reply_text(
            f"❌ City not found.\nTry: {', '.join(list(UK_CITIES.keys())[:8])}..."
        )
        return

    sub = await get_subscriber(chat_id) or {
        "chat_id": chat_id, "tier": "alerts",
        "radius": 25, "job_type": "fulltime",
        "candidate_id": "", "amazon_cookies": {},
        "csrf_token": "", "joined": datetime.now().isoformat()
    }
    sub["locations"] = [city]
    sub["active"] = True
    await save_subscriber(sub)

    await update.message.reply_text(
        f"✅ Location set to *{city.title()}*\n"
        f"🔍 Alerts are now *active!*",
        parse_mode="Markdown"
    )


async def cmd_radius(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Usage: `/radius 25`", parse_mode="Markdown")
        return
    try:
        radius = max(5, min(100, int(context.args[0])))
    except ValueError:
        await update.message.reply_text("Please enter a number e.g. `/radius 25`", parse_mode="Markdown")
        return
    sub = await get_subscriber(chat_id)
    if sub:
        sub["radius"] = radius
        await save_subscriber(sub)
        await update.message.reply_text(f"✅ Radius set to *{radius} miles*", parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    sub = await get_subscriber(chat_id)
    if not sub:
        await update.message.reply_text("❌ Not registered. Use /start first.")
        return
    tier_info = TIERS.get(sub.get("tier", "alerts"), TIERS["alerts"])
    status = "✅ Active" if sub.get("active") else "⏸️ Paused"
    locations = sub.get("locations", [])
    await update.message.reply_text(
        f"📊 *Your Status*\n\n"
        f"Status: {status}\n"
        f"Tier: {tier_info['emoji']} {tier_info['label']}\n"
        f"📍 Location: {', '.join(locations) if locations else 'Not set'}\n"
        f"📏 Radius: {sub.get('radius', 25)} miles\n"
        f"⏰ Job type: {sub.get('job_type', 'fulltime').title()}",
        parse_mode="Markdown"
    )


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    sub = await get_subscriber(chat_id)
    if not sub or not sub.get("locations"):
        await update.message.reply_text("❌ Set your location first with /location")
        return
    await update.message.reply_text("🔍 Checking for jobs now...")
    city = sub["locations"][0]
    lat, lng = UK_CITIES.get(city, (52.4862, -1.8904))
    radius = sub.get("radius", 25)
    jobs = await search_jobs(lat, lng, radius)
    parsed = [j for j in [parse_job(j) for j in jobs] if j]
    if not parsed:
        await update.message.reply_text(
            f"😔 No jobs found near {city.title()} right now.\n"
            f"I'll alert you when something comes up!"
        )
        return
    await update.message.reply_text(
        f"✅ Found *{len(parsed)} job(s)* near {city.title()}!",
        parse_mode="Markdown"
    )
    for job in parsed[:5]:
        await update.message.reply_text(
            format_job_alert(job, sub.get("tier", "alerts")),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        await asyncio.sleep(0.5)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    sub = await get_subscriber(chat_id)
    if sub:
        sub["active"] = False
        await save_subscriber(sub)
        await update.message.reply_text("⏸️ Alerts *paused*. Use /resume to restart.", parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    sub = await get_subscriber(chat_id)
    if sub:
        sub["active"] = True
        await save_subscriber(sub)
        await update.message.reply_text("▶️ Alerts *resumed!*", parse_mode="Markdown")


async def cmd_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{info['emoji']} {info['label']} — £{info['price']}/mo",
            callback_data=f"tier_{name}"
        )]
        for name, info in TIERS.items()
    ])
    await update.message.reply_text(
        "💎 *Subscription Tiers*\n\n"
        "🔔 *Alerts Only* — £5/mo\nJob alerts every 30 seconds\n\n"
        "⚡ *Instant Alerts* — £10/mo\nPriority + faster polling\n\n"
        "🤖 *Auto-Submit* — £20/mo\nWe apply to jobs automatically\n\n"
        "Select below:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM submit_audit WHERE chat_id = ? ORDER BY timestamp DESC LIMIT 5",
            (chat_id,)
        ) as cursor:
            logs = [dict(r) for r in await cursor.fetchall()]

    if not logs:
        await update.message.reply_text("📋 No submission history yet.")
        return

    lines = ["📋 *Recent Submissions*\n"]
    for log in logs:
        icon = "✅" if log["success"] else "❌"
        dt = log["timestamp"][:16].replace("T", " ")
        lines.append(
            f"{icon} *{log['job_title']}* — {log['city']}\n"
            f"   `{log['state_before']}` → `{log['state_after']}`\n"
            f"   {dt}\n"
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown"
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = str(query.from_user.id)
    data = query.data

    if data.startswith("loc_"):
        city = data[4:]
        sub = await get_subscriber(chat_id) or {
            "chat_id": chat_id, "tier": "alerts",
            "radius": 25, "job_type": "fulltime",
            "candidate_id": "", "amazon_cookies": {},
            "csrf_token": "", "joined": datetime.now().isoformat()
        }
        sub["locations"] = [city]
        sub["active"] = True
        await save_subscriber(sub)
        await query.edit_message_text(
            f"✅ Location set to *{city.title()}*\n🔍 Alerts active!",
            parse_mode="Markdown"
        )

    elif data.startswith("tier_"):
        tier = data[5:]
        sub = await get_subscriber(chat_id)
        if sub:
            sub["tier"] = tier
            await save_subscriber(sub)
        info = TIERS.get(tier, TIERS["alerts"])
        await query.edit_message_text(
            f"✅ Tier updated to {info['emoji']} *{info['label']}*\n"
            f"Payment: £{info['price']}/month\n"
            f"Contact @Amazonjobs100_support to complete payment.",
            parse_mode="Markdown"
        )

    elif data == "status":
        sub = await get_subscriber(chat_id)
        if sub:
            tier_info = TIERS.get(sub.get("tier", "alerts"), TIERS["alerts"])
            status = "✅ Active" if sub.get("active") else "⏸️ Paused"
            await query.edit_message_text(
                f"📊 *Your Status*\n\n"
                f"Status: {status}\n"
                f"Tier: {tier_info['emoji']} {tier_info['label']}\n"
                f"📍 Location: {', '.join(sub.get('locations', []) or ['Not set'])}\n"
                f"📏 Radius: {sub.get('radius', 25)} miles",
                parse_mode="Markdown"
            )


# ═══════════════════════════════════════════════════════════
# POLLING ENGINE
# ═══════════════════════════════════════════════════════════

async def poll_and_alert(app):
    logger.info(f"🔄 Polling started (every {POLL_INTERVAL}s)")

    while True:
        try:
            subs = await get_all_active_subscribers()

            location_map = {}
            for sub in subs:
                for city in sub.get("locations", []):
                    if city not in location_map:
                        location_map[city] = []
                    location_map[city].append(sub)

            for city, sub_list in location_map.items():
                coords = UK_CITIES.get(city)
                if not coords:
                    continue

                lat, lng = coords
                max_radius = max(s.get("radius", 25) for s in sub_list)
                jobs = await search_jobs(lat, lng, max_radius)

                for job in jobs:
                    parsed = parse_job(job)
                    if not parsed:
                        continue

                    fp = parsed["fingerprint"]
                    if await is_job_seen(fp):
                        continue

                    await mark_job_seen(fp, parsed["jobId"])

                    for sub in sub_list:
                        if parsed["distance"] > sub.get("radius", 25):
                            continue

                        try:
                            await app.bot.send_message(
                                chat_id=int(sub["chat_id"]),
                                text=format_job_alert(
                                    parsed, sub.get("tier", "alerts")
                                ),
                                parse_mode="Markdown",
                                disable_web_page_preview=True
                            )

                            # Auto-submit for premium tier
                            if (sub.get("tier") == "auto_submit"
                                    and sub.get("amazon_cookies")
                                    and sub.get("candidate_id")):
                                asyncio.create_task(
                                    run_auto_submit(app, sub, parsed)
                                )

                        except Exception as e:
                            logger.error(f"Alert failed {sub['chat_id']}: {e}")

                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Poll error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def run_auto_submit(app, subscriber: dict, job: dict):
    """Run auto-submit for a subscriber"""
    chat_id = subscriber["chat_id"]
    cookies = subscriber.get("amazon_cookies", {})
    csrf_token = subscriber.get("csrf_token", "")
    candidate_id = subscriber.get("candidate_id", "")

    try:
        async with aiohttp.ClientSession() as session:
            result = await submit_application(
                session=session,
                application_id=job.get("applicationId", ""),
                candidate_id=candidate_id,
                job_id=job["jobId"],
                csrf_token=csrf_token,
                cookies=cookies
            )

        await save_audit({
            "timestamp": datetime.now().isoformat(),
            "chat_id": chat_id,
            "candidate_id": candidate_id,
            "application_id": job.get("applicationId", ""),
            "job_id": job["jobId"],
            "job_title": job.get("title"),
            "city": job.get("city"),
            "pay": job.get("payMin"),
            "state_before": result.get("state_before"),
            "state_after": result.get("state_after"),
            "submitted": int(result.get("submitted", False)),
            "success": int(result.get("success", False)),
            "error": None
        })

        if result["success"]:
            await app.bot.send_message(
                chat_id=int(chat_id),
                text=(
                    f"✅ *Application Submitted!*\n\n"
                    f"📦 {job.get('title')}\n"
                    f"📍 {job.get('city')}\n"
                    f"💰 {job.get('payMinStr')}/hr\n\n"
                    f"Check jobsatamazon.co.uk for next steps!"
                ),
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Auto-submit failed: {e}")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set!")

    logger.info("🚀 Starting @Amazonjobs100_bot FINAL")

    async def post_init(application):
        await init_db()
        asyncio.create_task(poll_and_alert(application))

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("location", cmd_location))
    app.add_handler(CommandHandler("radius",   cmd_radius))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("jobs",     cmd_jobs))
    app.add_handler(CommandHandler("pause",    cmd_pause))
    app.add_handler(CommandHandler("resume",   cmd_resume))
    app.add_handler(CommandHandler("tier",     cmd_tier))
    app.add_handler(CommandHandler("history",  cmd_history))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("✅ Bot ready!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
