import logging
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/London")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

log = logging.getLogger(__name__)

DEBUG: bool = os.environ.get("DEBUG", "0") == "1"


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    raw = env_str(name)

    if not raw:
        return default

    try:
        return int(raw)
    except ValueError:
        log.warning("[CONFIG] Invalid int for %s=%r, using %s", name, raw, default)
        return default


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name).lower()

    if not raw:
        return default

    return raw in {"1", "true", "yes", "y", "on"}


def env_list(name: str) -> List[str]:
    return [x.strip() for x in env_str(name).split(",") if x.strip()]


MAX_ACCOUNTS: int = env_int("MAX_ACCOUNTS", 5)
COOKIE_FRESH_HOURS: int = env_int("COOKIE_FRESH_HOURS", 12)

BOT_TOKEN: str = env_str("BOT_TOKEN")
CHAT_ID: str = env_str("CHAT_ID")
TELEGRAM_API: str = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

DECODO_USER: str = env_str("DECODO_USER")
DECODO_PASS: str = env_str("DECODO_PASS")
DECODO_HOST: str = env_str("DECODO_HOST", "gb.decodo.com")
DECODO_PORT: str = env_str("DECODO_PORT", "30004")
PROXY_POOL: List[str] = env_list("PROXY_POOL")

AMAZON_EMAIL: str = env_str("AMAZON_EMAIL")
AMAZON_PIN: str = env_str("AMAZON_PIN")
AMAZON_COOKIES: str = env_str("AMAZON_COOKIES")
AMAZON_COOKIES_TS: str = env_str("AMAZON_COOKIES_TS")

DEFAULT_DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
DATA_DIR: str = env_str("DATA_DIR", DEFAULT_DATA_DIR)
DB_PATH: str = os.path.join(DATA_DIR, "owner_bot.db")

PEAK_WINDOWS = [
    ((10, 55), (11, 25)),
    ((22, 55), (23, 25)),
]

WAREHOUSE_KEYWORDS: List[str] = [
    "warehouse", "fulfillment", "fulfilment", "sortation", "sort centre",
    "sort center", "delivery station", "fc associate", "warehouse operative",
    "warehouse associate", "sortation operative", "fulfillment associate",
    "fulfilment associate", "seasonal associate", "process assistant",
    "picker", "packer", "stower", "problem solver", "production operator",
    "site assistant", "amazon associate", "operations associate",
]

BLOCKED_KEYWORDS: List[str] = [
    "customer service", "software", "engineer", "manager", "corporate",
    "marketing", " hr ", "finance", "recruiter", "sales", "vcc",
    "loss prevention", "learning ambassador", "data entry", "legal",
    "it support", "business analyst",
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

_ACCOUNT_CACHE: Optional[List[Dict[str, Any]]] = None


@dataclass
class AmazonAccount:
    id: int
    email: str = ""
    pin: str = ""
    cookies: str = ""
    session: List[Any] = field(default_factory=list)
    logged_in: bool = False
    priority: int = 1
    cookie_timestamp: Optional[str] = None

    def as_dict(self, include_secrets: bool = True) -> Dict[str, Any]:
        data = asdict(self)

        if not include_secrets:
            data["pin"] = "***" if self.pin else ""
            data["cookies"] = "***" if self.cookies else ""

        return data


def now_london() -> datetime:
    return datetime.now(TZ)


def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def normalize_city(city: str) -> str:
    return str(city or "").strip().lower()


def validate_city_coords(strict: bool = False) -> None:
    missing = [
        postcode
        for postcode in CITY_POSTCODES.values()
        if postcode not in CITY_COORDS
    ]

    if missing:
        message = f"Missing coordinates for postcodes: {missing}"

        if strict:
            raise RuntimeError(message)

        log.warning("[CONFIG] %s", message)


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


def _time_to_minutes(hour: int, minute: int) -> int:
    return hour * 60 + minute


def is_peak_time() -> bool:
    now = now_london()
    current = _time_to_minutes(now.hour, now.minute)

    for start, end in PEAK_WINDOWS:
        start_min = _time_to_minutes(*start)
        end_min = _time_to_minutes(*end)

        if start_min <= current <= end_min:
            return True

    return False


def next_peak_window() -> str:
    return "10:55–11:25 or 22:55–23:25 (London time)"


def get_tier(chat_id: str, subscriber: Dict[str, Any]) -> str:
    if CHAT_ID and str(chat_id) == str(CHAT_ID):
        return "owner"

    return subscriber.get("tier", "free")


def load_accounts(force_reload: bool = False) -> List[Dict[str, Any]]:
    global _ACCOUNT_CACHE

    if _ACCOUNT_CACHE is not None and not force_reload:
        return [dict(account) for account in _ACCOUNT_CACHE]

    accounts: List[AmazonAccount] = []

    for i in range(1, MAX_ACCOUNTS + 1):
        email = env_str(f"AMAZON_EMAIL_{i}")
        pin = env_str(f"AMAZON_PIN_{i}")
        cookies = env_str(f"AMAZON_COOKIES_{i}")
        cookie_ts = env_str(f"AMAZON_COOKIES_{i}_TS")
        priority = env_int(f"AMAZON_PRIORITY_{i}", i)

        if i == 1:
            email = email or AMAZON_EMAIL
            pin = pin or AMAZON_PIN
            cookies = cookies or AMAZON_COOKIES
            cookie_ts = cookie_ts or AMAZON_COOKIES_TS

        if not email and not cookies:
            continue

        accounts.append(
            AmazonAccount(
                id=i,
                email=email,
                pin=pin,
                cookies=cookies,
                priority=priority,
                cookie_timestamp=cookie_ts or None,
            )
        )

    accounts.sort(key=lambda account: account.priority)

    if DEBUG:
        for account in accounts:
            log.debug("[ACCOUNT] %s", account.as_dict(include_secrets=False))

    _ACCOUNT_CACHE = [account.as_dict() for account in accounts]

    return [dict(account) for account in _ACCOUNT_CACHE]


def validate_env(strict: bool = False) -> None:
    warnings: List[str] = []
    errors: List[str] = []

    ensure_data_dir()
    validate_city_coords(strict=strict)

    if MAX_ACCOUNTS < 1:
        errors.append("MAX_ACCOUNTS must be at least 1")

    if COOKIE_FRESH_HOURS < 1:
        errors.append("COOKIE_FRESH_HOURS must be at least 1")

    if not BOT_TOKEN:
        warnings.append("BOT_TOKEN missing — Telegram disabled")

    if BOT_TOKEN and not TELEGRAM_API:
        errors.append("TELEGRAM_API unavailable")

    if not CHAT_ID:
        warnings.append("CHAT_ID missing — owner chat not set")

    accounts = load_accounts(force_reload=True)

    if not accounts:
        warnings.append("No Amazon accounts configured")

    if not get_proxy_url():
        warnings.append("No proxy configured")

    for warning in warnings:
        log.warning("[ENV] %s", warning)

    if strict:
        errors.extend(warnings)

    if errors:
        message = "; ".join(errors)

        if strict:
            raise RuntimeError(message)

        log.error("[ENV] %s", message)
