import asyncio
import logging
import aiohttp
from collections import defaultdict

from config import (
    BOT_TOKEN, CHAT_ID, TELEGRAM_API,
    get_proxy_url, is_peak_time, load_accounts
)
from storage import init_subscribers
from telegram_bot import tg_send, handle_updates
from scheduler import check_jobs, send_daily_summary

log = logging.getLogger(__name__)

async def main():
    log.info("👑 Amazon KING BOT v17 Starting!")
    log.info(f"🌐 Proxy: {'Decodo ✅' if get_proxy_url() else '❌'}")

    # ── Initialise shared state ───────────────────────────────────────────────
    subscribers = init_subscribers()
    accounts    = load_accounts()

    state = {
        "subscribers":   subscribers,
        "known_jobs":    {},
        "job_history":   [],
        "bot_paused":    False,
        "accounts":      accounts,
        "posting_times": defaultdict(list),
    }

    log.info(f"👥 Subscribers: {len(subscribers)} | 🤖 Accounts: {len(accounts)}")

    # ── Clear pending Telegram updates on startup ─────────────────────────────
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
                    log.info(f"✅ Cleared {len(results)} pending updates")
    except Exception as e:
        log.warning(f"Could not clear updates: {e}")

    # ── Start background tasks ────────────────────────────────────────────────
    asyncio.create_task(handle_updates(state))
    asyncio.create_task(send_daily_summary(state))

    # ── Startup message ───────────────────────────────────────────────────────
    await asyncio.sleep(2)
    await tg_send(f"""👑 <b>Amazon KING BOT v17 ONLINE!</b>
━━━━━━━━━━━━━━━━━
✅ Fixed submit endpoint
✅ scheduleId now included
✅ candidateSFId resolved
✅ One search → ALL UK jobs
✅ 36hr+ filter (no part-time)
✅ Decodo UK proxy
✅ Modular codebase
⚡ 3s peak / 10s normal
━━━━━━━━━━━━━━━━━
🌐 Proxy: {'✅ Decodo UK' if get_proxy_url() else '❌'}
👥 {len(subscribers)} subscriber(s) | 🤖 {len(accounts)} account(s)
━━━━━━━━━━━━━━━━━
Send /test to preview!""")

    # ── Main loop ─────────────────────────────────────────────────────────────
    await check_jobs(state)

    while True:
        await asyncio.sleep(3 if is_peak_time() else 10)
        await check_jobs(state)


if __name__ == "__main__":
    asyncio.run(main())
