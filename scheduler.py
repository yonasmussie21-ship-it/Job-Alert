import asyncio
import logging
from datetime import datetime
from collections import defaultdict
from config import CHAT_ID
from amazon_scraper import fetch_jobs, fetch_job_details
from job_parser import score_job, is_fresh_job, job_distance_miles, is_night_shift
from application_preparer import auto_submit_account
from telegram_bot import tg_send, send_all_shifts, tg_alert

log = logging.getLogger(__name__)

# ─── CHECK JOBS ──────────────────────────────────────────────────────────────
async def check_jobs(state: dict) -> int:
    """
    Main job check loop.
    state = {subscribers, known_jobs, job_history, bot_paused, accounts, posting_times}
    """
    if state.get("bot_paused"):
        return 0

    subscribers   = state["subscribers"]
    known_jobs    = state["known_jobs"]
    job_history   = state["job_history"]
    accounts      = state["accounts"]
    posting_times = state.setdefault("posting_times", defaultdict(list))

    jobs      = await fetch_jobs()
    new_count = 0

    for job in jobs:
        jid = job["id"]
        if jid in known_jobs:
            continue

        new_count += 1
        posting_times[job["location"][:20]].append(datetime.utcnow().hour)
        log.info(f"🆕 NEW: {job['location']} £{job['pay']}/hr")

        job = await fetch_job_details(job)
        known_jobs[jid] = job
        job_history.append(job)

        job_score, skip = score_job(job)
        if skip:
            continue

        # Distance for owner
        owner_prefs   = subscribers.get(CHAT_ID, {})
        job_postcode  = job.get("postcode", "")
        best_distance = None
        if job_postcode:
            for loc in owner_prefs.get("locations", ["Birmingham"]):
                d = await job_distance_miles(job_postcode, loc)
                if d is not None:
                    if best_distance is None or d < best_distance:
                        best_distance = d

        await send_all_shifts(
            job, "new", chat_id=CHAT_ID,
            distance=best_distance,
            score=job_score if job_score > 0 else None
        )

        if accounts and not is_fresh_job(job):
            log.info(f"🤖 Auto-submitting for owner: {job['location']}")
            asyncio.create_task(
                auto_submit_account(
                    job, accounts[0],
                    telegram_send=tg_send,
                    alert_fn=tg_alert,
                    chat_id=CHAT_ID,
                    tier="owner"
                )
            )

    if new_count == 0:
        log.info(f"👑 No new jobs — {len(known_jobs)} tracked")
    return new_count


# ─── DAILY SUMMARY ───────────────────────────────────────────────────────────
async def send_daily_summary(state: dict):
    while True:
        now = datetime.utcnow()
        if now.hour == 7 and now.minute == 0:
            job_history = state.get("job_history", [])
            subscribers = state.get("subscribers", {})
            today = [
                j for j in job_history
                if j.get("found_at","")[:10] == now.strftime("%Y-%m-%d")
            ]
            if today:
                best    = max(today, key=lambda x: x.get("pay",0))
                avg_pay = sum(j.get("pay",0) for j in today) / len(today)
                nights  = sum(1 for j in today if is_night_shift(j.get("schedule","")))
                await tg_send(f"""📊 <b>Daily Summary</b>
━━━━━━━━━━━━━━━━━
📅 {now.strftime('%Y-%m-%d')}
🆕 Jobs: {len(today)} | 🌙 Nights: {nights}
💰 Avg: £{avg_pay:.2f}/hr
⭐ Best: {best.get('location','?')} £{best.get('pay','?')}/hr
👥 Subscribers: {len(subscribers)}
━━━━━━━━━━━━━━━━━
Keep going Yonas! 💪""")
            await asyncio.sleep(60)
        await asyncio.sleep(30)
