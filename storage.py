import base64
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
APPLICATIONS_FILE = os.path.join(DATA_DIR, "applications.json")
COOKIES_DIR = os.path.join(DATA_DIR, "cookies")
ERROR_LOG_FILE = os.path.join(DATA_DIR, "errors.log")

MAX_JOB_HISTORY = 1000
MAX_ERROR_LOG_BYTES = 2_000_000
MAX_BACKUPS_PER_FILE = 5

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


def _storage_metric(path: str, action: str) -> None:
    try:
        if os.path.exists(path):
            log.debug(
                "[STORAGE_METRIC] action=%s file=%s size=%s",
                action,
                os.path.basename(path),
                os.path.getsize(path),
            )
    except Exception:
        pass


def _read_json_unlocked(path: str, default: T) -> T:
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        _storage_metric(path, "read")
        return data

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

        _storage_metric(path, "write")

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
    global _known_jobs_cache

    with _locked(KNOWN_JOBS_FILE):
        if _known_jobs_cache is None:
            data = _read_json_unlocked(KNOWN_JOBS_FILE, {})
            _known_jobs_cache = data if isinstance(data, dict) else {}

        return dict(_known_jobs_cache)


def save_known_jobs(known_jobs: Dict[str, Any]) -> None:
    global _known_jobs_cache

    if not isinstance(known_jobs, dict):
        log.warning("[KNOWN_JOBS_INVALID] Expected dict")
        return

    with _locked(KNOWN_JOBS_FILE):
        _known_jobs_cache = dict(known_jobs)
        _write_json_unlocked(KNOWN_JOBS_FILE, _known_jobs_cache)


def save_known_job(job: Dict[str, Any]) -> None:
    global _known_jobs_cache

    job_id = job.get("id")
    if not job_id:
        log.warning("[KNOWN_JOB_MISSING_ID] %s", job)
        return

    with _locked(KNOWN_JOBS_FILE):
        if _known_jobs_cache is None:
            data = _read_json_unlocked(KNOWN_JOBS_FILE, {})
            _known_jobs_cache = data if isinstance(data, dict) else {}

        _known_jobs_cache[str(job_id)] = job
        _write_json_unlocked(KNOWN_JOBS_FILE, _known_jobs_cache)


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


def save_application(job_id: str, app_id: str, status: str) -> None:
    def updater(apps: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(apps, dict):
            apps = {}

        apps[str(job_id)] = {
            "app_id": app_id,
            "status": status,
            "saved_at": _now_iso(),
        }

        return apps

    _update_json(APPLICATIONS_FILE, {}, updater)


def load_applications() -> Dict[str, Any]:
    data = _read_json(APPLICATIONS_FILE, {})
    return data if isinstance(data, dict) else {}


def _cookie_path(account_id: int) -> str:
    return os.path.join(COOKIES_DIR, f"account_{account_id}.json")


def _get_cookie_secret() -> Optional[bytes]:
    raw = os.environ.get("COOKIE_STORAGE_KEY", "").strip()

    if not raw:
        return None

    try:
        return base64.urlsafe_b64decode(raw)
    except Exception:
        log.warning("[COOKIE_KEY_INVALID] COOKIE_STORAGE_KEY is not valid base64")
        return None


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _encode_cookie_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    key = _get_cookie_secret()

    if not key:
        return payload

    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encrypted = base64.urlsafe_b64encode(_xor_bytes(raw, key)).decode("ascii")

    return {
        "encrypted": True,
        "payload": encrypted,
        "saved_at": payload.get("saved_at"),
        "hvhcid": payload.get("hvhcid", ""),
    }


def _decode_cookie_payload(data: Any) -> Any:
    if not isinstance(data, dict) or not data.get("encrypted"):
        return data

    key = _get_cookie_secret()

    if not key:
        log.warning("[COOKIE_DECRYPT_SKIPPED] missing COOKIE_STORAGE_KEY")
        return None

    try:
        encrypted = base64.urlsafe_b64decode(data.get("payload", ""))
        raw = _xor_bytes(encrypted, key)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        log.warning("[COOKIE_DECRYPT_FAILED] %s", e)
        return None


def load_cookie_record(account_id: int) -> Optional[Dict[str, Any]]:
    data = _read_json(_cookie_path(account_id), None)
    decoded = _decode_cookie_payload(data)
    return decoded if isinstance(decoded, dict) else None


def load_cookies(account_id: int) -> Optional[List[Dict[str, Any]]]:
    data = load_cookie_record(account_id)

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

    payload = {
        "cookies": cookies,
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
