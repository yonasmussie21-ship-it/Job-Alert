"""
main.py — OWNER MODE v1 entry point
"""
import asyncio
import logging
import aiohttp
from config import TELEGRAM_API, CHAT_ID, get_proxy_url, is_peak_time, now_london, load_accounts
from storage import init_db, load_cookies, get_known_job_ids
from telegram_bot import tg_send, handle_updates
from scheduler import check_jobs, send_daily_summary, cookie_health_check

log = logging.getLogger(__name__)

async def main():
    log.info("👑 Amazon KING BOT — OWNER MODE v1 Starting!")

    # ── Init database ─────────────────────────────────────────────────────────
    init_db()

    # ── Load accounts ─────────────────────────────────────────────────────────
    accounts = load_accounts()
    log.info(f"🤖 Accounts loaded: {len(accounts)}")
    log.info(f"🌐 Proxy: {'✅ Decodo' if get_proxy_url() else '❌ None'}")

    # ── Restore known jobs from SQLite (survive restarts) ─────────────────────
    known_job_ids = get_known_job_ids()
    known_jobs    = {jid: {"id": jid} for jid in known_job_ids}
    log.info(f"💾 Restored {len(known_jobs)} known jobs from DB")

    # ── Shared state ──────────────────────────────────────────────────────────
    state = {
        "known_jobs":  known_jobs,
        "job_history": [],
        "bot_paused":  False,
        "accounts":    accounts,
    }

    # ── Clear old Telegram updates ────────────────────────────────────────────
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{TELEGRAM_API}/getUpdates?offset=-1&timeout=1"
            ) as r:
                data    = await r.json()
                results = data.get("result", [])
                if results:
                    last_id = results[-1]["update_id"]
                    await s.get(f"{TELEGRAM_API}/getUpdates?offset={last_id+1}&timeout=1")
                    log.info(f"✅ Cleared {len(results)} pending Telegram updates")
    except Exception as e:
        log.warning(f"Could not clear updates: {e}")

    # ── Start background tasks ────────────────────────────────────────────────
    asyncio.create_task(handle_updates(state))
    asyncio.create_task(send_daily_summary(state))
    asyncio.create_task(cookie_health_check(state))

    # ── Startup message ───────────────────────────────────────────────────────
    await asyncio.sleep(2)
    import os
    full_submit = os.environ.get("ENABLE_FULL_SUBMIT","false").lower() == "true"
    now         = now_london()

    await tg_send(f"""👑 <b>Amazon KING BOT — OWNER MODE v1</b>
━━━━━━━━━━━━━━━━━
✅ SQLite persistence active
✅ Shared browser context
✅ Per-account cookies
✅ HTML safe alerts
✅ Rate limited Telegram
✅ Owner-only commands
━━━━━━━━━━━━━━━━━
🕐 London time: {now.strftime('%H:%M %Z')}
🌐 Proxy: {'✅ Decodo UK' if get_proxy_url() else '❌ None'}
🤖 Accounts: {len(accounts)}
💾 Known jobs: {len(known_jobs)}
🚀 Full submit: {'✅ ON' if full_submit else '⏸️ PREPARE ONLY'}
━━━━━━━━━━━━━━━━━
Send /help for commands""")

    # ── First scan ────────────────────────────────────────────────────────────
    await check_jobs(state)

    # ── Main loop ─────────────────────────────────────────────────────────────
    while True:
        interval = 3 if is_peak_time() else 10
        await asyncio.sleep(interval)
        await check_jobs(state)


if __name__ == "__main__":
    asyncio.run(main())
