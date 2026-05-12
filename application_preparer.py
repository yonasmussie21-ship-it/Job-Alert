import asyncio
import json
import os
import logging
from dataclasses import dataclass
from typing import Optional, Any, Dict

import aiohttp

from config import CHAT_ID
from storage import load_cookies, save_application, log_error
from job_parser import shift_priority, is_fresh_job

log = logging.getLogger(__name__)

ACCOUNT_ID = 1

BASE_URL = "https://www.jobsatamazon.co.uk/application/api/candidate-application"
CSRF_URL = "https://www.jobsatamazon.co.uk/authorize/api/csrf?countryCode=UK"
GRAPHQL_URL = "https://www.jobsatamazon.co.uk/graphql"

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


@dataclass
class StepResult:
    ok: bool
    message: str = ""
    data: Any = None
    status: Optional[str] = None


@dataclass
class PrepareResult:
    status: str
    job_id: str
    app_id: Optional[str]
    schedule_id: Optional[str]
    apply_url: str
    message: str


def _is_full_submit_enabled() -> bool:
    return os.environ.get("ENABLE_FULL_SUBMIT", "false").lower() == "true"


def _extract_auth(cookies: list) -> dict:
    auth = {}

    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")

        if name == "HVH_ACCESS_TOKEN":
            auth["token"] = value
        elif name == "hvhcid":
            auth["hvhcid"] = value
        elif name == "aws-waf-token":
            auth["waf_token"] = value

    return auth


def _build_headers(cookies: list, auth: dict, job_id: str = "") -> dict:
    cookie_str = "; ".join(
        f"{c.get('name')}={c.get('value')}"
        for c in cookies
        if c.get("name") and c.get("value")
    )

    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "cookie": cookie_str,
        "authorization": auth.get("token", ""),
        "bb-ui-version": "bb-ui-v2",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "origin": "https://www.jobsatamazon.co.uk",
        "referer": f"https://www.jobsatamazon.co.uk/application/uk/?jobId={job_id}",
    }


def _load_account_cookies(account: dict) -> StepResult:
    acc_id = account.get("id", ACCOUNT_ID)

    if account.get("cookies"):
        try:
            cookies = json.loads(account["cookies"])
            log.info(f"[COOKIES_LOADED] {len(cookies)} from account {acc_id} env")
            return StepResult(ok=True, data=cookies)
        except Exception as e:
            log.warning(f"[COOKIES_PARSE_FAILED] account={acc_id}: {e}")

    cookies = load_cookies(acc_id)
    if cookies:
        log.info(f"[COOKIES_LOADED] {len(cookies)} from DB account={acc_id}")
        return StepResult(ok=True, data=cookies)

    if acc_id == 1:
        global_cookies = os.environ.get("AMAZON_COOKIES", "")
        if global_cookies:
            try:
                cookies = json.loads(global_cookies)
                log.info(f"[COOKIES_LOADED] {len(cookies)} from global env fallback")
                return StepResult(ok=True, data=cookies)
            except Exception as e:
                log.warning(f"[COOKIES_GLOBAL_PARSE_FAILED] {e}")

    log.warning(f"[COOKIES_NOT_FOUND] account={acc_id}")
    log_error("COOKIE_EXPIRED", f"account={acc_id}: no cookies")

    return StepResult(
        ok=False,
        status="cookie_expired",
        message="No cookies found",
    )


def _validate_auth(auth: Dict[str, str], acc_id: int) -> StepResult:
    if not auth.get("token"):
        msg = "No HVH_ACCESS_TOKEN in cookies"
        log_error("COOKIE_EXPIRED", f"account={acc_id}: no token")
        return StepResult(ok=False, status="cookie_expired", message=msg)

    if not auth.get("hvhcid"):
        msg = "No hvhcid in cookies"
        log_error("COOKIE_EXPIRED", f"account={acc_id}: no hvhcid")
        return StepResult(ok=False, status="cookie_expired", message=msg)

    return StepResult(ok=True)


async def _request_text_with_retry(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    step: str,
    attempts: int = 3,
    timeout: int = 15,
    **kwargs,
) -> StepResult:
    last_error = ""

    for attempt in range(1, attempts + 1):
        try:
            async with session.request(
                method,
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                **kwargs,
            ) as response:
                text = await response.text()

                if response.status in RETRY_STATUSES and attempt < attempts:
                    log.warning(
                        "[RETRY_HTTP] step=%s attempt=%s status=%s body=%s",
                        step,
                        attempt,
                        response.status,
                        text[:180],
                    )
                    await asyncio.sleep(1.5 * attempt)
                    continue

                return StepResult(
                    ok=200 <= response.status < 300,
                    status=str(response.status),
                    message=text,
                    data={
                        "status": response.status,
                        "text": text,
                    },
                )

        except Exception as e:
            last_error = str(e)

            if attempt < attempts:
                log.warning(
                    "[RETRY_EXCEPTION] step=%s attempt=%s error=%s",
                    step,
                    attempt,
                    e,
                )
                await asyncio.sleep(1.5 * attempt)
                continue

    return StepResult(
        ok=False,
        status="request_failed",
        message=last_error,
    )


def _parse_json_step(text: str, step: str) -> StepResult:
    try:
        return StepResult(ok=True, data=json.loads(text))
    except Exception as e:
        log.warning("[%s_JSON_PARSE_FAILED] %s body=%s", step, e, text[:200])
        return StepResult(ok=False, message=f"{step} JSON parse failed")


async def _get_csrf_token(session, headers: dict) -> StepResult:
    res = await _request_text_with_retry(
        session,
        "GET",
        CSRF_URL,
        headers=headers,
        step="CSRF",
        timeout=10,
    )

    status_code = int(res.status) if res.status and res.status.isdigit() else None

    if status_code == 401:
        return StepResult(ok=False, status="cookie_expired", message="Cookies expired (401)")

    if status_code == 403:
        return StepResult(ok=False, status="cookie_expired", message="Cookies blocked (403) — WAF or IP issue")

    if not res.ok:
        return StepResult(ok=False, status="csrf_failed", message=f"CSRF failed: {res.message[:160]}")

    parsed = _parse_json_step(res.data["text"], "CSRF")
    if not parsed.ok:
        return StepResult(ok=False, status="csrf_failed", message=parsed.message)

    token = parsed.data.get("token", "")
    if not token:
        return StepResult(ok=False, status="csrf_failed", message="CSRF token missing")

    log.info("[CSRF_OK]")
    return StepResult(ok=True, data=token)


async def _resolve_candidate_id(session, headers: dict, hvhcid: str) -> StepResult:
    payload = {
        "operationName": "queryCandidate",
        "query": """
            query queryCandidate($bbCandidateId: String!) {
                queryCandidate(bbCandidateId: $bbCandidateId) {
                    candidateId
                    candidateSFId
                    firstName
                    lastName
                    __typename
                }
            }
        """,
        "variables": {"bbCandidateId": hvhcid},
    }

    res = await _request_text_with_retry(
        session,
        "POST",
        GRAPHQL_URL,
        json=payload,
        headers=headers,
        step="CANDIDATE",
        timeout=15,
    )

    if not res.ok:
        log.warning("[CANDIDATE_FAILED] status=%s body=%s", res.status, res.message[:160])
        return StepResult(ok=False, message="Candidate lookup failed")

    parsed = _parse_json_step(res.data["text"], "CANDIDATE")
    if not parsed.ok:
        return StepResult(ok=False, message=parsed.message)

    candidate = parsed.data.get("data", {}).get("queryCandidate", {}) or {}
    sf_id = candidate.get("candidateSFId", "")

    if sf_id:
        log.info(f"[CANDIDATE_ID_OK] {sf_id[:12]}...")
        return StepResult(
            ok=True,
            data={
                "candidate_id": sf_id,
                "confirmed": True,
            },
        )

    log.warning("[CANDIDATE_ID_MISSING] candidateSFId not found")

    return StepResult(
        ok=True,
        data={
            "candidate_id": hvhcid,
            "confirmed": False,
        },
    )


async def _find_existing_application(session, headers: dict, job_id: str) -> StepResult:
    res = await _request_text_with_retry(
        session,
        "GET",
        f"{BASE_URL}/applications?jobId={job_id}&locale=en-GB",
        headers=headers,
        step="APP_CHECK",
        timeout=10,
    )

    if not res.ok:
        log.warning("[APP_CHECK_FAILED] status=%s body=%s", res.status, res.message[:160])
        return StepResult(ok=False, message="Application check failed")

    parsed = _parse_json_step(res.data["text"], "APP_CHECK")
    if not parsed.ok:
        return StepResult(ok=False, message=parsed.message)

    apps = parsed.data if isinstance(parsed.data, list) else parsed.data.get("applications", [])

    for app in apps:
        if app.get("jobId") == job_id:
            app_id = app.get("applicationId") or app.get("id")
            if app_id:
                log.info(f"[APP_EXISTING] {app_id}")
                return StepResult(ok=True, data=app_id)

    return StepResult(ok=True, data=None)


async def _create_application(session, headers: dict, job_id: str) -> StepResult:
    res = await _request_text_with_retry(
        session,
        "POST",
        f"{BASE_URL}/application",
        json={"jobId": job_id, "locale": "en-GB"},
        headers=headers,
        step="APP_CREATE",
        timeout=15,
    )

    if not res.ok:
        log.warning("[APP_CREATE_FAILED] status=%s body=%s", res.status, res.message[:200])
        log_error("APP_CREATE_FAILED", f"{res.status}: {res.message[:200]}")
        return StepResult(ok=False, message="Application creation failed")

    parsed = _parse_json_step(res.data["text"], "APP_CREATE")
    if not parsed.ok:
        log_error("APP_CREATE_PARSE_ERROR", res.data["text"][:200])
        return StepResult(ok=False, message="Application creation parse error")

    app_id = parsed.data.get("applicationId") or parsed.data.get("id")

    if not app_id:
        log_error("APP_ID_MISSING", res.data["text"][:200])
        return StepResult(ok=False, message="Application ID missing in response")

    log.info(f"[APP_CREATED] {app_id}")
    return StepResult(ok=True, data=app_id)


async def _get_best_schedule(session, headers: dict, job_id: str, app_id: str) -> StepResult:
    res = await _request_text_with_retry(
        session,
        "POST",
        f"{BASE_URL}/job/get-all-schedules/{job_id}",
        json={"applicationId": app_id, "locale": "en-GB"},
        headers=headers,
        step="SCHEDULE",
        timeout=15,
    )

    if not res.ok:
        log.warning("[SCHEDULE_FAILED] status=%s body=%s", res.status, res.message[:200])
        log_error("SCHEDULE_FAILED", f"{res.status}: {res.message[:200]}")
        return StepResult(ok=False, status="no_schedule", message="Failed to fetch schedules")

    parsed = _parse_json_step(res.data["text"], "SCHEDULE")
    if not parsed.ok:
        log_error("SCHEDULE_PARSE_ERROR", res.data["text"][:200])
        return StepResult(ok=False, status="no_schedule", message="Failed to parse schedules")

    schedules = parsed.data.get("availableSchedules", {}).get("schedules", [])

    if not schedules:
        log.warning("[NO_SCHEDULES] No shifts available")
        return StepResult(ok=False, status="no_schedule", message="No shifts available")

    best = sorted(
        schedules,
        key=lambda s: shift_priority(
            s.get("scheduleText", "") or s.get("externalJobTitle", "")
        ),
    )[0]

    schedule_id = best.get("scheduleId") or best.get("scheduleID") or best.get("id")
    schedule_text = (
        best.get("scheduleText", "")
        or best.get("externalJobTitle", "")
        or "Shift selected"
    )

    if not schedule_id:
        log.warning("[SCHEDULE_ID_MISSING]")
        return StepResult(
            ok=False,
            status="no_schedule",
            message="Schedule ID missing",
            data={"schedule_text": schedule_text},
        )

    log.info(f"[SCHEDULE_SELECTED] {schedule_id} — {schedule_text}")

    return StepResult(
        ok=True,
        data={
            "schedule_id": schedule_id,
            "schedule_text": schedule_text,
        },
    )


async def _advance_workflow(session, headers: dict, app_id: str) -> StepResult:
    for step_name in ["job-opportunities", "additional-information", "review-submit"]:
        res = await _request_text_with_retry(
            session,
            "PUT",
            f"{BASE_URL}/update-workflow-step-name",
            json={
                "applicationId": app_id,
                "workflowStepName": step_name,
            },
            headers=headers,
            step=f"WORKFLOW_{step_name}",
            timeout=10,
        )

        status_code = int(res.status) if res.status and res.status.isdigit() else None
        log.info(f"[WORKFLOW] → {step_name} ({res.status})")

        if status_code not in [200, 204]:
            msg = f"Workflow step {step_name} failed with {res.status}"
            log.warning(f"[WORKFLOW_FAILED] {msg}")
            return StepResult(ok=False, message=msg)

    return StepResult(ok=True)


def _can_full_submit(
    *,
    candidate_confirmed: bool,
    schedule_id: Optional[str],
    app_id: Optional[str],
    job_id: Optional[str],
) -> StepResult:
    if not job_id:
        return StepResult(ok=False, message="Full submit blocked: missing job ID")

    if not app_id:
        return StepResult(ok=False, message="Full submit blocked: missing application ID")

    if not schedule_id:
        return StepResult(ok=False, message="Full submit blocked: missing schedule ID")

    if not candidate_confirmed:
        return StepResult(ok=False, message="Full submit blocked: candidateSFId not confirmed")

    return StepResult(ok=True)


async def _submit_application(
    session,
    headers: dict,
    job_id: str,
    app_id: str,
    candidate_id: str,
    schedule_id: str,
) -> StepResult:
    res = await _request_text_with_retry(
        session,
        "POST",
        f"{BASE_URL}/submit-application",
        json={
            "applicationId": app_id,
            "jobId": job_id,
            "candidateId": candidate_id,
            "scheduleId": schedule_id,
        },
        headers=headers,
        step="SUBMIT",
        timeout=20,
    )

    log.info(f"[SUBMIT_RESPONSE] {res.status}: {res.message[:200]}")

    status_code = int(res.status) if res.status and res.status.isdigit() else None

    if status_code in [200, 201, 204]:
        log.info(f"[SUBMIT_SUCCESS] app_id={app_id}")
        return StepResult(ok=True)

    log.warning(f"[SUBMIT_FAILED] {res.status}: {res.message[:300]}")
    log_error("SUBMIT_FAILED", f"job={job_id} {res.status}: {res.message[:200]}")

    return StepResult(ok=False, message="Submit failed")


async def prepare_application(job: dict, account: dict) -> dict:
    job_id = job.get("id", "")
    acc_id = account.get("id", ACCOUNT_ID)

    result = PrepareResult(
        status="failed",
        job_id=job_id,
        app_id=None,
        schedule_id=None,
        apply_url=job.get("link", ""),
        message="",
    )

    if not job_id:
        result.message = "Missing job ID"
        log_error("PREPARE_MISSING_JOB_ID", str(job))
        return result.__dict__

    log.info(f"[AUTO_PREPARE_STARTED] job={job_id} account={acc_id}")

    cookies_res = _load_account_cookies(account)
    if not cookies_res.ok:
        result.status = cookies_res.status or "cookie_expired"
        result.message = cookies_res.message
        return result.__dict__

    cookies = cookies_res.data
    auth = _extract_auth(cookies)

    auth_res = _validate_auth(auth, acc_id)
    if not auth_res.ok:
        result.status = auth_res.status or "cookie_expired"
        result.message = auth_res.message
        return result.__dict__

    headers = _build_headers(cookies, auth, job_id)

    log.info(f"[COOKIES_OK] hvhcid={auth.get('hvhcid', '?')[:8]}...")

    async with aiohttp.ClientSession() as session:
        csrf_res = await _get_csrf_token(session, headers)

        if not csrf_res.ok:
            result.status = csrf_res.status or "cookie_expired"
            result.message = csrf_res.message
            log_error("CSRF_FAILED", f"account={acc_id}: {csrf_res.message}")
            return result.__dict__

        headers_with_csrf = dict(headers)
        headers_with_csrf["x-csrf-token"] = csrf_res.data

        candidate_res = await _resolve_candidate_id(
            session=session,
            headers=headers_with_csrf,
            hvhcid=auth.get("hvhcid", ""),
        )

        if not candidate_res.ok:
            log.warning(f"[CANDIDATE_LOOKUP_ISSUE] {candidate_res.message}")
            candidate_id = auth.get("hvhcid", "")
            candidate_confirmed = False
        else:
            candidate_id = candidate_res.data["candidate_id"]
            candidate_confirmed = candidate_res.data["confirmed"]

        app_res = await _find_existing_application(session, headers_with_csrf, job_id)

        if not app_res.ok:
            log.warning(f"[APP_CHECK_PROBLEM] {app_res.message}")

        app_id = app_res.data if app_res.ok else None

        if not app_id:
            create_res = await _create_application(session, headers_with_csrf, job_id)

            if not create_res.ok:
                result.message = create_res.message or "No application ID obtained"
                log_error("APP_ID_MISSING", job_id)
                return result.__dict__

            app_id = create_res.data

        result.app_id = app_id

        headers_with_app = dict(headers_with_csrf)
        headers_with_app["referer"] = (
            f"https://www.jobsatamazon.co.uk/application/uk/"
            f"?applicationId={app_id}&jobId={job_id}"
        )

        schedule_res = await _get_best_schedule(
            session=session,
            headers=headers_with_app,
            job_id=job_id,
            app_id=app_id,
        )

        if not schedule_res.ok:
            result.status = schedule_res.status or "no_schedule"
            result.schedule_id = None
            result.message = schedule_res.message or "No schedule selected"
            log_error("NO_SCHEDULE", f"job={job_id} app={app_id}")
            return result.__dict__

        schedule_id = schedule_res.data["schedule_id"]
        schedule_text = schedule_res.data["schedule_text"]

        result.schedule_id = schedule_id
        result.message = schedule_text or "Shift selected"

        wf_res = await _advance_workflow(session, headers_with_app, app_id)

        if not wf_res.ok:
            result.status = "failed"
            result.message = f"Workflow failed: {wf_res.message}"
            log_error("WORKFLOW_FAILED", f"job={job_id} app={app_id}: {wf_res.message}")
            return result.__dict__

        if _is_full_submit_enabled():
            log.info("[FULL_SUBMIT_REQUESTED] ENABLE_FULL_SUBMIT=true")

            guard = _can_full_submit(
                candidate_confirmed=candidate_confirmed,
                schedule_id=schedule_id,
                app_id=app_id,
                job_id=job_id,
            )

            if not guard.ok:
                result.message = guard.message
                log_error("SUBMIT_BLOCKED", guard.message)
                return result.__dict__

            submit_res = await _submit_application(
                session=session,
                headers=headers_with_app,
                job_id=job_id,
                app_id=app_id,
                candidate_id=candidate_id,
                schedule_id=schedule_id,
            )

            if submit_res.ok:
                result.status = "submitted"
                result.apply_url = (
                    f"https://www.jobsatamazon.co.uk/checklist/{job_id}/{app_id}"
                )
                save_application(job_id, app_id, "submitted")
                return result.__dict__

            result.message = submit_res.message or "Full submit failed"
            return result.__dict__

        apply_url = (
            f"https://www.jobsatamazon.co.uk/application/uk/"
            f"?applicationId={app_id}&jobId={job_id}"
        )

        result.status = "prepared"
        result.apply_url = apply_url
        result.message = schedule_text or "Shift selected"

        save_application(job_id, app_id, "prepared")

        log.info(f"[AUTO_PREPARE_SUCCESS] app_id={app_id}")
        return result.__dict__


async def run_auto_prepare(
    job: dict,
    account: dict,
    alert_fn,
    chat_id: str = CHAT_ID,
) -> None:
    if is_fresh_job(job):
        await alert_fn(job, "fresh_alert", chat_id=chat_id)
        return

    await alert_fn(
        job,
        "applying",
        chat_id=chat_id,
        account_id=account.get("id"),
    )

    result = await prepare_application(job, account)
    status = result.get("status")

    if status == "cookie_expired":
        await alert_fn(job, "cookie_expired", chat_id=chat_id)
        return

    if status == "no_schedule":
        log.warning(f"[NO_SCHEDULE_ALERT] {result.get('message')}")
        await alert_fn(job, "ready", chat_id=chat_id)
        return

    if status == "submitted":
        await alert_fn(
            job,
            "applied",
            chat_id=chat_id,
            account_id=account.get("id"),
            apply_url=result.get("apply_url"),
        )
        return

    if status == "prepared":
        await alert_fn(
            job,
            "prepared",
            chat_id=chat_id,
            account_id=account.get("id"),
            apply_url=result.get("apply_url"),
        )
        return

    log.warning(f"[PREPARE_FALLBACK] {result.get('message')}")
    await alert_fn(job, "ready", chat_id=chat_id)
