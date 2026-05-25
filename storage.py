import copy
import json
import logging
import os
import shutil
from contextlib import contextmanager
from datetime import datetime
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, TypeVar

from cryptography.fernet import Fernet, InvalidToken

from config import DATA_DIR, TZ

log = logging.getLogger(__name__)

T = TypeVar("T")

SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
KNOWN_JOBS_FILE = os.path.join(DATA_DIR, "known_jobs.json")
JOB_HISTORY_FILE = os.path.join(DATA_DIR, "job_history.json")
APPLICATIONS_FILE = os.path.join(DATA_DIR, "applications.json")
COOKIES_DIR = os.path.join(DATA_DIR, "cookies")
ERROR_LOG_FILE = os.path.join(DATA_DIR, "errors.log")

MAX_JOB_HISTORY = 1000
MAX_ERROR_LOG_BYTES = 2_000_000
MAX_BACKUPS_PER_FILE = 5
MAX_APPLICATION_HISTORY = 20

_locks: Dict[str, RLock] = {}
_global_lock = RLock()
_known_jobs_cache: Optional[Dict[str, Any]] = None

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


def _file_lock(path: str) -> RLock:
    real_path = os.path.abspath(path)

    with _global_lock:
        if real_path not in _locks:
            _locks[real_path] = RLock()
        return _locks[real_path]


@contextmanager
def _locked(path: str):
    with _file_lock(path):
        yield


def _cleanup_backups(path: str, suffix: str) -> None:
    try:
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        prefix = f"{base}.{suffix}."

        backups = sorted(
            [
                os.path.join(directory, f)
                for f in os.listdir(directory)
                if f.startswith(prefix)
            ],
            key=os.path.getmtime,
        )

        for old in backups[:-MAX_BACKUPS_PER_FILE]:
            os.remove(old)

    except Exception as e:
        log.warning("[BACKUP_CLEANUP_FAILED] %s: %s", path, e)


def _backup_file(path: str, suffix: str) -> Optional[str]:
    try:
        if not os.path.exists(path):
            return None

        ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S_%f")
        backup_path = f"{path}.{suffix}.{ts}"

        shutil.copy2(path, backup_path)
        _cleanup_backups(path, suffix)

        return backup_path

    except Exception as e:
        log.warning("[STORAGE_BACKUP_FAILED] %s: %s", path, e)
        return None


def _read_json_unlocked(path: str, default: T) -> T:
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except json.JSONDecodeError as e:
        backup = _backup_file(path, "corrupt")
        log.warning("[STORAGE_JSON_CORRUPT] %s backup=%s error=%s", path, backup, e)
        return default

    except Exception as e:
        log.warning("[STORAGE_READ_FAILED] %s: %s", path, e)
        return default


def _read_json(path: str, default: T) -> T:
    with _locked(path):
        return _read_json_unlocked(path, default)


def _write_json_unlocked(path: str, data: Any, backup: bool = True) -> None:
    tmp_path = f"{path}.tmp"

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)

        if backup and os.path.exists(path):
            _backup_file(path, "bak")

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)

        dir_fd = os.open(os.path.dirname(path), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    except Exception as e:
        log.warning("[STORAGE_WRITE_FAILED] %s: %s", path, e)

        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

        raise


def _write_json(path: str, data: Any, backup: bool = True) -> None:
    with _locked(path):
        _write_json_unlocked(path, data, backup=backup)


def _update_json(
    path: str,
    default: T,
    updater: Callable[[T], T],
    backup: bool = True,
) -> T:
    with _locked(path):
        current = _read_json_unlocked(path, default)
        updated = updater(current)
        _write_json_unlocked(path, updated, backup=backup)
        return updated


def clear_known_jobs_cache() -> None:
    global _known_jobs_cache
    with _locked(KNOWN_JOBS_FILE):
        _known_jobs_cache = None


def load_subscribers() -> Dict[str, Any]:
    data = _read_json(SUBSCRIBERS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_subscribers(subscribers: Dict[str, Any]) -> None:
    if not isinstance(subscribers, dict):
        log.warning("[SUBSCRIBERS_INVALID] Expected dict")
        return

    _write_json(SUBSCRIBERS_FILE, subscribers)


def update_subscribers(
    updater: Callable[[Dict[str, Any]], Dict[str, Any]]
) -> Dict[str, Any]:
    return _update_json(SUBSCRIBERS_FILE, {}, updater)


def load_known_jobs() -> Dict[str, Any]:
    global _known_jobs_cache

    with _locked(KNOWN_JOBS_FILE):
        if _known_jobs_cache is None:
            data = _read_json_unlocked(KNOWN_JOBS_FILE, {})
            _known_jobs_cache = data if isinstance(data, dict) else {}

        return copy.deepcopy(_known_jobs_cache)


def save_known_jobs(known_jobs: Dict[str, Any]) -> None:
    global _known_jobs_cache

    if not isinstance(known_jobs, dict):
        log.warning("[KNOWN_JOBS_INVALID] Expected dict")
        return

    with _locked(KNOWN_JOBS_FILE):
        updated = copy.deepcopy(known_jobs)
        _write_json_unlocked(KNOWN_JOBS_FILE, updated, backup=True)
        _known_jobs_cache = updated


def save_known_job(job: Dict[str, Any]) -> None:
    global _known_jobs_cache

    if not isinstance(job, dict):
        log.warning("[KNOWN_JOB_INVALID] Expected dict")
        return

    job_id = job.get("id")

    if not job_id:
        log.warning("[KNOWN_JOB_MISSING_ID] %s", job)
        return

    with _locked(KNOWN_JOBS_FILE):
        if _known_jobs_cache is None:
            data = _read_json_unlocked(KNOWN_JOBS_FILE, {})
            _known_jobs_cache = data if isinstance(data, dict) else {}

        updated = copy.deepcopy(_known_jobs_cache)
        updated[str(job_id)] = copy.deepcopy(job)

        # Avoid creating a backup every few seconds during scans.
        _write_json_unlocked(KNOWN_JOBS_FILE, updated, backup=False)
        _known_jobs_cache = updated


def is_known_job(job_id: str) -> bool:
    if not job_id:
        return False

    return str(job_id) in load_known_jobs()


def mark_job_known(job: Dict[str, Any]) -> None:
    save_known_job(job)


def load_job_history() -> List[Dict[str, Any]]:
    data = _read_json(JOB_HISTORY_FILE, [])
    return data if isinstance(data, list) else []


def save_job_history(history: List[Dict[str, Any]]) -> None:
    if not isinstance(history, list):
        log.warning("[JOB_HISTORY_INVALID] Expected list")
        return

    _write_json(JOB_HISTORY_FILE, history[-MAX_JOB_HISTORY:])


def append_job_history(entry: Dict[str, Any]) -> None:
    if not isinstance(entry, dict):
        log.warning("[JOB_HISTORY_ENTRY_INVALID] Expected dict")
        return

    def updater(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(history, list):
            history = []

        history.append(entry)
        return history[-MAX_JOB_HISTORY:]

    _update_json(JOB_HISTORY_FILE, [], updater)


def save_application(job_id: str, app_id: str, status: str) -> None:
    if not job_id or not app_id:
        log.warning("[APPLICATION_INVALID] job_id=%s app_id=%s", job_id, app_id)
        return

    now = _now_iso()

    def updater(apps: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(apps, dict):
            apps = {}

        existing = apps.get(str(job_id), {})
        existing_history = existing.get("history", [])

        if not isinstance(existing_history, list):
            existing_history = []

        history = [
            *existing_history,
            {
                "status": status,
                "app_id": app_id,
                "at": now,
            },
        ][-MAX_APPLICATION_HISTORY:]

        apps[str(job_id)] = {
            "app_id": app_id,
            "status": status,
            "saved_at": existing.get("saved_at", now),
            "updated_at": now,
            "history": history,
        }

        return apps

    _update_json(APPLICATIONS_FILE, {}, updater)


def load_applications() -> Dict[str, Any]:
    data = _read_json(APPLICATIONS_FILE, {})
    return data if isinstance(data, dict) else {}


def load_application(job_id: str) -> Optional[Dict[str, Any]]:
    if not job_id:
        return None

    apps = load_applications()
    app = apps.get(str(job_id))

    return copy.deepcopy(app) if isinstance(app, dict) else None


def _cookie_path(account_id: int) -> str:
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        raise ValueError("account_id must be an integer")

    if account_id <= 0:
        raise ValueError("account_id must be positive")

    return os.path.join(COOKIES_DIR, f"account_{account_id}.json")


def _get_fernet() -> Optional[Fernet]:
    key = os.environ.get("COOKIE_STORAGE_KEY", "").strip()

    if not key:
        return None

    try:
        return Fernet(key.encode("utf-8"))
    except Exception as e:
        log.warning("[COOKIE_KEY_INVALID] %s", e)
        return None


def _encode_cookie_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    fernet = _get_fernet()

    if not fernet:
        return payload

    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encrypted = fernet.encrypt(raw).decode("utf-8")

    return {
        "encrypted": True,
        "algorithm": "fernet",
        "payload": encrypted,
        "saved_at": payload.get("saved_at"),
        "hvhcid": payload.get("hvhcid", ""),
    }


def _decode_cookie_payload(data: Any) -> Any:
    if not isinstance(data, dict) or not data.get("encrypted"):
        return data

    if data.get("algorithm") != "fernet":
        log.warning("[COOKIE_DECRYPT_UNSUPPORTED_ALGORITHM] %s", data.get("algorithm"))
        return None

    fernet = _get_fernet()

    if not fernet:
        log.warning("[COOKIE_DECRYPT_SKIPPED] missing COOKIE_STORAGE_KEY")
        return None

    try:
        token = data.get("payload", "").encode("utf-8")
        raw = fernet.decrypt(token)
        return json.loads(raw.decode("utf-8"))

    except InvalidToken:
        log.warning("[COOKIE_DECRYPT_INVALID_TOKEN]")
        return None

    except Exception as e:
        log.warning("[COOKIE_DECRYPT_FAILED] %s", e)
        return None


def load_cookie_record(account_id: int) -> Optional[Dict[str, Any]]:
    data = _read_json(_cookie_path(account_id), None)
    decoded = _decode_cookie_payload(data)

    return copy.deepcopy(decoded) if isinstance(decoded, dict) else None


def load_cookies(account_id: int) -> Optional[List[Dict[str, Any]]]:
    data = load_cookie_record(account_id)

    if not isinstance(data, dict):
        return None

    cookies = data.get("cookies")

    if not isinstance(cookies, list):
        return None

    if not all(isinstance(cookie, dict) for cookie in cookies):
        log.warning("[COOKIES_INVALID] account=%s cookies must contain dict objects", account_id)
        return None

    return copy.deepcopy(cookies)


def save_cookies(
    account_id: int,
    cookies: List[Dict[str, Any]],
    hvhcid: str = "",
) -> None:
    if not isinstance(cookies, list):
        raise ValueError("cookies must be a list")

    if not all(isinstance(cookie, dict) for cookie in cookies):
        raise ValueError("cookies must contain dict objects")

    payload = {
        "cookies": copy.deepcopy(cookies),
        "hvhcid": hvhcid,
        "saved_at": _now_iso(),
    }

    _write_json(_cookie_path(account_id), _encode_cookie_payload(payload))


def get_cookie_age_hours(account_id: int) -> Optional[float]:
    data = load_cookie_record(account_id)

    if not data:
        return None

    saved_at = data.get("saved_at")

    if not saved_at:
        return None

    try:
        ts = datetime.fromisoformat(saved_at)

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=TZ)

        return (datetime.now(TZ) - ts).total_seconds() / 3600

    except Exception as e:
        log.warning("[COOKIE_AGE_FAILED] account=%s error=%s", account_id, e)
        return None


def rotate_error_log_if_needed() -> None:
    try:
        if not os.path.exists(ERROR_LOG_FILE):
            return

        if os.path.getsize(ERROR_LOG_FILE) < MAX_ERROR_LOG_BYTES:
            return

        rotated = f"{ERROR_LOG_FILE}.old.{datetime.now(TZ).strftime('%Y%m%d_%H%M%S_%f')}"
        os.replace(ERROR_LOG_FILE, rotated)

        with open(ERROR_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")

        log.warning("[ERROR_LOG_ROTATED] rotated=%s", rotated)
        _cleanup_backups(ERROR_LOG_FILE, "old")

    except Exception as e:
        log.warning("[ERROR_LOG_ROTATE_FAILED] %s", e)


def log_error(error_type: str, detail: str) -> None:
    try:
        os.makedirs(os.path.dirname(ERROR_LOG_FILE), exist_ok=True)

        with _locked(ERROR_LOG_FILE):
            rotate_error_log_if_needed()

            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{_now_iso()}] {error_type}: {detail}\n")
                f.flush()
                os.fsync(f.fileno())

    except Exception as e:
        log.warning("[ERROR_LOG_FAILED] %s", e)


def _can_write_dir(path: str) -> bool:
    test_path = os.path.join(path, ".storage_healthcheck")

    try:
        os.makedirs(path, exist_ok=True)

        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
            f.flush()
            os.fsync(f.fileno())

        os.remove(test_path)
        return True

    except Exception as e:
        log.warning("[STORAGE_HEALTH_WRITE_FAILED] path=%s error=%s", path, e)

        try:
            if os.path.exists(test_path):
                os.remove(test_path)
        except Exception:
            pass

        return False


def storage_health() -> Dict[str, Any]:
    return {
        "data_dir": {
            "path": DATA_DIR,
            "exists": os.path.isdir(DATA_DIR),
            "writable": _can_write_dir(DATA_DIR),
        },
        "cookies_dir": {
            "path": COOKIES_DIR,
            "exists": os.path.isdir(COOKIES_DIR),
            "writable": _can_write_dir(COOKIES_DIR),
        },
        "files": {
            "subscribers": SUBSCRIBERS_FILE,
            "known_jobs": KNOWN_JOBS_FILE,
            "job_history": JOB_HISTORY_FILE,
            "applications": APPLICATIONS_FILE,
            "errors": ERROR_LOG_FILE,
        },
        "cookie_encryption": {
            "enabled": bool(os.environ.get("COOKIE_STORAGE_KEY", "").strip()),
        },
    }
