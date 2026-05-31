"""
application_preparer.py — SAFE PREPARE-ONLY mode for OWNER v1
Flow: fetch CSRF → get candidateId → create/find app → get schedules
      → advance workflow → STOP → send Telegram button → owner submits manually
Full submit only enabled if ENABLE_FULL_SUBMIT=true env var is set.
"""
import json
import os
import logging
import aiohttp
from html import escape
from config import CHAT_ID, now_london
from storage import load_cookies, save_cookies, save_application, log_error
from job_parser import shift_priority, is_fresh_job

log = logging.getLogger(__name__)

BASE_URL        = "https://www.jobsatamazon.co.uk/application/api/candidate-application"
ENABLE_FULL_SUBMIT = os.environ.get("ENABLE_FULL_SUBMIT","false").lower() == "true"

# ─── AUTH ─────────────────────────────────────────────────────────────────────
async def extract_auth(cookies: list) -> dict:
    auth = {}
    for c in cookies:
        name = c.get("name","")
        if name == "HVH_ACCESS_TOKEN": auth["token"]      = c.get("value","")
        elif name == "hvhcid":         auth["hvhcid"]     = c.get("value","")
        elif name == "aws-waf-token":  auth["waf_token"]  = c.get("value","")
    return auth

def _headers(cookies: list, auth: dict, job_id: str = "") -> dict:
    cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
    return {
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "cookie":        cookie_str,
        "authorization": auth.get("token",""),
        "bb-ui-version": "bb-ui-v2",
        "user-agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "origin":        "https://www.jobsatamazon.co.uk",
        "referer":       f"https://www.jobsatamazon.co.uk/application/uk/?jobId={job_id}",
    }

# ─── COOKIE VALIDATION ────────────────────────────────────────────────────────
async def validate_cookies(cookies: list) -> tuple[bool, str]:
    """Check cookies are valid and not expired. Returns (ok, reason)."""
    if not cookies:
        return False, "No cookies found"
    auth = await extract_auth(cookies)
    if not auth.get("token"):
        return False, "No HVH_ACCESS_TOKEN in cookies"
    if not auth.get("hvhcid"):
        return False, "No hvhcid in cookies"

    # Quick API check
    headers = _headers(cookies, auth)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://www.jobsatamazon.co.uk/authorize/api/csrf?countryCode=UK",
                headers=headers, timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                if r.status == 401:
                    return False, "Cookies expired (401)"
                if r.status == 403:
                    return False, "Cookies blocked (403) — WAF or IP issue"
                if r.status != 200:
                    return False, f"Unexpected status {r.status}"
    except Exception as e:
        return False, f"Validation request failed: {e}"

    return True, "OK"

# ─── LOAD ACCOUNT COOKIES ────────────────────────────────────────────────────
def load_account_cookies(account: dict) -> list:
    """
    Load cookies in priority order:
    1. account["cookies"] env var (per-account)
    2. SQLite saved cookies for this account_id
    3. Global AMAZON_COOKIES env (fallback for account 1 only)
    """
    acc_id = account.get("id", 1)

    # 1. Per-account cookies from env
    if account.get("cookies"):
        try:
            cookies = json.loads(account["cookies"])
            log.info(f"[COOKIES] Loaded {len(cookies)} from account {acc_id} env")
            return cookies
        except Exception as e:
            log.warning(f"[COOKIES] Parse error for account {acc_id}: {e}")

    # 2. SQLite
    cookies = load_cookies(acc_id)
    if cookies:
        log.info(f"[COOKIES] Loaded {len(cookies)} from DB for account {acc_id}")
        return cookies

    # 3. Global fallback — only for account 1
    if acc_id == 1:
        global_cookies = os.environ.get("AMAZON_COOKIES","")
        if global_cookies:
            try:
                cookies = json.loads(global_cookies)
                log.info(f"[COOKIES] Loaded {len(cookies)} from global env (account 1 fallback)")
                return cookies
            except Exception as e:
                log.warning(f"[COOKIES] Global parse error: {e}")

    log.warning(f"[COOKIES] No cookies found for account {acc_id}")
    return []

# ─── PREPARE APPLICATION (SAFE MODE) ─────────────────────────────────────────
async def prepare_application(job: dict, account: dict) -> dict:
    """
    Prepare an application up to shift selection.
    Returns result dict with status, app_id, schedule info, apply_url.
    Does NOT submit unless ENABLE_FULL_SUBMIT=true.
    """
    job_id = job.get("id","")
    acc_id = account.get("id", 1)

    log.info(f"[AUTO_PREPARE_STARTED] job={job_id} account={acc_id}")

    result = {
        "status":     "failed",
        "job_id":     job_id,
        "app_id":     None,
        "schedule_id": None,
        "apply_url":  job.get("link",""),
        "message":    "",
    }

    # Load + validate cookies
    cookies = load_account_cookies(account)
    ok, reason = await validate_cookies(cookies)
    if not ok:
        log.warning(f"[COOKIE_EXPIRED] account={acc_id} reason={reason}")
        log_error("COOKIE_EXPIRED", f"Account {acc_id}: {reason}")
        result["status"]  = "cookie_expired"
        result["message"] = reason
        return result

    auth    = await extract_auth(cookies)
    headers = _headers(cookies, auth, job_id)

    log.info(f"[COOKIES_OK] hvhcid={auth.get('hvhcid','?')[:8]}...")

    async with aiohttp.ClientSession() as session:

        # Step 1: CSRF token
        try:
            async with session.get(
                "https://www.jobsatamazon.co.uk/authorize/api/csrf?countryCode=UK",
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                csrf_data  = await r.json() if r.status == 200 else {}
                csrf_token = csrf_data.get("token","")
                if csrf_token:
                    headers["x-csrf-token"] = csrf_token
                    log.info("[CSRF_OK]")
        except Exception as e:
            log.warning(f"[CSRF_FAILED] {e}")

        # Step 2: Resolve candidateSFId
        candidate_id = auth.get("hvhcid","")
        try:
            async with session.post(
                "https://www.jobsatamazon.co.uk/graphql",
                json={
                    "operationName": "queryCandidate",
                    "query": """query queryCandidate($bbCandidateId: String!) {
                        queryCandidate(bbCandidateId: $bbCandidateId) {
                            candidateId candidateSFId firstName lastName __typename
                        }
                    }""",
                    "variables": {"bbCandidateId": candidate_id}
                },
                headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    data      = await r.json()
                    candidate = data.get("data",{}).get("queryCandidate",{}) or {}
                    sf_id     = candidate.get("candidateSFId","")
                    if sf_id:
                        candidate_id = sf_id
                    log.info(f"[CANDIDATE_ID] {candidate_id[:12]}...")
        except Exception as e:
            log.warning(f"[CANDIDATE_FAILED] {e}")

        # Step 3: Check existing application
        app_id = None
        try:
            async with session.get(
                f"{BASE_URL}/applications?jobId={job_id}&locale=en-GB",
                headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    apps = data if isinstance(data, list) else data.get("applications",[])
                    for app in apps:
                        if app.get("jobId") == job_id or app.get("active"):
                            app_id = app.get("applicationId") or app.get("id")
                            log.info(f"[APP_EXISTING] {app_id}")
                            break
        except Exception as e:
            log.warning(f"[APP_CHECK_FAILED] {e}")

        # Step 4: Create new application if needed
        if not app_id:
            try:
                async with session.post(
                    f"{BASE_URL}/application",
                    json={"jobId": job_id, "locale": "en-GB"},
                    headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    text = await r.text()
                    if r.status in [200, 201]:
                        data   = json.loads(text)
                        app_id = data.get("applicationId") or data.get("id")
                        log.info(f"[APP_CREATED] {app_id}")
                    else:
                        log.warning(f"[APP_CREATE_FAILED] {r.status}: {text[:200]}")
                        result["message"] = f"Create app failed {r.status}"
                        return result
            except Exception as e:
                log.error(f"[APP_CREATE_ERROR] {e}")
                result["message"] = str(e)
                return result

        if not app_id:
            result["message"] = "No application ID obtained"
            return result

        result["app_id"] = app_id
        headers["referer"] = (
            f"https://www.jobsatamazon.co.uk/application/uk/"
            f"?applicationId={app_id}&jobId={job_id}"
        )

        # Step 5: Get schedules
        schedule_id   = None
        schedule_text = None
        try:
            async with session.post(
                f"{BASE_URL}/job/get-all-schedules/{job_id}",
                json={"applicationId": app_id, "locale": "en-GB"},
                headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    data      = await r.json()
                    schedules = data.get("availableSchedules",{}).get("schedules",[])
                    if schedules:
                        best = sorted(
                            schedules,
                            key=lambda s: shift_priority(
                                s.get("scheduleText","") or s.get("externalJobTitle","")
                            )
                        )[0]
                        schedule_id   = best.get("scheduleId") or best.get("scheduleID") or best.get("id")
                        schedule_text = best.get("scheduleText","")
                        result["schedule_id"] = schedule_id
                        log.info(f"[SCHEDULE_SELECTED] {schedule_id} — {schedule_text}")
                    else:
                        log.warning("[NO_SCHEDULES] No shifts available for this job")
                else:
                    text = await r.text()
                    log.warning(f"[SCHEDULE_FAILED] {r.status}: {text[:200]}")
        except Exception as e:
            log.warning(f"[SCHEDULE_ERROR] {e}")

        # Step 6: Advance workflow
        for step_name in ["job-opportunities", "additional-information", "review-submit"]:
            try:
                async with session.put(
                    f"{BASE_URL}/update-workflow-step-name",
                    json={"applicationId": app_id, "workflowStepName": step_name},
                    headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    log.info(f"[WORKFLOW] → {step_name} ({r.status})")
            except Exception as e:
                log.warning(f"[WORKFLOW_ERROR] {step_name}: {e}")

        # Step 7: Full submit (owner only, explicit opt-in)
        if ENABLE_FULL_SUBMIT:
            log.info("[FULL_SUBMIT] ENABLE_FULL_SUBMIT=true — submitting now...")
            submit_body = {
                "applicationId": app_id,
                "jobId":         job_id,
                "candidateId":   candidate_id,
            }
            if schedule_id:
                submit_body["scheduleId"] = schedule_id
            try:
                async with session.post(
                    f"{BASE_URL}/submit-application",
                    json=submit_body,
                    headers=headers, timeout=aiohttp.ClientTimeout(total=20)
                ) as r:
                    text = await r.text()
                    log.info(f"[SUBMIT_RESPONSE] ({r.status}): {text[:200]}")
                    if r.status in [200, 201, 204]:
                        log.info(f"[SUBMIT_SUCCESS] app_id={app_id}")
                        result["status"]    = "submitted"
                        result["apply_url"] = f"https://www.jobsatamazon.co.uk/checklist/{job_id}/{app_id}"
                        save_application(job_id, app_id, "submitted")
                        return result
                    else:
                        log.warning(f"[SUBMIT_FAILED] {r.status}: {text[:300]}")
                        log_error("SUBMIT_FAILED", f"job={job_id} status={r.status}: {text[:200]}")
            except Exception as e:
                log.error(f"[SUBMIT_ERROR] {e}")
                log_error("SUBMIT_ERROR", str(e))

        # ── SAFE MODE: stop here, send button to owner ─────────────────────
        checklist_url = f"https://www.jobsatamazon.co.uk/application/uk/?applicationId={app_id}&jobId={job_id}"
        result["status"]    = "prepared"
        result["apply_url"] = checklist_url
        result["message"]   = schedule_text or "Shift selected"
        save_application(job_id, app_id, "prepared")
        log.info(f"[AUTO_PREPARE_SUCCESS] app_id={app_id} url={checklist_url}")
        return result

# ─── ORCHESTRATOR ─────────────────────────────────────────────────────────────
async def run_auto_prepare(job: dict, account: dict, alert_fn, chat_id: str = CHAT_ID):
    """Run prepare flow and fire Telegram alerts."""
    if is_fresh_job(job):
        await alert_fn(job, "fresh_alert", chat_id=chat_id)
        return

    await alert_fn(job, "applying", chat_id=chat_id, account_id=account.get("id"))

    result = await prepare_application(job, account)
    status = result["status"]

    if status == "cookie_expired":
        await alert_fn(job, "cookie_expired", chat_id=chat_id)
        return

    if status in ["prepared", "submitted"]:
        alert_status = "applied" if status == "submitted" else "prepared"
        await alert_fn(
            job, alert_status,
            chat_id=chat_id,
            account_id=account.get("id"),
            apply_url=result.get("apply_url"),
        )
        return

    # Fallback — something went wrong, send manual alert
    log.warning(f"[PREPARE_FALLBACK] {result.get('message')}")
    await alert_fn(job, "ready", chat_id=chat_id)
