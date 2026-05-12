import os
import logging
import random
from dataclasses import dataclass, asdict
from urllib.parse import quote
from zoneinfo import ZoneInfo
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ─── TIMEZONE ────────────────────────────────────────────────────────────────
TZ = ZoneInfo("Europe/London")

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DEBUG: bool = os.environ.get("DEBUG", "0") == "1"
if DEBUG:
    log.setLevel(logging.DEBUG)
    log.debug("[CONFIG] Debug mode enabled")

# ─── GENERAL SETTINGS ────────────────────────────────────────────────────────
MAX_ACCOUNTS: int = int(os.environ.get("MAX_ACCOUNTS", "5"))
COOKIE_FRESH_HOURS: int = int(os.environ.get("COOKIE_FRESH_HOURS", "12"))

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
CHAT_ID: str = os.environ.get("CHAT_ID", "1027065157")
TELEGRAM_API: str = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

# ─── PROXY ───────────────────────────────────────────────────────────────────
DECODO_USER: str = os.environ.get("DECODO_USER", "")
DECODO_PASS: str = os.environ.get("DECODO_PASS", "")
DECODO_HOST: str = os.environ.get("DECODO_HOST", "gb.decodo.com")
DECODO_PORT: str = os.environ.get("DECODO_PORT", "30004")

PROXY_POOL: List[str] = [
    p.strip()
    for p in os.environ.get("PROXY_POOL", "").split(",")
    if p.strip()
]

# ─── AMAZON ──────────────────────────────────────────────────────────────────
AMAZON_EMAIL: str = os.environ.get("AMAZON_EMAIL", "")
AMAZON_PIN: str = os.environ.get("AMAZON_PIN", "")

# ─── STORAGE ─────────────────────────────────────────────────────────────────
DATA_DIR: str = os.environ.get("DATA_DIR", "/data" if os.path.exists("/data") else "/tmp")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH: str = os.path.join(DATA_DIR, "owner_bot.db")

# ─── JOB FILTERS ─────────────────────────────────────────────────────────────
WAREHOUSE_KEYWORDS: List[str] = [
    "warehouse", "fulfillment", "fulfilment", "sortation", "sort centre", "sort center",
    "delivery station", "fc associate", "warehouse operative", "warehouse associate",
    "sortation operative", "fulfillment associate", "fulfilment associate",
    "seasonal associate", "process assistant", "picker", "packer", "stower",
    "problem solver", "production operator", "site assistant",
    "amazon associate", "operations associate",
]

BLOCKED_KEYWORDS: List[str] = [
    "customer service", "software", "engineer", "manager", "corporate", "marketing",
    " hr ", "finance", "recruiter", "sales", "vcc", "loss prevention",
    "learning ambassador", "data entry", "legal", "it support", "business analyst",
]

FRESH_KEYWORDS: List[str] = [
    "amazon fresh",
    "whole foods",
    "fresh grocery",
]

JOB_TYPES: Dict[str, List[str]] = {
    "warehouse": WAREHOUSE_KEYWORDS,
    "fresh": FRESH_KEYWORDS,
}

CITY_POSTCODES: Dict[str, str] = {
    "birmingham": "B1 1BB",
    "london": "EC1A 1BB",
    "manchester": "M1 1AE",
    "leeds": "LS1 1BA",
    "glasgow": "G1 1AA",
    "liverpool": "L1 1JF",
    "sheffield": "S1 1AA",
    "bristol": "BS1 1AA",
    "newcastle": "NE1 1AA",
    "nottingham": "NG1 1AA",
    "leicester": "LE1 1AA",
    "coventry": "CV1 1AA",
    "wolverhampton": "WV1 1AA",
    "derby": "DE1 1AA",
    "cardiff": "CF10 1AA",
    "edinburgh": "EH1 1AA",
    "belfast": "BT1 1AA",
    "southampton": "SO14 1AA",
    "portsmouth": "PO1 1AA",
    "oxford": "OX1 1AA",
    "cambridge": "CB1 1AA",
    "reading": "RG1 1AA",
    "luton": "LU1 1AA",
    "northampton": "NN1 1AA",
    "milton keynes": "MK9 1AA",
    "warrington": "WA1 1AA",
    "hull": "HU1 1AA",
    "doncaster": "DN1 1AA",
    "chesterfield": "S40 1AA",
    "wakefield": "WF1 1AA",
    "durham": "DH1 1AA",
    "sunderland": "SR1 1AA",
    "middlesbrough": "TS1 1AA",
    "bolton": "BL1 1AA",
    "wigan": "WN1 1AA",
    "stockport": "SK1 1AA",
    "stoke": "ST1 1AA",
    "swansea": "SA1 1AA",
    "exeter": "EX1 1AA",
    "swindon": "SN1 1AA",
    "peterborough": "PE1 1AA",
    "norwich": "NR1 1AA",
    "basildon": "SS14 1AA",
    "ipswich": "IP1 1AA",
    "gloucester": "GL4 3HR",
}

CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "B1 1BB": (52.4862, -1.8904),
    "EC1A 1BB": (51.5200, -0.0990),
    "M1 1AE": (53.4808, -2.2426),
    "LS1 1BA": (53.7997, -1.5492),
    "G1 1AA": (55.8642, -4.2518),
    "L1 1JF": (53.4084, -2.9916),
    "S1 1AA": (53.3811, -1.4701),
    "BS1 1AA": (51.4545, -2.5879),
    "NE1 1AA": (54.9783, -1.6178),
    "NG1 1AA": (52.9540, -1.1549),
    "LE1 1AA": (52.6369, -1.1398),
    "CV1 1AA": (52.4068, -1.5197),
    "WV1 1AA": (52.5852, -2.1297),
    "DE1 1AA": (52.9225, -1.4746),
    "CF10 1AA": (51.4816, -3.1791),
    "EH1 1AA": (55.9533, -3.1883),
    "BT1 1AA": (54.5973, -5.9301),
    "SO14 1AA": (50.9097, -1.4044),
    "PO1 1AA": (50.7989, -1.0919),
    "OX1 1AA": (51.7520, -1.2577),
    "CB1 1AA": (52.2053, 0.1218),
    "RG1 1AA": (51.4543, -0.9781),
    "LU1 1AA": (51.8787, -0.4200),
    "NN1 1AA": (52.2405, -0.9027),
    "MK9 1AA": (52.0406, -0.7594),
    "WA1 1AA": (53.3900, -2.5970),
    "HU1 1AA": (53.7457, -0.3367),
    "DN1 1AA": (53.5228, -1.1286),
    "S40 1AA": (53.2350, -1.4216),
    "WF1 1AA": (53.6830, -1.4977),
    "DH1 1AA": (54.7761, -1.5733),
    "SR1 1AA": (54.9069, -1.3838),
    "TS1 1AA": (54.5740, -1.2343),
    "BL1 1AA": (53.5780, -2.4286),
    "WN1 1AA": (53.5450, -2.6333),
    "SK1 1AA": (53.4083, -2.1578),
    "ST1 1AA": (53.0271, -2.1772),
    "SA1 1AA": (51.6214, -3.9436),
    "EX1 1AA": (50.7236, -3.5275),
    "SN1 1AA": (51.5558, -1.7797),
    "PE1 1AA": (52.5695, -0.2405),
    "NR1 1AA": (52.6309, 1.2974),
    "SS14 1AA": (51.5790, 0.4553),
    "IP1 1AA": (52.0567, 1.1482),
    "GL4 3HR": (51.8585, -2.2180),
}

missing_coords = [
    postcode
    for postcode in CITY_POSTCODES.values()
    if postcode not in CITY_COORDS
]

if missing_coords:
    log.warning("[CONFIG] Missing coordinates for postcodes: %s", missing_coords)


@dataclass
class AmazonAccount:
    id: int
    email: str
    pin: str
    cookies: str
    session: List[Any]
    logged_in: bool = False
    priority: int = 1
    cookie_timestamp: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def now_london() -> datetime:
    return datetime.now(TZ)


def cookie_is_fresh(cookie_ts: Optional[str]) -> bool:
    if not cookie_ts:
        return False

    try:
        ts = datetime.fromisoformat(cookie_ts)

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=TZ)

        age_hours = (now_london() - ts).total_seconds() / 3600
        return age_hours < COOKIE_FRESH_HOURS

    except Exception as e:
        if DEBUG:
            log.debug("[COOKIE_TS_INVALID] %s", e)
        return False


def get_proxy_url() -> Optional[str]:
    if PROXY_POOL:
        return random.choice(PROXY_POOL)

    if DECODO_USER and DECODO_PASS:
        password = quote(DECODO_PASS, safe="")
        return f"http://{DECODO_USER}:{password}@{DECODO_HOST}:{DECODO_PORT}"

    return None


def is_peak_time() -> bool:
    now = now_london()
    h, m = now.hour, now.minute

    morning_peak = (h == 10 and m >= 55) or (h == 11 and m <= 25)
    evening_peak = (h == 22 and m >= 55) or (h == 23 and m <= 25)

    return morning_peak or evening_peak


def next_peak_window() -> str:
    return "10:55–11:25 or 22:55–23:25 (London time)"


def validate_env() -> None:
    if not BOT_TOKEN:
        log.warning("[ENV] BOT_TOKEN missing — Telegram disabled")

    if not TELEGRAM_API:
        log.warning("[ENV] TELEGRAM_API unavailable")

    if not CHAT_ID:
        log.warning("[ENV] CHAT_ID missing — owner chat not set")

    if not AMAZON_EMAIL and not os.environ.get("AMAZON_COOKIES"):
        log.warning("[ENV] No Amazon email or global cookies provided")

    if not get_proxy_url():
        log.warning("[ENV] No proxy configured")


def load_accounts() -> List[Dict[str, Any]]:
    accounts: List[AmazonAccount] = []

    for i in range(1, MAX_ACCOUNTS + 1):
        email = os.environ.get(f"AMAZON_EMAIL_{i}", "")
        pin = os.environ.get(f"AMAZON_PIN_{i}", "")
        cookies = os.environ.get(f"AMAZON_COOKIES_{i}", "")
        cookie_ts = os.environ.get(f"AMAZON_COOKIES_{i}_TS", "")
        priority_str = os.environ.get(f"AMAZON_PRIORITY_{i}", "")

        if i == 1:
            email = email or AMAZON_EMAIL
            pin = pin or AMAZON_PIN
            cookies = cookies or os.environ.get("AMAZON_COOKIES", "")
            cookie_ts = cookie_ts or os.environ.get("AMAZON_COOKIES_TS", "")

        if not (email or cookies):
            continue

        try:
            priority = int(priority_str) if priority_str else i
        except ValueError:
            priority = i

        account = AmazonAccount(
            id=i,
            email=email,
            pin=pin,
            cookies=cookies,
            session=[],
            logged_in=False,
            priority=priority,
            cookie_timestamp=cookie_ts or None,
        )

        accounts.append(account)

    accounts.sort(key=lambda account: account.priority)

    if DEBUG:
        for account in accounts:
            log.debug(
                "[ACCOUNT] id=%s email=%s priority=%s fresh_cookies=%s",
                account.id,
                account.email or "(cookies-only)",
                account.priority,
                cookie_is_fresh(account.cookie_timestamp),
            )

    return [account.as_dict() for account in accounts]


validate_env()
