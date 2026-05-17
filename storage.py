import json
import logging
import os
import shutil
from contextlib import contextmanager
from datetime import datetime
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, TypeVar

from config import DATA_DIR, TZ

log = logging.getLogger(__name__)

T = TypeVar("T")

SUBSCRIBERS_FILE = os.path.join(DATA_DIR, "subscribers.json")
KNOWN_JOBS_FILE = os.path.join(DATA_DIR, "known_jobs.json")
JOB_HISTORY_FILE = os.path.join(DATA_DIR, "job_history.json")
COOKIES_DIR = os.path.join(DATA_DIR, "cookies")
ERROR_LOG_FILE = os.path.join(DATA_DIR, "errors.log")

MAX_JOB_HISTORY = 1000
MAX_ERROR_LOG_BYTES = 2_000_000

_locks: Dict[str, RLock] = {}
_global_lock = RLock()

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
    lock = _file_lock(path)

    with lock:
        yield


def _backup_file(path: str, suffix: str) -> Optional[str]:
    try:
        if not os.path.exists(path):
            return None

        ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        backup_path = f"{path}.{suffix}.{ts}"

        shutil.copy2(path, backup_path)
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


def _update_json(path: str, default: T, updater: Callable[[T], T]) -> T:
    with _locked(path):
        current = _read_json_unlocked(path, default)
        updated = updater(current)
        _write_json_unlocked(path, updated)
        return updated


def load_subscribers() -> Dict[str, Any]:
    data = _read_json(SUBSCRIBERS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_subscribers(subscribers: Dict[str, Any]) -> None:
    _write_json(SUBSCRIBERS_FILE, subscribers)


def update_subscribers(updater: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
    return _update_json(SUBSCRIBERS_FILE, {}, updater)


def load_known_jobs() -> Dict[str, Any]:
    data = _read_json(KNOWN_JOBS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_known_jobs(known_jobs: Dict[str, Any]) -> None:
    _write_json(KNOWN_JOBS_FILE, known_jobs)


def save_known_job(job: Dict[str, Any]) -> None:
    job_id = job.get("id")

    if not job_id:
        log.warning("[KNOWN_JOB_MISSING_ID] %s", job)
        return

    def updater(known: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(known, dict):
            known = {}

        known[str(job_id)] = job
        return known

    _update_json(KNOWN_JOBS_FILE, {}, updater)


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
    def updater(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(history, list):
            history = []

        history.append(entry)
        return history[-MAX_JOB_HISTORY:]

    _update_json(JOB_HISTORY_FILE, [], updater)


def _cookie_path(account_id: int) -> str:
    return os.path.join(COOKIES_DIR, f"account_{account_id}.json")


def load_cookie_record(account_id: int) -> Optional[Dict[str, Any]]:
    data = _read_json(_cookie_path(account_id), None)
    return data if isinstance(data, dict) else None


def load_cookies(account_id: int) -> Optional[List[Dict[str, Any]]]:
    data = _read_json(_cookie_path(account_id), None)

    if isinstance(data, dict):
        cookies = data.get("cookies")
        return cookies if isinstance(cookies, list) else None

    if isinstance(data, list):
        return data

    return None


def save_cookies(
    account_id: int,
    cookies: List[Dict[str, Any]],
    hvhcid: str = "",
) -> None:
    if not isinstance(cookies, list):
        raise ValueError("cookies must be a list")

    _write_json(
        _cookie_path(account_id),
        {
            "cookies": cookies,
            "hvhcid": hvhcid,
            "saved_at": _now_iso(),
        },
    )


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

        backup = _backup_file(ERROR_LOG_FILE, "old")
        open(ERROR_LOG_FILE, "w", encoding="utf-8").close()

        log.warning("[ERROR_LOG_ROTATED] backup=%s", backup)

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
