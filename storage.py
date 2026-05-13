import os
import json
import logging
from datetime import datetime
from config import SUBSCRIBERS_FILE, COOKIES_FILE, CHAT_ID

log = logging.getLogger(__name__)

# ─── FILE PATHS ──────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", "/opt/amazon-bot/data")
KNOWN_JOBS_FILE = os.path.join(DATA_DIR, "known_jobs.json")
JOB_HISTORY_FILE = os.path.join(DATA_DIR, "job_history.json")
ERROR_LOG_FILE = os.path.join(DATA_DIR, "errors.json")
APPLICATIONS_FILE = os.path.join(DATA_DIR, "applications.json")

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

# ─── KNOWN JOBS ──────────────────────────────────────────────────────────────
def load_known_jobs():
    try:
        if os.path.exists(KNOWN_JOBS_FILE):
            with open(KNOWN_JOBS_FILE, "r") as f:
                return set(json.load(f))
    except:
        pass
    return set()

def save_known_jobs(jobs):
    try:
        with open(KNOWN_JOBS_FILE, "w") as f:
            json.dump(list(jobs), f)
    except Exception as e:
        log.error(f"save_known_jobs error: {e}")

# alias
save_known_job = save_known_jobs

def is_known_job(job_id, known_jobs):
    return job_id in known_jobs

def mark_job_known(job_id, known_jobs):
    known_jobs.add(job_id)
    return known_jobs

# ─── JOB HISTORY ─────────────────────────────────────────────────────────────
def load_job_history():
    try:
        if os.path.exists(JOB_HISTORY_FILE):
            with open(JOB_HISTORY_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return []

def save_job_history(history):
    try:
        with open(JOB_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        log.error(f"save_job_history error: {e}")

# ─── ERROR LOG ────────────────────────────────────────────────────────────────
def log_error(error_type, detail=""):
    try:
        errors = []
        if os.path.exists(ERROR_LOG_FILE):
            with open(ERROR_LOG_FILE, "r") as f:
                errors = json.load(f)
        errors.append({
            "type": error_type,
            "detail": detail,
            "ts": datetime.utcnow().isoformat()
        })
        errors = errors[-500:]
        with open(ERROR_LOG_FILE, "w") as f:
            json.dump(errors, f, indent=2)
    except Exception as e:
        log.error(f"log_error failed: {e}")

# ─── APPLICATIONS ─────────────────────────────────────────────────────────────
def save_application(application):
    try:
        apps = []
        if os.path.exists(APPLICATIONS_FILE):
            with open(APPLICATIONS_FILE, "r") as f:
                apps = json.load(f)
        apps.append(application)
        with open(APPLICATIONS_FILE, "w") as f:
            json.dump(apps, f, indent=2)
    except Exception as e:
        log.error(f"save_application error: {e}")

# ─── COOKIE AGE ──────────────────────────────────────────────────────────────
def get_cookie_age_hours(cookie_timestamp):
    try:
        from datetime import timezone
        ts = datetime.fromisoformat(str(cookie_timestamp))
        now = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600
    except:
        return 9999.0
