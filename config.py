import os
import pytz
from datetime import datetime

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
ENABLE_FULL_SUBMIT = os.environ.get("ENABLE_FULL_SUBMIT", "false").lower() == "true"
TELEGRAM_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
DATA_DIR = os.environ.get("DATA_DIR", "/home/ubuntu/Job-Alert/data")
TZ = pytz.timezone("Europe/London")

def now_london():
    return datetime.now(TZ)

def is_peak_time():
    now = now_london()
    return 6 <= now.hour < 23

def get_proxy_url():
    return os.environ.get("PROXY_URL", None)

def get_tier(user_id=None):
    return os.environ.get("USER_TIER", "free")

BLOCKED_KEYWORDS = ["agency", "driver", "manager", "intern"]

FRESH_KEYWORDS = ["new", "just posted", "today"]

WAREHOUSE_KEYWORDS = ["warehouse", "fulfillment", "sortation", "delivery station"]

CITY_POSTCODES = {
    "birmingham": "B1",
    "coventry": "CV1",
    "wolverhampton": "WV1",
    "dudley": "DY1",
    "walsall": "WS1",
    "derby": "DE1",
    "nottingham": "NG1",
    "leicester": "LE1",
    "stoke": "ST1",
    "shrewsbury": "SY1",
    "hereford": "HR1",
    "worcester": "WR1",
    "gloucester": "GL1",
    "oxford": "OX1",
    "milton keynes": "MK1",
    "northampton": "NN1",
    "luton": "LU1",
    "london": "E1",
    "bristol": "BS1",
    "swindon": "SN1",
    "reading": "RG1",
    "manchester": "M1",
    "leeds": "LS1",
    "sheffield": "S1",
    "liverpool": "L1",
    "newcastle": "NE1",
    "bradford": "BD1",
    "hull": "HU1",
    "york": "YO1",
}

CITY_COORDS = {
    "birmingham": (52.4862, -1.8904),
    "coventry": (52.4068, -1.5197),
    "wolverhampton": (52.5862, -2.1288),
    "dudley": (52.5126, -2.0813),
    "walsall": (52.5860, -1.9820),
    "derby": (52.9225, -1.4746),
    "nottingham": (52.9548, -1.1581),
    "leicester": (52.6369, -1.1398),
    "stoke": (53.0027, -2.1794),
    "london": (51.5074, -0.1278),
    "manchester": (53.4808, -2.2426),
    "leeds": (53.8008, -1.5491),
    "sheffield": (53.3811, -1.4701),
    "liverpool": (53.4084, -2.9916),
    "bristol": (51.4545, -2.5879),
    "swindon": (51.5558, -1.7797),
}
