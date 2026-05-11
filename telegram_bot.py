import asyncio
import json
import logging
import re
import aiohttp
from datetime import datetime
from config import BOT_TOKEN, CHAT_ID, TELEGRAM_API, get_tier
from storage import save_subscribers
from job_parser import is_night_shift, is_fresh_job, score_job, job_distance_miles

log = logging.getLogger(__name__)

# ─── STATE (passed in from main) ─────────────────────────────────────────────
otp_waiting = {}
otp_codes   = {}
onboarding  = {}

# ─── CORE SEND ────────────────────────────────────────────────────────────────
async def tg_send(text, reply_markup=None, chat_id=None):
    cid     = chat_id or CHAT_ID
    payload = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{TELEGRAM_API}/sendMessage", json=payload)
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ─── ALERTS ──────────────────────────────────────────────────────────────────
async def tg_alert(job, status="new", chat_id=None, distance=None,
                   account_id=None, shift_index=None, total_shifts=None, score=None):
    cid = chat_id or CHAT_ID

    headers_map = {
        "new":         "🚨 <b>NEW AMAZON JOB — ACT NOW!</b>",
        "applying":    f"🤖 <b>AUTO-SUBMITTING{' (Acc '+str(account_id)+')' if account_id else ''}...</b>",
        "applied":     f"✅ <b>APPLIED FOR YOU{' (Acc '+str(account_id)+')' if account_id else ''}!</b>",
        "ready":       "👆 <b>APPLICATION READY — TAP TO SUBMIT!</b>",
        "prepared":    "✅ <b>SHIFT SELECTED — TAP TO CONTINUE!</b>",
        "fresh_alert": "🌿 <b>AMAZON FRESH — MANUAL APPLY ONLY</b>",
    }
    header = headers_map.get(status, "⚠️ <b>OPEN MANUALLY!</b>")

    pay_str  = job.get("pay_display") or f"{job.get('pay','?'):.2f}"
    dist_str = f"\n📏 Distance: <b>{distance} miles</b>" if distance else ""

    shifts   = job.get("shifts", [])
    schedule = job.get("schedule")
    if shift_index is not None and shifts and shift_index < len(shifts):
        schedule = shifts[shift_index]

    night     = " 🌙 NIGHT SHIFT" if is_night_shift(schedule or "") else ""
    fresh     = " 🌿 FRESH" if is_fresh_job(job) else ""
    perm      = " ⭐ PERMANENT" if "permanent" in job.get("contract","").lower() else ""
    shift_str = ""
    if total_shifts and total_shifts > 1 and shift_index is not None:
        shift_str = f"\n🔄 <b>Shift {shift_index+1} of {total_shifts}</b>"
    score_str = f"\n⭐ Score: <b>{score}</b>" if score else ""

    first_day_str = job.get("firstDay") or "See listing"
    schedule_str  = schedule or "See listing"
    hours_str     = job.get("hours") or "See listing"
    desc_str      = f"\n📝 {job.get('description')}" if job.get("description") else ""

    job_id   = job.get("id","")
    job_link = (f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}"
                if job_id != "TEST-001"
                else job.get("link","https://www.jobsatamazon.co.uk"))

    text = f"""{header}{shift_str}{perm}
━━━━━━━━━━━━━━━━━━━━━
📍 <b>{job.get('location','Unknown')}</b>
📦 {job.get('title','Warehouse Operative')}{night}{fresh}
💰 <b>£{pay_str}/hr</b>
📋 {job.get('contract','Seasonal')}
📅 First Day: <b>{first_day_str}</b>
🕘 Schedule: <b>{schedule_str}</b>
🕐 Hours/Week: <b>{hours_str}</b>{dist_str}{score_str}{desc_str}
━━━━━━━━━━━━━━━━━━━━━"""

    if status == "applied":
        text += "\n🎉 <b>Check your Amazon Jobs dashboard!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "ready":
        text += "\n👆 <b>Tap OPEN APPLICATION → Log in → Submit!</b>\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "prepared":
        text += "\n✅ <b>Bot selected the best shift for you!</b>\n👆 Tap OPEN APPLICATION → Schedule → Review → Submit!\n━━━━━━━━━━━━━━━━━━━━━"
    elif status == "fresh_alert":
        text += "\n🌿 <b>Fresh excluded from auto-submit</b>\n━━━━━━━━━━━━━━━━━━━━━"

    markup = {
        "inline_keyboard": [
            [{"text": "🚀 OPEN APPLICATION", "url": job_link}],
            [{"text": "✅ MARK SUBMITTED",   "callback_data": f"applied_{job['id']}"},
             {"text": "❌ IGNORE",           "callback_data": f"skip_{job['id']}"}]
        ]
    } if status in ["new","ready","prepared","fresh_alert"] else None

    await tg_send(text, markup, chat_id=cid)


async def send_all_shifts(job, status="new", chat_id=None, distance=None, score=None):
    shifts = job.get("shifts", [])
    if not shifts or len(shifts) <= 1:
        await tg_alert(job, status, chat_id=chat_id, distance=distance, score=score)
        return
    for i in range(len(shifts)):
        await tg_alert(job, status, chat_id=chat_id, distance=distance,
                       shift_index=i, total_shifts=len(shifts), score=score)
        await asyncio.sleep(0.5)

# ─── ONBOARDING ──────────────────────────────────────────────────────────────
async def start_onboarding(cid, name="there"):
    onboarding[cid] = {"step": "job_type", "locations": [], "name": name}
    await tg_send(f"""👑 <b>Welcome {name}!</b>

I'm the Amazon Warehouse Job Alert Bot.
I find UK warehouse jobs and alert you instantly!

<b>Step 1 of 4 — Job Type</b>
1️⃣ Full-time only
2️⃣ Part-time only
3️⃣ Both""", chat_id=cid)


async def handle_onboarding(cid, text, subscribers):
    state = onboarding.get(cid, {})
    step  = state.get("step", "")

    if step == "job_type":
        mapping = {"1": "fulltime", "2": "parttime", "3": "both",
                   "1️⃣": "fulltime", "2️⃣": "parttime", "3️⃣": "both"}
        jt = mapping.get(text)
        if not jt:
            await tg_send("Please reply 1, 2 or 3 ☝️", chat_id=cid)
            return
        labels = {"fulltime":"Full-time only","parttime":"Part-time only","both":"Both"}
        state["job_type"] = jt
        state["step"]     = "location_1"
        onboarding[cid]   = state
        await tg_send(f"""✅ Job type: <b>{labels[jt]}</b>

<b>Step 2 of 4 — Your Location</b>
Enter your city or postcode:
Examples: <b>Birmingham</b>, <b>Leeds</b>, <b>B1 1BB</b>""", chat_id=cid)

    elif step == "location_1":
        state["locations"] = [text.strip()]
        state["step"]      = "location_2"
        onboarding[cid]    = state
        await tg_send(f"""✅ Location: <b>{text.strip()}</b>

<b>Step 3 of 4 — Second Location (Optional)</b>
Add another location or type <b>DONE</b> to skip.""", chat_id=cid)

    elif step == "location_2":
        if text.strip().upper() not in ["DONE","SKIP","NO","N"]:
            state["locations"].append(text.strip())
        state["step"]   = "radius"
        onboarding[cid] = state
        await tg_send("""<b>Step 4 of 4 — Travel Radius</b>
How far can you travel from your location?
🚗 <b>10</b> — Very local
🚗 <b>25</b> — Nearby
🚗 <b>50</b> — Wide search""", chat_id=cid)

    elif step == "radius":
        try:
            radius = int(re.search(r'\d+', text).group())
        except:
            await tg_send("Please enter a number e.g. <b>25</b>", chat_id=cid)
            return
        state["radius"] = radius
        state["step"]   = "confirm"
        onboarding[cid] = state
        locs   = state.get("locations", [])
        jt     = state.get("job_type", "both")
        jlabel = {"fulltime":"Full-time only","parttime":"Part-time only",
                  "both":"Full-time & Part-time"}.get(jt)
        ltext  = "\n".join([f"📍 {'⭐ ' if i==0 else ''}{l}" for i, l in enumerate(locs)])
        await tg_send(f"""<b>Confirm Your Preferences</b>
━━━━━━━━━━━━━━━━━
{ltext}
🚗 Radius: <b>{radius} miles</b>
📋 Type: <b>{jlabel}</b>
━━━━━━━━━━━━━━━━━
Reply <b>CONFIRM</b> or <b>RESTART</b>""", chat_id=cid)

    elif step == "confirm":
        if text.upper() == "CONFIRM":
            subscribers[cid] = {
                "name":           state.get("name","Friend"),
                "locations":      state.get("locations",[]),
                "radius":         state.get("radius",30),
                "job_type":       state.get("job_type","both"),
                "setup_complete": True,
                "auto_apply":     False,
                "tier":           "free",
                "joined":         datetime.utcnow().isoformat(),
            }
            save_subscribers(subscribers)
            onboarding.pop(cid, None)
            await tg_send("""🎉 <b>You're all set!</b>

You'll get instant alerts when Amazon warehouse jobs drop near you!

Use /help for all commands.""", chat_id=cid)
        elif text.upper() == "RESTART":
            await start_onboarding(cid, state.get("name","there"))
        else:
            await tg_send("Reply <b>CONFIRM</b> or <b>RESTART</b>", chat_id=cid)

# ─── UPDATE HANDLER ──────────────────────────────────────────────────────────
async def handle_updates(state: dict):
    """state = {subscribers, known_jobs, job_history, bot_paused, accounts}"""
    offset    = 0
    processed = set()

    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{TELEGRAM_API}/getUpdates?offset={offset}&timeout=10"
                ) as r:
                    data = await r.json()
                    for update in data.get("result", []):
                        uid    = update["update_id"]
                        offset = uid + 1
                        if uid not in processed:
                            processed.add(uid)
                            if len(processed) > 1000:
                                processed.clear()
                            await _process_update(update, state)
        except Exception as e:
            log.error(f"Update error: {e}")
        await asyncio.sleep(2)


async def _process_update(update, state):
    subscribers = state["subscribers"]
    known_jobs  = state["known_jobs"]
    job_history = state["job_history"]

    if "callback_query" in update:
        cb = update["callback_query"]
        d  = cb.get("data","")
        if d.startswith("applied_"):
            await tg_send("✅ Marked as submitted! Good luck! 💪🔥")
        elif d.startswith("skip_"):
            await tg_send("❌ Job ignored.")
        return

    msg     = update.get("message", {})
    text    = msg.get("text","").strip()
    cid     = str(msg.get("chat",{}).get("id", CHAT_ID))
    name    = msg.get("chat",{}).get("first_name","Friend")
    text_lw = text.lower()

    # OTP handler
    if cid in otp_waiting and text and text.isdigit():
        otp_codes[cid] = text
        otp_waiting[cid].set()
        await tg_send("✅ OTP received! Submitting...", chat_id=cid)
        return

    if cid in onboarding:
        await handle_onboarding(cid, text, subscribers)
        return

    if text_lw == "/start":
        if cid in subscribers and subscribers[cid].get("setup_complete"):
            sub  = subscribers[cid]
            locs = ", ".join(sub.get("locations",[]))
            tier = get_tier(cid, sub)
            auto = ("✅ Full auto-submit" if tier in ("owner","premium") else
                    "📋 Prepare & notify" if tier == "standard" else
                    "🔔 Alerts only")
            await tg_send(f"""👋 <b>Welcome back {name}!</b>

📍 {locs}
🚗 {sub.get('radius',30)} miles
📋 {sub.get('job_type','both')}
🤖 {auto}

Use /help for all commands!""", chat_id=cid)
        else:
            await start_onboarding(cid, name)

    elif text_lw == "/setup":
        await start_onboarding(cid, name)

    elif text_lw == "/mypreferences":
        sub = subscribers.get(cid, {})
        if not sub:
            await tg_send("You haven't set up yet! Send /start", chat_id=cid)
            return
        locs   = sub.get("locations",[])
        jt     = sub.get("job_type","both")
        jlabel = {"fulltime":"Full-time only","parttime":"Part-time only",
                  "both":"Full-time & Part-time"}.get(jt,"Both")
        ltext  = "\n".join([f"📍 {'⭐ ' if i==0 else ''}{l}" for i,l in enumerate(locs)])
        tier   = get_tier(cid, sub)
        auto   = ("✅ Full auto-submit" if tier in ("owner","premium") else
                  "📋 Prepare & notify" if tier == "standard" else
                  "🔔 Alerts only")
        await tg_send(f"""📋 <b>Your Preferences</b>
━━━━━━━━━━━━━━━━━
{ltext}
🚗 Radius: {sub.get('radius',30)} miles
📋 Job type: {jlabel}
🤖 {auto}
━━━━━━━━━━━━━━━━━
Use /setup to update""", chat_id=cid)

    elif text_lw == "/status":
        from config import get_proxy_url, is_peak_time
        peak  = is_peak_time()
        speed = "3s ⚡ PEAK" if peak else "10s 🔄 Normal"
        proxy = "✅ Decodo UK" if get_proxy_url() else "❌ No proxy"
        await tg_send(f"""📊 <b>Bot Status</b>
━━━━━━━━━━━━━━━━━
Status: {"⏸️ PAUSED" if state['bot_paused'] else "✅ RUNNING"}
🌐 Proxy: {proxy}
👥 Subscribers: {len(subscribers)}
🤖 Accounts: {len(state['accounts'])}
Jobs tracked: {len(known_jobs)}
History: {len(job_history)}
⚡ Speed: {speed}
━━━━━━━━━━━━━━━━━""", chat_id=cid)

    elif text_lw == "/subscribers" and cid == CHAT_ID:
        txt = f"👥 <b>{len(subscribers)} Subscribers:</b>\n━━━━━━━━━━━\n"
        for scid, sub in subscribers.items():
            locs = ", ".join(sub.get("locations",[]))
            tier = get_tier(scid, sub)
            txt += f"• {sub.get('name','?')} | {locs} | {sub.get('radius',30)}mi | {tier}\n"
        await tg_send(txt, chat_id=cid)

    elif text_lw == "/scrape":
        await tg_send("🔍 <b>Scanning ALL UK Amazon jobs...</b>", chat_id=cid)
        from scheduler import check_jobs
        count = await check_jobs(state)
        await tg_send(
            f"✅ New: {count} | Tracked: {len(known_jobs)}\n"
            f"{'🎉 New jobs found!' if count > 0 else '⏳ No new jobs this scan'}",
            chat_id=cid
        )

    elif text_lw == "/jobs":
        if not known_jobs:
            await tg_send("📭 No jobs yet — send /scrape to scan!", chat_id=cid)
        else:
            txt = f"📋 <b>Last {min(5,len(known_jobs))} Jobs:</b>\n━━━━━━━━━━━\n"
            for job in list(known_jobs.values())[-5:]:
                night = "🌙" if is_night_shift(job.get("schedule","")) else "☀️"
                sched = job.get("schedule") or "See listing"
                day   = job.get("firstDay") or "See listing"
                txt  += f"{night} {job.get('location')}\n💰 £{job.get('pay')}/hr | {job.get('contract')}\n📅 {day} | {sched[:30]}\n\n"
            await tg_send(txt, chat_id=cid)

    elif text_lw == "/history":
        if not job_history:
            await tg_send("📭 No history yet!", chat_id=cid)
        else:
            total  = len(job_history)
            avg    = sum(j.get("pay",0) for j in job_history) / total
            best   = max(job_history, key=lambda x: x.get("pay",0))
            nights = sum(1 for j in job_history if is_night_shift(j.get("schedule","")))
            await tg_send(f"""📊 <b>History</b>
Total: {total} | 🌙 Nights: {nights}
Avg: £{avg:.2f}/hr
Best: {best.get('location','?')} £{best.get('pay','?')}/hr""", chat_id=cid)

    elif text_lw == "/test":
        test_job = {
            "id": "TEST-001", "title": "Warehouse Operative",
            "location": "Weybridge, England (West Surrey) KT13 0YU",
            "postcode": "KT13 0YU", "pay": 15.30, "pay_display": "15.30",
            "contract": "Seasonal | Full-time", "firstDay": "2026-05-10",
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

    elif text_lw == "/pause" and cid == CHAT_ID:
        state["bot_paused"] = True
        await tg_send("⏸️ Bot paused.", chat_id=cid)

    elif text_lw == "/resume" and cid == CHAT_ID:
        state["bot_paused"] = False
        await tg_send("▶️ Bot resumed! 🔥", chat_id=cid)

    elif text_lw == "/clearcache" and cid == CHAT_ID:
        known_jobs.clear()
        await tg_send("🗑️ Cache cleared — bot will re-alert all jobs next scan.", chat_id=cid)

    elif text_lw == "/help":
        await tg_send("""👑 <b>Amazon KING BOT v17</b>
━━━━━━━━━━━━━━━━━
/start          — Welcome & setup
/setup          — Update preferences
/mypreferences  — View settings
/status         — Bot status
/scrape         — Scan now
/jobs           — Recent jobs
/history        — All time stats
/test           — Test alert (3 shifts)
/subscribers    — All users (admin)
/clearcache     — Reset job cache (admin)
/pause          — Pause bot (admin)
/resume         — Resume bot (admin)
━━━━━━━━━━━━━━━━━
🔗 Share: t.me/Jibhub_bot
🆓 Free = alerts only
💰 Standard = prepared application
👑 Premium = full auto-submit
🌿 Fresh = alert only""", chat_id=cid)
