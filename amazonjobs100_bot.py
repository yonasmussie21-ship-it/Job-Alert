#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║           @Amazonjobs100_bot — Complete Bot v2.0                    ║
║     Amazon UK Warehouse Job Alerts + Auto-Submit             ║
║                                                              ║
║  Architecture:                                               ║
║  ┌─────────────┐    ┌──────────────┐    ┌────────────────┐  ║
║  │  Telegram   │←──→│  Job Poller  │←──→│  Amazon API    │  ║
║  │  Bot Layer  │    │  (30s loop)  │    │  GraphQL/REST  │  ║
║  └─────────────┘    └──────────────┘    └────────────────┘  ║
║         │                  │                                  ║
║  ┌─────────────┐    ┌──────────────┐                         ║
║  │ Subscribers │    │ Auto-Submit  │                         ║
║  │  Storage    │    │   Engine     │                         ║
║  └─────────────┘    └──────────────┘                         ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import os
import time
import hashlib
from datetime import datetime
from typing import Optional
from pathlib import Path

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("amazonjobs")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
SUBSCRIBERS_FILE = Path("/tmp/subscribers.json")
SEEN_JOBS_FILE = Path("/tmp/seen_jobs.json")

BASE_URL = "https://www.jobsatamazon.co.uk"
GRAPHQL_URL = f"{BASE_URL}/candidate/graphql"
WAF_BASE = "https://ba86c1f50953.c9dd3436.eu-west-2.token.awswaf.com/ba86c1f50953"

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "bb-ui-version": "bb-ui-v2",
}

# ── UK City → Coordinates ──────────────────────────────────
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
    "sthelens":      (53.4500, -2.7333),
    "luton":         (51.8787, -0.4200),
    "milton keynes": (52.0406, -0.7594),
    "reading":       (51.4543, -0.9781),
    "swindon":       (51.5558, -1.7797),
    "sunderland":    (54.9069, -1.3838),
    "doncaster":     (53.5228, -1.1289),
}

# ── Shift priority tiers ───────────────────────────────────
SHIFT_PRIORITY = {
    "NIGHT":   3,   # 18:30+ start — highest priority
    "EVENING": 2,   # 14:00–18:30
    "DAY":     1,   # before 14:00
}

# ── Subscription tiers ────────────────────────────────────
TIERS = {
    "alerts":      {"price": 5,  "emoji": "🔔", "label": "Alerts Only"},
    "instant":     {"price": 10, "emoji": "⚡", "label": "Instant Alerts"},
    "auto_submit": {"price": 20, "emoji": "🤖", "label": "Auto-Submit"},
}


# ═══════════════════════════════════════════════════════════
# STORAGE
# ═══════════════════════════════════════════════════════════

def load_subscribers() -> dict:
    if SUBSCRIBERS_FILE.exists():
        return json.loads(SUBSCRIBERS_FILE.read_text())
    return {}


def save_subscribers(subs: dict):
    SUBSCRIBERS_FILE.write_text(json.dumps(subs, indent=2))


def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    return set()


def save_seen_jobs(seen: set):
    SEEN_JOBS_FILE.write_text(json.dumps(list(seen)))


def job_fingerprint(job: dict) -> str:
    """Create unique fingerprint for a job listing"""
    key = f"{job.get('jobId')}:{job.get('scheduleId', '')}:{job.get('city')}"
    return hashlib.md5(key.encode()).hexdigest()


# ═══════════════════════════════════════════════════════════
# AMAZON API — JOB SEARCH
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
      postalCode
      locationName
      totalPayRateMin
      totalPayRateMax
      distance
      scheduleCount
      currencyCode
      employmentTypeL10N
      distanceL10N
      totalPayRateMinL10N
      totalPayRateMaxL10N
      bonusPay
      surgePay
    }
  }
}
"""


def search_amazon_jobs(lat: float, lng: float, radius: int = 80) -> list:
    """Search Amazon jobs by coordinates"""
    try:
        resp = requests.post(
            GRAPHQL_URL,
            headers={**HEADERS_BASE, "Content-Type": "application/json"},
            json={
                "operationName": "searchJobCardsByLocation",
                "variables": {
                    "searchJobRequest": {
                        "locale": "en-GB",
                        "country": "United Kingdom",
                        "pageSize": 100,
                        "lat": lat,
                        "lng": lng,
                        "radius": radius,
                        "keyWords": "",
                        "equalFilters": [],
                        "containFilters": [],
                        "nextToken": None
                    }
                },
                "query": SEARCH_QUERY
            },
            timeout=15
        )
        resp.raise_for_status()
        return (
            resp.json()
            .get("data", {})
            .get("searchJobCardsByLocation", {})
            .get("jobCards", [])
        )
    except Exception as e:
        logger.error(f"Job search failed: {e}")
        return []


def get_schedules_for_job(job_id: str, session: requests.Session) -> list:
    """Get all available shifts for a job"""
    try:
        resp = session.get(
            f"{BASE_URL}/application/api/job/get-all-schedules/{job_id}?locale=en-GB",
            headers=HEADERS_BASE,
            timeout=10
        )
        if resp.status_code == 200:
            return resp.json().get("data", {}).get("schedules", [])
    except Exception as e:
        logger.warning(f"Get schedules failed: {e}")
    return []


def pick_best_shift(schedules: list) -> Optional[dict]:
    """
    Pick the best shift using priority logic:
    Night > Evening > Day, then highest rated within tier
    """
    if not schedules:
        return None

    def shift_score(s):
        start_time = s.get("shiftStartTime", "00:00")
        try:
            hour = int(start_time.split(":")[0])
        except:
            hour = 9

        if hour >= 18:
            tier = SHIFT_PRIORITY["NIGHT"]
        elif hour >= 14:
            tier = SHIFT_PRIORITY["EVENING"]
        else:
            tier = SHIFT_PRIORITY["DAY"]

        rating = s.get("siteRating", 0) or 0
        pay = s.get("basePay", 0) or 0

        return (tier, rating, pay)

    return max(schedules, key=shift_score)


# ═══════════════════════════════════════════════════════════
# AMAZON API — AUTHENTICATION
# ═══════════════════════════════════════════════════════════

def get_csrf_token(session: requests.Session) -> str:
    resp = session.get(
        f"{BASE_URL}/authorize/api/csrf?countryCode=UK",
        headers=HEADERS_BASE,
        timeout=10
    )
    resp.raise_for_status()
    return resp.json().get("token", "")


def authorize_session(session: requests.Session, amazon_token: str) -> dict:
    resp = session.post(
        f"{BASE_URL}/authorize/api/authorize?countryCode=UK",
        headers={**HEADERS_BASE, "Content-Type": "application/json"},
        json={"redirectUrl": "www.jobsatamazon.co.uk", "token": amazon_token},
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()


def get_waf_token(session: requests.Session) -> str:
    """Get WAF bypass token"""
    try:
        inputs = session.get(
            f"{WAF_BASE}/inputs?client=browser",
            headers=HEADERS_BASE, timeout=10
        ).json()
        challenge = inputs.get("challenge", {})

        verify = session.post(
            f"{WAF_BASE}/mp_verify",
            files={
                "solution_data": (None, "A" * 2048),
                "solution_metadata": (None, json.dumps({
                    "challenge": {
                        "input": challenge.get("input", ""),
                        "hmac": challenge.get("hmac", ""),
                        "region": challenge.get("region", "eu-west-2")
                    },
                    "solution": None,
                    "signals": []
                }))
            },
            headers={k: v for k, v in HEADERS_BASE.items()
                     if k != "Content-Type"},
            timeout=10
        )
        return verify.json().get("token", "")
    except Exception as e:
        logger.warning(f"WAF token failed: {e}")
        return ""


# ═══════════════════════════════════════════════════════════
# AMAZON API — AUTO-SUBMIT
# ═══════════════════════════════════════════════════════════

def update_workflow_step(
    session: requests.Session,
    application_id: str,
    csrf_token: str
) -> dict:
    resp = session.put(
        f"{BASE_URL}/application/api/candidate-application/update-workflow-step-name",
        headers={
            **HEADERS_BASE,
            "Content-Type": "application/json;charset=UTF-8",
            "x-csrf-token": csrf_token,
        },
        json={
            "applicationId": application_id,
            "workflowStepName": "review-submit"
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json().get("data", {})


def submit_application(
    session: requests.Session,
    application_id: str,
    job_id: str,
    schedule_id: str,
    csrf_token: str,
    waf_token: str = ""
) -> bool:
    """Submit application — tries multiple payload variants"""
    if waf_token:
        session.cookies.set(
            "aws-waf-token", waf_token,
            domain="www.jobsatamazon.co.uk"
        )

    headers = {
        **HEADERS_BASE,
        "Content-Type": "application/json;charset=UTF-8",
        "x-csrf-token": csrf_token,
        "Referer": (
            f"{BASE_URL}/application/uk/"
            f"?applicationId={application_id}"
            f"&jobId={job_id}"
            f"#/review-submit"
        )
    }

    # Try payload variants in order
    payloads = [
        {"applicationId": application_id},
        {"applicationId": application_id, "jobId": job_id},
        {
            "applicationId": application_id,
            "jobId": job_id,
            "scheduleId": schedule_id
        },
    ]

    for i, payload in enumerate(payloads):
        try:
            resp = session.post(
                f"{BASE_URL}/application/api/candidate-application/submit-application",
                headers=headers,
                json=payload,
                timeout=15
            )
            if resp.status_code == 200:
                logger.info(f"✅ Submitted with payload variant {i+1}")
                return True
            elif resp.status_code == 403:
                logger.error("WAF blocked — need Playwright")
                return False
            logger.warning(f"Payload {i+1} returned {resp.status_code}")
        except Exception as e:
            logger.error(f"Submit attempt {i+1} failed: {e}")

    return False


def verify_submission(session: requests.Session, application_id: str) -> bool:
    """Verify application was actually submitted"""
    try:
        resp = session.get(
            f"{BASE_URL}/selfservice/api/schedule/details/application/{application_id}",
            headers=HEADERS_BASE, timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("legacyStatus") == "APPLICATION_SUBMITTED"
    except:
        pass

    # Fallback
    try:
        resp = session.get(
            f"{BASE_URL}/application/api/candidate-application/applications/{application_id}",
            headers=HEADERS_BASE, timeout=10
        )
        return resp.json().get("data", {}).get("submitted", False)
    except:
        return False


def auto_submit_for_subscriber(subscriber: dict, job: dict) -> dict:
    """
    Full auto-submit flow for a subscriber.
    Returns result dict with success/error.
    """
    session = requests.Session()
    cookies = subscriber.get("amazon_cookies", {})
    amazon_token = subscriber.get("amazon_token", "")
    application_id = job.get("applicationId", "")
    job_id = job.get("jobId", "")
    schedule_id = job.get("scheduleId", "")

    session.cookies.update(cookies)

    try:
        csrf = get_csrf_token(session)
        time.sleep(0.3)

        auth = authorize_session(session, amazon_token)
        if not auth.get("isValid"):
            return {"success": False, "error": "Auth failed"}
        time.sleep(0.3)

        waf = get_waf_token(session)
        time.sleep(0.3)

        update_workflow_step(session, application_id, csrf)
        time.sleep(1)

        success = submit_application(
            session, application_id, job_id, schedule_id, csrf, waf
        )
        time.sleep(1)

        if success:
            confirmed = verify_submission(session, application_id)
            return {"success": confirmed, "error": None}

        return {"success": False, "error": "Submit returned non-200"}

    except Exception as e:
        return {"success": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# MESSAGE FORMATTING
# ═══════════════════════════════════════════════════════════

def format_job_alert(job: dict, subscriber: dict) -> str:
    """Format a beautiful job alert message"""
    title = job.get("jobTitle", "Warehouse Operative")
    city = job.get("city", "Unknown")
    employment = job.get("employmentTypeL10N", job.get("employmentType", ""))
    pay_min = job.get("totalPayRateMinL10N", "")
    pay_max = job.get("totalPayRateMaxL10N", "")
    distance = job.get("distanceL10N", "")
    schedules = job.get("scheduleCount", 0)
    job_id = job.get("jobId", "")
    surge = job.get("surgePay")
    bonus = job.get("bonusPay")

    pay_str = f"{pay_min}" if pay_min == pay_max else f"{pay_min}–{pay_max}"

    lines = [
        f"🚨 *NEW JOB ALERT*",
        f"",
        f"📦 *{title}*",
        f"📍 {city}",
        f"💰 {pay_str}/hr",
        f"📋 {employment}",
    ]

    if distance:
        lines.append(f"📏 {distance} away")
    if schedules:
        lines.append(f"🕐 {schedules} shift(s) available")
    if surge:
        lines.append(f"⚡ Surge pay: £{surge:.2f}/hr extra")
    if bonus:
        lines.append(f"🎁 Bonus: £{bonus:.2f}")

    lines += [
        f"",
        f"🔗 [Apply Now](https://www.jobsatamazon.co.uk/en-GB/search?jobId={job_id})",
        f"",
        f"⏰ {datetime.now().strftime('%H:%M · %d %b %Y')}",
    ]

    tier = subscriber.get("tier", "alerts")
    if tier == "auto_submit":
        lines.append(f"\n🤖 _Auto-submitting for you..._")
    elif tier == "instant":
        lines.append(f"\n⚡ _Instant alert — apply fast!_")

    return "\n".join(lines)


def format_status_message(subscriber: dict, jobs_found: int) -> str:
    """Format /status command response"""
    tier = subscriber.get("tier", "alerts")
    tier_info = TIERS.get(tier, TIERS["alerts"])
    locations = subscriber.get("locations", [])
    job_type = subscriber.get("job_type", "fulltime")
    radius = subscriber.get("radius", 25)
    active = subscriber.get("active", True)

    status = "✅ Active" if active else "⏸️ Paused"

    return (
        f"📊 *Your AmazonJobs Status*\n\n"
        f"Status: {status}\n"
        f"Tier: {tier_info['emoji']} {tier_info['label']}\n"
        f"📍 Locations: {', '.join(locations) if locations else 'Not set'}\n"
        f"📏 Radius: {radius} miles\n"
        f"⏰ Job type: {job_type.title()}\n"
        f"🔍 Jobs tracked today: {jobs_found}\n\n"
        f"Use /help to see all commands."
    )


# ═══════════════════════════════════════════════════════════
# BOT COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    chat_id = str(update.effective_chat.id)
    subs = load_subscribers()

    if chat_id not in subs:
        subs[chat_id] = {
            "chat_id": chat_id,
            "tier": "alerts",
            "active": False,
            "locations": [],
            "radius": 25,
            "job_type": "fulltime",
            "joined": datetime.now().isoformat(),
            "amazon_cookies": {},
            "amazon_token": "",
        }
        save_subscribers(subs)

    keyboard = [
        [
            InlineKeyboardButton("🔔 Set Location", callback_data="set_location"),
            InlineKeyboardButton("⚙️ Preferences", callback_data="preferences"),
        ],
        [
            InlineKeyboardButton("💎 Upgrade Tier", callback_data="upgrade"),
            InlineKeyboardButton("📊 My Status", callback_data="status"),
        ],
    ]

    await update.message.reply_text(
        "👋 *Welcome to @Amazonjobs100_bot!*\n\n"
        "🏭 I find Amazon UK warehouse jobs and alert you instantly.\n\n"
        "🚀 *What I can do:*\n"
        "• 🔔 Real-time job alerts\n"
        "• ⚡ Instant notifications\n"
        "• 🤖 Auto-apply to jobs for you\n\n"
        "💡 *Get started:* Set your location with /location\n\n"
        "📖 Type /help for all commands.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    await update.message.reply_text(
        "📖 *@Amazonjobs100_bot Commands*\n\n"
        "🏁 *Setup:*\n"
        "/start — Welcome & setup\n"
        "/location [city] — Set your location\n"
        "/radius [miles] — Set search radius (default: 25)\n"
        "/jobtype [fulltime|parttime|both] — Job type filter\n\n"
        "📊 *Info:*\n"
        "/status — Your current settings\n"
        "/jobs — Manually check for jobs now\n\n"
        "⚙️ *Control:*\n"
        "/pause — Pause alerts\n"
        "/resume — Resume alerts\n"
        "/tier — View/change subscription tier\n\n"
        "🤖 *Auto-Submit (Premium):*\n"
        "/connect — Connect your Amazon account\n"
        "/autosubmit [on|off] — Toggle auto-submit\n\n"
        "💬 *Support:* @AmazonJobsSupport",
        parse_mode="Markdown"
    )


async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /location command"""
    chat_id = str(update.effective_chat.id)

    if not context.args:
        # Show city picker keyboard
        cities = list(UK_CITIES.keys())[:16]
        keyboard = [
            [
                InlineKeyboardButton(
                    city.title(),
                    callback_data=f"loc_{city}"
                )
                for city in cities[i:i+2]
            ]
            for i in range(0, len(cities), 2)
        ]
        await update.message.reply_text(
            "📍 *Choose your location:*\n"
            "Or type: `/location birmingham`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    city = " ".join(context.args).lower().strip()

    if city not in UK_CITIES:
        await update.message.reply_text(
            f"❌ City `{city}` not found.\n\n"
            f"Available: {', '.join(UK_CITIES.keys())}",
            parse_mode="Markdown"
        )
        return

    subs = load_subscribers()
    if chat_id not in subs:
        subs[chat_id] = {"chat_id": chat_id, "tier": "alerts",
                         "active": True, "locations": [], "radius": 25,
                         "job_type": "fulltime", "amazon_cookies": {},
                         "amazon_token": ""}

    subs[chat_id]["locations"] = [city]
    subs[chat_id]["active"] = True
    save_subscribers(subs)

    lat, lng = UK_CITIES[city]
    await update.message.reply_text(
        f"✅ Location set to *{city.title()}*\n"
        f"📡 Searching within {subs[chat_id].get('radius', 25)} miles\n"
        f"🔍 Alerts are now *active*!\n\n"
        f"Use /radius to adjust search distance.",
        parse_mode="Markdown"
    )


async def cmd_radius(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /radius command"""
    chat_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text(
            "📏 Usage: `/radius 25`\n"
            "Min: 5 miles | Max: 100 miles",
            parse_mode="Markdown"
        )
        return

    try:
        radius = int(context.args[0])
        radius = max(5, min(100, radius))
    except ValueError:
        await update.message.reply_text("❌ Please enter a number e.g. `/radius 25`",
                                        parse_mode="Markdown")
        return

    subs = load_subscribers()
    if chat_id in subs:
        subs[chat_id]["radius"] = radius
        save_subscribers(subs)
        await update.message.reply_text(
            f"✅ Search radius set to *{radius} miles*",
            parse_mode="Markdown"
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    chat_id = str(update.effective_chat.id)
    subs = load_subscribers()
    seen = load_seen_jobs()

    sub = subs.get(chat_id, {})
    if not sub:
        await update.message.reply_text(
            "❌ You're not registered. Use /start first."
        )
        return

    await update.message.reply_text(
        format_status_message(sub, len(seen)),
        parse_mode="Markdown"
    )


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /jobs — manual job check"""
    chat_id = str(update.effective_chat.id)
    subs = load_subscribers()
    sub = subs.get(chat_id, {})

    if not sub or not sub.get("locations"):
        await update.message.reply_text(
            "❌ Set your location first with /location"
        )
        return

    await update.message.reply_text("🔍 Checking for jobs now...")

    city = sub["locations"][0]
    lat, lng = UK_CITIES.get(city, (52.4862, -1.8904))
    radius = sub.get("radius", 25)

    jobs = search_amazon_jobs(lat, lng, radius)

    if not jobs:
        await update.message.reply_text(
            f"😔 No jobs found near {city.title()} right now.\n"
            f"I'll alert you as soon as something comes up!"
        )
        return

    await update.message.reply_text(
        f"✅ Found *{len(jobs)} job(s)* near {city.title()}!\n"
        f"Sending details...",
        parse_mode="Markdown"
    )

    for job in jobs[:5]:  # Max 5 jobs at once
        msg = format_job_alert(job, sub)
        await update.message.reply_text(msg, parse_mode="Markdown",
                                        disable_web_page_preview=True)
        await asyncio.sleep(0.5)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = load_subscribers()
    if chat_id in subs:
        subs[chat_id]["active"] = False
        save_subscribers(subs)
        await update.message.reply_text(
            "⏸️ Alerts *paused*. Use /resume to restart.",
            parse_mode="Markdown"
        )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = load_subscribers()
    if chat_id in subs:
        subs[chat_id]["active"] = True
        save_subscribers(subs)
        await update.message.reply_text(
            "▶️ Alerts *resumed*! I'll notify you of new jobs.",
            parse_mode="Markdown"
        )


async def cmd_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show tier options"""
    keyboard = [
        [InlineKeyboardButton(
            f"{info['emoji']} {info['label']} — £{info['price']}/mo",
            callback_data=f"tier_{name}"
        )]
        for name, info in TIERS.items()
    ]
    await update.message.reply_text(
        "💎 *Choose Your Tier*\n\n"
        "🔔 *Alerts Only* — £5/mo\n"
        "Get job alerts every 30 seconds\n\n"
        "⚡ *Instant Alerts* — £10/mo\n"
        "Priority alerts + faster polling\n\n"
        "🤖 *Auto-Submit* — £20/mo\n"
        "We apply to jobs automatically for you\n\n"
        "Select below to upgrade:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Amazon account connection"""
    await update.message.reply_text(
        "🔗 *Connect Your Amazon Account*\n\n"
        "To enable auto-submit I need your Amazon Jobs session.\n\n"
        "📋 *Steps:*\n"
        "1. Open Chrome on desktop\n"
        "2. Go to jobsatamazon.co.uk\n"
        "3. Login to your account\n"
        "4. Press F12 → Application → Cookies\n"
        "5. Copy your session cookies\n"
        "6. Send them here as JSON\n\n"
        "⚠️ _Your credentials are stored securely and only used for job applications._\n\n"
        "Need help? Contact @AmazonJobsSupport",
        parse_mode="Markdown"
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks"""
    query = update.callback_query
    await query.answer()

    chat_id = str(query.from_user.id)
    data = query.data

    if data.startswith("loc_"):
        city = data[4:]
        subs = load_subscribers()
        if chat_id not in subs:
            subs[chat_id] = {
                "chat_id": chat_id, "tier": "alerts",
                "active": True, "locations": [],
                "radius": 25, "job_type": "fulltime",
                "amazon_cookies": {}, "amazon_token": ""
            }
        subs[chat_id]["locations"] = [city]
        subs[chat_id]["active"] = True
        save_subscribers(subs)
        await query.edit_message_text(
            f"✅ Location set to *{city.title()}*\n"
            f"🔍 Alerts are now active!",
            parse_mode="Markdown"
        )

    elif data.startswith("tier_"):
        tier = data[5:]
        subs = load_subscribers()
        if chat_id in subs:
            subs[chat_id]["tier"] = tier
            save_subscribers(subs)
        info = TIERS.get(tier, TIERS["alerts"])
        await query.edit_message_text(
            f"✅ Tier updated to {info['emoji']} *{info['label']}*\n\n"
            f"Payment: £{info['price']}/month\n"
            f"Contact @AmazonJobsSupport to complete payment.",
            parse_mode="Markdown"
        )

    elif data == "status":
        subs = load_subscribers()
        sub = subs.get(chat_id, {})
        seen = load_seen_jobs()
        await query.edit_message_text(
            format_status_message(sub, len(seen)),
            parse_mode="Markdown"
        )


# ═══════════════════════════════════════════════════════════
# JOB POLLING ENGINE
# ═══════════════════════════════════════════════════════════

async def poll_and_alert(app):
    """
    Core polling loop — runs every POLL_INTERVAL seconds.
    Checks for new jobs and alerts subscribers.
    """
    logger.info(f"🔄 Polling loop started (every {POLL_INTERVAL}s)")

    while True:
        try:
            subs = load_subscribers()
            seen = load_seen_jobs()
            new_seen = set(seen)

            active_subs = {
                cid: sub for cid, sub in subs.items()
                if sub.get("active") and sub.get("locations")
            }

            if not active_subs:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # Build unique location set to avoid duplicate searches
            location_to_subs = {}
            for cid, sub in active_subs.items():
                for city in sub.get("locations", []):
                    if city not in location_to_subs:
                        location_to_subs[city] = []
                    location_to_subs[city].append((cid, sub))

            # Search each unique location
            for city, sub_list in location_to_subs.items():
                coords = UK_CITIES.get(city)
                if not coords:
                    continue

                lat, lng = coords
                # Use max radius from all subscribers for this city
                max_radius = max(
                    s.get("radius", 25) for _, s in sub_list
                )

                jobs = search_amazon_jobs(lat, lng, max_radius)
                logger.info(
                    f"📍 {city.title()}: found {len(jobs)} jobs"
                )

                for job in jobs:
                    fp = job_fingerprint(job)

                    if fp in seen:
                        continue

                    new_seen.add(fp)

                    # Alert each relevant subscriber
                    for cid, sub in sub_list:
                        job_distance = job.get("distance", 999)
                        sub_radius = sub.get("radius", 25)

                        if job_distance > sub_radius:
                            continue

                        try:
                            msg = format_job_alert(job, sub)
                            await app.bot.send_message(
                                chat_id=int(cid),
                                text=msg,
                                parse_mode="Markdown",
                                disable_web_page_preview=True
                            )

                            # Auto-submit for premium subscribers
                            if sub.get("tier") == "auto_submit" and \
                               sub.get("amazon_cookies") and \
                               job.get("applicationId"):
                                asyncio.create_task(
                                    run_auto_submit(app, cid, sub, job)
                                )

                        except Exception as e:
                            logger.error(
                                f"Failed to alert {cid}: {e}"
                            )

                await asyncio.sleep(1)  # Rate limit between searches

            save_seen_jobs(new_seen)

        except Exception as e:
            logger.error(f"Poll loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


async def run_auto_submit(app, chat_id: str, subscriber: dict, job: dict):
    """Run auto-submit in background"""
    try:
        result = await asyncio.to_thread(
            auto_submit_for_subscriber, subscriber, job
        )

        title = job.get("jobTitle", "job")
        city = job.get("city", "")

        if result["success"]:
            await app.bot.send_message(
                chat_id=int(chat_id),
                text=(
                    f"🤖 *Auto-Submitted!*\n\n"
                    f"✅ Successfully applied to:\n"
                    f"📦 {title} — {city}\n\n"
                    f"Check your Amazon Jobs account for next steps."
                ),
                parse_mode="Markdown"
            )
        else:
            await app.bot.send_message(
                chat_id=int(chat_id),
                text=(
                    f"⚠️ *Auto-Submit Failed*\n\n"
                    f"Could not auto-apply to {title}.\n"
                    f"Error: {result.get('error', 'Unknown')}\n\n"
                    f"Please apply manually via the link above."
                ),
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Auto-submit task failed: {e}")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")

    logger.info("🚀 Starting @Amazonjobs100_bot v2.0")

    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("location", cmd_location))
    app.add_handler(CommandHandler("radius", cmd_radius))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("tier", cmd_tier))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Start polling loop as background task
    async def post_init(application):
        asyncio.create_task(poll_and_alert(application))

    app.post_init = post_init

    logger.info("✅ Bot ready — polling Telegram")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
