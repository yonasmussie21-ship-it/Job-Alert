import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp

from config import BOT_TOKEN, CHAT_ID, TELEGRAM_API, get_tier
from job_parser import is_fresh_job, is_night_shift
from storage import save_subscribers

log = logging.getLogger(__name__)

otp_waiting: Dict[str, asyncio.Event] = {}
otp_codes: Dict[str, str] = {}
onboarding: Dict[str, Dict[str, Any]] = {}


async def tg_send(text: str, reply_markup: Optional[dict] = None, chat_id: Optional[str] = None) -> None:
    cid = chat_id or CHAT_ID

    payload = {
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=20) as response:
                if response.status >= 400:
                    body = await response.text()
                    log.warning("[TELEGRAM_SEND_FAILED] status=%s body=%s", response.status, body[:300])
    except Exception as exc:
        log.warning("[TELEGRAM_ERROR] %s", exc)


def _job_link(job: Dict[str, Any], apply_url: Optional[str] = None) -> str:
    if apply_url:
        return apply_url

    job_id = job.get("id", "")

    if job_id and job_id != "TEST-001":
        return f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}"

    return job.get("link", "https://www.jobsatamazon.co.uk")


async def tg_alert(
    job: Dict[str, Any],
    status: str = "new",
    chat_id: Optional[str] = None,
    distance: Optional[float] = None,
    account_id: Optional[int] = None,
    shift_index: Optional[int] = None,
    total_shifts: Optional[int] = None,
    score: Optional[int] = None,
    apply_url: Optional[str] = None,
) -> None:
    cid = chat_id or CHAT_ID

    headers_map = {
        "new": "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>",
        "applying": f"🤖 <b>PREPARING APPLICATION{' — Account ' + str(account_id) if account_id else ''}</b>",
        "prepared": f"✅ <b>APPLICATION PREPARED{' — Account ' + str(account_id) if account_id else ''}</b>",
        "applied": f"✅ <b>APPLICATION SUBMITTED{' — Account ' + str(account_id) if account_id else ''}</b>",
        "ready": "👆 <b>APPLICATION READY — OPEN MANUALLY</b>",
        "fresh_alert": "🌿 <b>AMAZON FRESH — MANUAL APPLY ONLY</b>",
        "cookie_expired": f"⚠️ <b>COOKIE EXPIRED{' — Account ' + str(account_id) if account_id else ''}</b>",
    }

    header = headers_map.get(status, "⚠️ <b>JOB UPDATE</b>")

    shifts = job.get("shifts", []) or []
    schedule = job.get("schedule")

    if shift_index is not None and shifts and shift_index < len(shifts):
        schedule = shifts[shift_index]

    pay = job.get("pay")
    pay_display = job.get("pay_display")

    if pay_display:
        pay_str = pay_display
    elif isinstance(pay, (int, float)):
        pay_str = f"{pay:.2f}"
    else:
        pay_str = str(pay or "?")

    night = " 🌙 NIGHT SHIFT" if is_night_shift(schedule or "") else ""
    fresh = " 🌿 FRESH" if is_fresh_job(job) else ""
    permanent = " ⭐ PERMANENT" if "permanent" in str(job.get("contract", "")).lower() else ""

    distance_str = f"\n📏 Distance: <b>{distance} miles</b>" if distance is not None else ""
    score_str = f"\n⭐ Score: <b>{score}</b>" if score else ""

    shift_str = ""
    if total_shifts and total_shifts > 1 and shift_index is not None:
        shift_str = f"\n🔄 <b>Shift {shift_index + 1} of {total_shifts}</b>"

    description = job.get("description")
    desc_str = f"\n📝 {description}" if description else ""

    first_day = job.get("firstDay") or "See listing"
    schedule_text = schedule or "See listing"
    hours = job.get("hours") or "See listing"

    text = f"""{header}{shift_str}{permanent}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location', 'Unknown')}</b>
📦 {job.get('title', 'Warehouse Operative')}{night}{fresh}
💰 <b>£{pay_str}/hr</b>
📋 {job.get('contract', 'Unknown')}
📅 First Day: <b>{first_day}</b>
🕘 Schedule: <b>{schedule_text}</b>
🕐 Hours/Week: <b>{hours}</b>{distance_str}{score_str}{desc_str}
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "applied":
        text += "\n🎉 <b>Submitted. Check your Amazon Jobs dashboard.</b>"
    elif status == "prepared":
        text += "\n✅ <b>Best shift selected. Open to review or continue manually.</b>"
    elif status == "ready":
        text += "\n👆 <b>Open application and submit manually.</b>"
    elif status == "fresh_alert":
        text += "\n🌿 <b>Fresh excluded from auto-submit.</b>"
    elif status == "cookie_expired":
        text += "\n🔐 <b>Refresh cookies before the bot can prepare/apply.</b>"

    link = _job_link(job, apply_url=apply_url)

    markup = None
    if status in {"new", "ready", "prepared", "fresh_alert", "cookie_expired"}:
        markup = {
            "inline_keyboard": [
                [{"text": "🚀 OPEN APPLICATION", "url": link}],
                [
                    {"text": "✅ MARK SUBMITTED", "callback_data": f"applied_{job.get('id', '')}"},
                    {"text": "❌ IGNORE", "callback_data": f"skip_{job.get('id', '')}"},
                ],
            ]
        }

    await tg_send(text, markup, chat_id=cid)


async def send_all_shifts(
    job: Dict[str, Any],
    status: str = "new",
    chat_id: Optional[str] = None,
    distance: Optional[float] = None,
    score: Optional[int] = None,
) -> None:
    shifts = job.get("shifts", []) or []

    if len(shifts) <= 1:
        await tg_alert(job, status, chat_id=chat_id, distance=distance, score=score)
        return

    for index in range(len(shifts)):
        await tg_alert(
            job,
            status,
            chat_id=chat_id,
            distance=distance,
            shift_index=index,
            total_shifts=len(shifts),
            score=score,
        )
        await asyncio.sleep(0.5)


async def start_onboarding(cid: str, name: str = "there") -> None:
    onboarding[cid] = {
        "step": "job_type",
        "locations": [],
        "name": name,
    }

    await tg_send(
        f"""👑 <b>Welcome {name}!</b>

I'm the Amazon Warehouse Job Alert Bot.

<b>Step 1 of 4 — Job Type</b>
1️⃣ Full-time only
2️⃣ Part-time only
3️⃣ Both""",
        chat_id=cid,
    )


async def handle_onboarding(cid: str, text: str, subscribers: Dict[str, Any]) -> None:
    state = onboarding.get(cid, {})
    step = state.get("step", "")

    if step == "job_type":
        mapping = {
            "1": "fulltime",
            "2": "parttime",
            "3": "both",
            "1️⃣": "fulltime",
            "2️⃣": "parttime",
            "3️⃣": "both",
        }

        job_type = mapping.get(text.strip())

        if not job_type:
            await tg_send("Please reply 1, 2 or 3.", chat_id=cid)
            return

        labels = {
            "fulltime": "Full-time only",
            "parttime": "Part-time only",
            "both": "Both",
        }

        state["job_type"] = job_type
        state["step"] = "location_1"
        onboarding[cid] = state

        await tg_send(
            f"""✅ Job type: <b>{labels[job_type]}</b>

<b>Step 2 of 4 — Your Location</b>
Enter your city or postcode:
Examples: <b>Birmingham</b>, <b>Leeds</b>, <b>B1 1BB</b>""",
            chat_id=cid,
        )

    elif step == "location_1":
        location = text.strip()

        if not location:
            await tg_send("Please enter a valid city or postcode.", chat_id=cid)
            return

        state["locations"] = [location]
        state["step"] = "location_2"
        onboarding[cid] = state

        await tg_send(
            f"""✅ Location: <b>{location}</b>

<b>Step 3 of 4 — Second Location Optional</b>
Add another location or type <b>DONE</b> to skip.""",
            chat_id=cid,
        )

    elif step == "location_2":
        value = text.strip()

        if value.upper() not in {"DONE", "SKIP", "NO", "N"}:
            state.setdefault("locations", []).append(value)

        state["step"] = "radius"
        onboarding[cid] = state

        await tg_send(
            """<b>Step 4 of 4 — Travel Radius</b>
How far can you travel?
🚗 <b>10</b> — Local
🚗 <b>25</b> — Nearby
🚗 <b>50</b> — Wide search""",
            chat_id=cid,
        )

    elif step == "radius":
        match = re.search(r"\d+", text)

        if not match:
            await tg_send("Please enter a number, for example <b>25</b>.", chat_id=cid)
            return

        radius = int(match.group())

        state["radius"] = radius
        state["step"] = "confirm"
        onboarding[cid] = state

        locations = state.get("locations", [])
        job_type = state.get("job_type", "both")

        job_label = {
            "fulltime": "Full-time only",
            "parttime": "Part-time only",
            "both": "Full-time & Part-time",
        }.get(job_type, "Both")

        location_text = "\n".join(
            f"📍 {'⭐ ' if index == 0 else ''}{location}"
            for index, location in enumerate(locations)
        )

        await tg_send(
            f"""<b>Confirm Your Preferences</b>
━━━━━━━━━━━━━━━━━
{location_text}
🚗 Radius: <b>{radius} miles</b>
📋 Type: <b>{job_label}</b>
━━━━━━━━━━━━━━━━━
Reply <b>CONFIRM</b> or <b>RESTART</b>""",
            chat_id=cid,
        )

    elif step == "confirm":
        value = text.strip().upper()

        if value == "CONFIRM":
            subscribers[cid] = {
                "name": state.get("name", "Friend"),
                "locations": state.get("locations", []),
                "radius": state.get("radius", 30),
                "job_type": state.get("job_type", "both"),
                "setup_complete": True,
                "auto_apply": False,
                "tier": "free",
                "joined": datetime.utcnow().isoformat(),
            }

            save_subscribers(subscribers)
            onboarding.pop(cid, None)

            await tg_send(
                """🎉 <b>You're all set!</b>

You'll get alerts when Amazon warehouse jobs drop near you.

Use /help for commands.""",
                chat_id=cid,
            )

        elif value == "RESTART":
            await start_onboarding(cid, state.get("name", "there"))
        else:
            await tg_send("Reply <b>CONFIRM</b> or <b>RESTART</b>.", chat_id=cid)


async def handle_updates(state: Dict[str, Any]) -> None:
    offset = 0
    processed: set[int] = set()

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{TELEGRAM_API}/getUpdates",
                    params={"offset": offset, "timeout": 10},
                    timeout=20,
                ) as response:
                    data = await response.json()

            for update in data.get("result", []):
                update_id = update["update_id"]
                offset = update_id + 1

                if update_id in processed:
                    continue

                processed.add(update_id)

                if len(processed) > 1000:
                    processed.clear()

                await _process_update(update, state)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[TELEGRAM_UPDATE_ERROR] %s", exc)

        await asyncio.sleep(2)


async def _process_update(update: Dict[str, Any], state: Dict[str, Any]) -> None:
    subscribers = state.setdefault("subscribers", {})
    known_jobs = state.setdefault("known_jobs", {})
    job_history = state.setdefault("job_history", [])

    if "callback_query" in update:
        callback = update["callback_query"]
        data = callback.get("data", "")

        if data.startswith("applied_"):
            await tg_send("✅ Marked as submitted. Good luck.")
        elif data.startswith("skip_"):
            await tg_send("❌ Job ignored.")

        return

    message = update.get("message", {})
    text = message.get("text", "").strip()
    chat = message.get("chat", {})
    cid = str(chat.get("id", CHAT_ID))
    name = chat.get("first_name", "Friend")
    text_lw = text.lower()

    if cid in otp_waiting and text and text.isdigit():
        otp_codes[cid] = text
        otp_waiting[cid].set()
        await tg_send("✅ OTP received.", chat_id=cid)
        return

    if cid in onboarding:
        await handle_onboarding(cid, text, subscribers)
        return

    if text_lw == "/start":
        if cid in subscribers and subscribers[cid].get("setup_complete"):
            sub = subscribers[cid]
            locations = ", ".join(sub.get("locations", []))
            tier = get_tier(cid, sub)

            mode = (
                "✅ Full auto-submit"
                if tier in {"owner", "premium"}
                else "📋 Prepare & notify"
                if tier == "standard"
                else "🔔 Alerts only"
            )

            await tg_send(
                f"""👋 <b>Welcome back {name}!</b>

📍 {locations}
🚗 {sub.get('radius', 30)} miles
📋 {sub.get('job_type', 'both')}
🤖 {mode}

Use /help for commands.""",
                chat_id=cid,
            )
        else:
            await start_onboarding(cid, name)

    elif text_lw == "/setup":
        await start_onboarding(cid, name)

    elif text_lw == "/status":
        from config import get_proxy_url, is_peak_time

        peak = is_peak_time()
        speed = "3s PEAK" if peak else "10s Normal"
        proxy = "✅ Configured" if get_proxy_url() else "❌ No proxy"

        await tg_send(
            f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {"⏸️ PAUSED" if state.get('bot_paused') else "✅ RUNNING"}
🌐 Proxy: {proxy}
👥 Subscribers: {len(subscribers)}
🤖 Accounts: {len(state.get('accounts', []))}
Jobs tracked: {len(known_jobs)}
History: {len(job_history)}
⚡ Speed: {speed}
━━━━━━━━━━━━━━━━━""",
            chat_id=cid,
        )

    elif text_lw == "/scrape":
        await tg_send("🔍 <b>Scanning Amazon jobs...</b>", chat_id=cid)

        from scheduler import check_jobs

        count = await check_jobs(state)

        await tg_send(
            f"✅ New: {count} | Tracked: {len(known_jobs)}\n"
            f"{'🎉 New jobs found.' if count > 0 else '⏳ No new jobs this scan.'}",
            chat_id=cid,
        )

    elif text_lw == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet — send /scrape to scan.", chat_id=cid)
            return

        text_out = f"📋 <b>Last {min(5, len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"

        for job in list(known_jobs.values())[-5:]:
            night = "🌙" if is_night_shift(job.get("schedule", "")) else "☀️"
            schedule = job.get("schedule") or "See listing"
            first_day = job.get("firstDay") or "See listing"

            text_out += (
                f"{night} {job.get('location')}\n"
                f"💰 £{job.get('pay')}/hr | {job.get('contract')}\n"
                f"📅 {first_day} | {schedule[:30]}\n\n"
            )

        await tg_send(text_out, chat_id=cid)

    elif text_lw == "/test":
        test_job = {
            "id": "TEST-001",
            "title": "Warehouse Operative",
            "location": "Weybridge, England KT13 0YU",
            "postcode": "KT13 0YU",
            "pay": 15.30,
            "pay_display": "15.30",
            "contract": "Seasonal | Full-time",
            "firstDay": "2026-05-10",
            "shifts": [
                "Sat, Sun, Mon, Tue 23:45 - 10:15",
                "Fri, Sat, Sun, Mon, Tue 6:30 - 13:00",
                "Fri, Sat, Sun, Mon 23:45 - 10:15",
            ],
            "schedule": "Sat, Sun, Mon, Tue 23:45 - 10:15",
            "hours": "40",
            "description": "Pick, pack and ship parcels at our fulfilment centre.",
            "link": "https://www.jobsatamazon.co.uk",
        }

        await send_all_shifts(test_job, "new", chat_id=cid, distance=47.0, score=15)

    elif text_lw == "/pause" and cid == str(CHAT_ID):
        state["bot_paused"] = True
        await tg_send("⏸️ Bot paused.", chat_id=cid)

    elif text_lw == "/resume" and cid == str(CHAT_ID):
        state["bot_paused"] = False
        await tg_send("▶️ Bot resumed.", chat_id=cid)

    elif text_lw == "/clearcache" and cid == str(CHAT_ID):
        known_jobs.clear()
        await tg_send("🗑️ Cache cleared.", chat_id=cid)

    elif text_lw == "/help":
        await tg_send(
            """👑 <b>Amazon Bot Commands</b>
━━━━━━━━━━━━━━━━━
/start — Welcome & setup
/setup — Update preferences
/status — Bot status
/scrape — Scan now
/jobs — Recent jobs
/test — Test alert
/pause — Pause bot
/resume — Resume bot
/clearcache — Clear cache
━━━━━━━━━━━━━━━━━""",
            chat_id=cid,
        )
