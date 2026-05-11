import os
import json
import logging
from datetime import datetime
from config import SUBSCRIBERS_FILE, COOKIES_FILE, CHAT_ID

log = logging.getLogger(__name__)

# ─── COOKIES ─────────────────────────────────────────────────────────────────
def load_cookies():
    try:
        if os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return []

def save_cookies(cookies):
    try:
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies, f)
        log.info(f"✅ Saved {len(cookies)} cookies")
    except Exception as e:
        log.error(f"Cookie save error: {e}")

# ─── SUBSCRIBERS ─────────────────────────────────────────────────────────────
def load_subscribers():
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {}

def save_subscribers(subs):
    try:
        with open(SUBSCRIBERS_FILE, "w") as f:
            json.dump(subs, f, indent=2)
    except Exception as e:
        log.error(f"Save subscribers error: {e}")

def init_subscribers():
    """Load subscribers and ensure owner exists."""
    subscribers = load_subscribers()
    if CHAT_ID not in subscribers:
        subscribers[CHAT_ID] = {
            "name":           "Yonas",
            "locations":      ["Birmingham"],
            "radius":         50,
            "job_type":       "both",
            "setup_complete": True,
            "auto_apply":     True,
            "tier":           "owner",
            "joined":         datetime.utcnow().isoformat(),
        }
    else:
        subscribers[CHAT_ID]["auto_apply"] = True
        subscribers[CHAT_ID]["tier"]       = "owner"
    save_subscribers(subscribers)
    return subscribers
