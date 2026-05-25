import asyncio
import json
import logging
import os
import random
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

import aiohttp

from config import CHAT_ID
from job_parser import is_fresh_job, shift_priority
from storage import load_application, load_cookies, log_error, save_application

log = logging.getLogger(__name__)

ACCOUNT_ID = 1

BASE_URL = "https://www.jobsatamazon.co.uk/application/api/candidate-application"
CSRF_URL = "https://www.jobsatamazon.co.uk/authorize/api/csrf?countryCode=UK"
GRAPHQL_URL = "https://www.jobsatamazon.co.uk/graphql"

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)

AUTO_SUBMIT_COOLDOWN_SECONDS = int(os.getenv("AUTO_SUBMIT_COOLDOWN_SECONDS", "45"))
MAX_ACCOUNT_FAILURES = int(os.getenv("MAX_ACCOUNT_FAILURES", "5"))

BLOCKED_EXISTING_STATUSES = {
    "prepared",
    "submitted",
    "applying",
    "cooldown",
}

_ACCOUNT_FAILURES: dict[int, int] = {}
_LAST_SUBMIT_AT: dict[int, float] = {}


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    account_id: int
    job_id: str


@dataclass
class StepResult:
    ok: bool
    status: str = "ok"
    message: str = ""
    data: Any = None


@dataclass
class PrepareResult:
    status: str
    job_id: str
    app_id: Optional[str]
    schedule_id: Optional[str]
    apply_url: str
    message: str


@dataclass(frozen=True)
class AuthContext:
    cookies: list[dict[str, Any]]
    token: str
    hvhcid: str
    waf_token: str = ""


def full_submit_enabled() -> bool:
    return (
        os.getenv("ENABLE_FULL_SUBMIT", "false").lower() == "true"
        and os.getenv("CONFIRM_FULL_SUBMIT", "") == "I_UNDERSTAND"
    )


def _now() -> float:
    return time.time()


def _record_failure(account_id: int) -> None:
    _ACCOUNT_FAILURES[account_id] = _ACCOUNT_FAILURES.get(account_id, 0) + 1


def _reset_failures(account_id: int) -> None:
    _ACCOUNT_FAILURES[account_id] = 0


def _circuit_open(account_id: int) -> bool:
    return _ACCOUNT_FAILURES.get(account_id, 0) >= MAX_ACCOUNT_FAILURES


def _cooldown_active(account_id: int) -> bool:
    last = _LAST_SUBMIT_AT.get(account_id)
    return bool(last and (_now() - last) < AUTO_SUBMIT_COOLDOWN_SECONDS)


def _mark_submit(account_id: int) -> None:
    _LAST_SUBMIT_AT[account_id] = _now()


def safe_json_loads(raw: str, label: str) -> Optional[Any]:
    try:
        return json.loads(raw)
    except Exception as exc:
        log.warning("[%s_JSON_FAILED] %s", label, exc)
        return None


def build_cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{cookie.get('name')}={cookie.get('value')}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value")
    )


def load_account_cookies(account: dict[str, Any]) -> StepResult:
    acc_id = int(account.get("id", ACCOUNT_ID))

    if account.get("cookies"):
        cookies = safe_json_loads(account["cookies"], f"ACCOUNT_{acc_id}_COOKIES")
        if isinstance(cookies, list):
            return StepResult(ok=True, data=cookies)

    cookies = load_cookies(acc_id)
    if cookies:
        return StepResult(ok=True, data=cookies)

    if acc_id == 1:
        raw = os.getenv("AMAZON_COOKIES", "")
        if raw:
            cookies = safe_json_loads(raw, "GLOBAL_COOKIES")
            if isinstance(cookies, list):
                return StepResult(ok=True, data=cookies)

    log_error("COOKIE_EXPIRED", f"account={acc_id}: no cookies")
    return StepResult(ok=False, status="cookie_expired", message="No cookies found")


def extract_auth(cookies: list[dict[str, Any]], acc_id: int) -> StepResult:
    if not all(isinstance(cookie, dict) for cookie in cookies):
        return StepResult(
            ok=False,
            status="cookie_expired",
            message="Invalid cookie format",
        )

    names = {cookie.get("name") for cookie in cookies}
    missing = {"HVH_ACCESS_TOKEN", "hvhcid"} - names

    if missing:
        log_error("COOKIE_EXPIRED", f"account={acc_id}: missing {sorted(missing)}")
        return StepResult(
            ok=False,
            status="cookie_expired",
            message=f"Missing cookies: {sorted(missing)}",
        )

    token = ""
    hvhcid = ""
    waf_token = ""

    for cookie in cookies:
        name = cookie.get("name", "")
        value = cookie.get("value", "")

        if name == "HVH_ACCESS_TOKEN":
            token = value
        elif name == "hvhcid":
            hvhcid = value
        elif name == "aws-waf-token":
            waf_token = value

    if not token or not hvhcid:
        return StepResult(
            ok=False,
            status="cookie_expired",
            message="Required auth cookie value empty",
        )

    return StepResult(
        ok=True,
        data=AuthContext(
            cookies=cookies,
            token=token,
            hvhcid=hvhcid,
            waf_token=waf_token,
        ),
    )


def detect_block(text: str) -> Optional[str]:
    body = (text or "").lower()

    if "<html" in body and "captcha" in body:
        return "captcha"

    if "captcha" in body:
        return "captcha"

    if "aws-waf" in body or "waf" in body:
        return "waf_block"

    if "access denied" in body or "forbidden" in body:
        return "access_denied"

    return None


class AmazonApplicationClient:
    def __init__(self, session: Optional[aiohttp.ClientSession] = None) -> None:
        self._external_session = session
        self._session = session

    async def __aenter__(self) -> "AmazonApplicationClient":
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)

        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._external_session is None and self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Client session not initialized")

        return self._session

    def headers(
        self,
        auth: AuthContext,
        ctx: RequestContext,
        app_id: str = "",
    ) -> dict[str, str]:
        referer = f"https://www.jobsatamazon.co.uk/application/uk/?jobId={ctx.job_id}"

        if app_id:
            referer = (
                "https://www.jobsatamazon.co.uk/application/uk/"
                f"?applicationId={app_id}&jobId={ctx.job_id}"
            )

        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "cookie": build_cookie_header(auth.cookies),
            "authorization": auth.token,
            "bb-ui-version": "bb-ui-v2",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "origin": "https://www.jobsatamazon.co.uk",
            "referer": referer,
            "x-client-request-id": ctx.request_id if not app_id else f"{ctx.request_id}:{app_id}",
        }

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        step: str,
        ctx: RequestContext,
        attempts: int = 3,
        **kwargs: Any,
    ) -> StepResult:
        last_error = ""

        for attempt in range(1, attempts + 1):
            try:
                async with self.session.request(method, url, **kwargs) as response:
                    text = await response.text()

                    blocked = detect_block(text)
                    if blocked:
                        log_error(
                            blocked.upper(),
                            f"step={step} status={response.status} body={text[:300]}",
                        )
                        return StepResult(
                            ok=False,
                            status=blocked,
                            message=f"{blocked} detected",
                        )

                    if response.status in RETRY_STATUSES and attempt < attempts:
                        delay = (1.5 * attempt) + random.uniform(0, 0.75)
                        log.warning(
                            "[RETRY_HTTP] request=%s step=%s attempt=%s status=%s body=%s",
                            ctx.request_id,
                            step,
                            attempt,
                            response.status,
                            text[:180],
                        )
                        await asyncio.sleep(delay)
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

            except asyncio.CancelledError:
                raise

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = str(exc)

                if attempt < attempts:
                    delay = (1.5 * attempt) + random.uniform(0, 0.75)
                    log.warning(
                        "[RETRY_EXCEPTION] request=%s step=%s attempt=%s error=%s",
                        ctx.request_id,
                        step,
                        attempt,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue

            except Exception as exc:
                last_error = str(exc)
                log.exception(
                    "[REQUEST_FATAL] request=%s step=%s error=%s",
                    ctx.request_id,
                    step,
                    exc,
                )
                break

        return StepResult(
            ok=False,
            status="request_failed",
            message=last_error,
        )

    @staticmethod
    def parse_json(text: str, step: str, ctx: RequestContext) -> StepResult:
        try:
            return StepResult(ok=True, data=json.loads(text))
        except Exception as exc:
            log.warning(
                "[%s_JSON_PARSE_FAILED] request=%s error=%s body=%s",
                step,
                ctx.request_id,
                exc,
                text[:200],
            )
            return StepResult(
                ok=False,
                status="parse_failed",
                message=f"{step} JSON parse failed",
            )

    async def get_csrf_token(
        self,
        headers: dict[str, str],
        ctx: RequestContext,
    ) -> StepResult:
        res = await self.request_text(
            "GET",
            CSRF_URL,
            headers=headers,
            step="CSRF",
            ctx=ctx,
        )

        if res.status in {
            "401",
            "403",
            "cookie_expired",
            "captcha",
            "waf_block",
            "access_denied",
        }:
            return StepResult(
                ok=False,
                status="cookie_expired",
                message=res.message or "Cookies blocked/expired",
            )

        if not res.ok:
            return StepResult(
                ok=False,
                status="csrf_failed",
                message=f"CSRF failed: {res.message[:160]}",
            )

        parsed = self.parse_json(res.data["text"], "CSRF", ctx)

        if not parsed.ok:
            return StepResult(
                ok=False,
                status="csrf_failed",
                message=parsed.message,
            )

        token = parsed.data.get("token", "")

        if not token:
            return StepResult(
                ok=False,
                status="csrf_failed",
                message="CSRF token missing",
            )

        return StepResult(ok=True, data=token)

    async def resolve_candidate_id(
        self,
        headers: dict[str, str],
        hvhcid: str,
        ctx: RequestContext,
    ) -> StepResult:
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
            "variables": {
                "bbCandidateId": hvhcid,
            },
        }

        res = await self.request_text(
            "POST",
            GRAPHQL_URL,
            json=payload,
            headers=headers,
            step="CANDIDATE",
            ctx=ctx,
        )

        if not res.ok:
            return StepResult(
                ok=False,
                status="candidate_failed",
                message="Candidate lookup failed",
            )

        parsed = self.parse_json(res.data["text"], "CANDIDATE", ctx)

        if not parsed.ok:
            return parsed

        candidate = parsed.data.get("data", {}).get("queryCandidate", {}) or {}
        sf_id = candidate.get("candidateSFId", "")

        if not sf_id:
            return StepResult(
                ok=False,
                status="candidate_failed",
                message="candidateSFId not confirmed",
            )

        return StepResult(ok=True, data=sf_id)

    async def find_existing_application(
        self,
        headers: dict[str, str],
        ctx: RequestContext,
    ) -> StepResult:
        res = await self.request_text(
            "GET",
            f"{BASE_URL}/applications?jobId={ctx.job_id}&locale=en-GB",
            headers=headers,
            step="APP_CHECK",
            ctx=ctx,
        )

        if not res.ok:
            return StepResult(
                ok=False,
                status="app_check_failed",
                message="Application check failed",
            )

        parsed = self.parse_json(res.data["text"], "APP_CHECK", ctx)

        if not parsed.ok:
            return parsed

        apps = parsed.data if isinstance(parsed.data, list) else parsed.data.get("applications", [])

        if not isinstance(apps, list):
            return StepResult(ok=True, data=None)

        for app in apps:
            if app.get("jobId") == ctx.job_id:
                app_id = app.get("applicationId") or app.get("id")
                if app_id:
                    return StepResult(ok=True, data=app_id)

        return StepResult(ok=True, data=None)

    async def create_application(
        self,
        headers: dict[str, str],
        ctx: RequestContext,
    ) -> StepResult:
        res = await self.request_text(
            "POST",
            f"{BASE_URL}/application",
            json={
                "jobId": ctx.job_id,
                "locale": "en-GB",
            },
            headers=headers,
            step="APP_CREATE",
            ctx=ctx,
        )

        if not res.ok:
            log_error("APP_CREATE_FAILED", f"{res.status}: {res.message[:200]}")
            return StepResult(
                ok=False,
                status="app_create_failed",
                message="Application creation failed",
            )

        parsed = self.parse_json(res.data["text"], "APP_CREATE", ctx)

        if not parsed.ok:
            log_error("APP_CREATE_PARSE_ERROR", res.data["text"][:200])
            return StepResult(
                ok=False,
                status="app_create_failed",
                message="Application creation parse error",
            )

        app_id = parsed.data.get("applicationId") or parsed.data.get("id")

        if not app_id:
            log_error("APP_ID_MISSING", res.data["text"][:200])
            return StepResult(
                ok=False,
                status="app_create_failed",
                message="Application ID missing",
            )

        return StepResult(ok=True, data=app_id)

    async def get_best_schedule(
        self,
        headers: dict[str, str],
        app_id: str,
        ctx: RequestContext,
    ) -> StepResult:
        res = await self.request_text(
            "POST",
            f"{BASE_URL}/job/get-all-schedules/{ctx.job_id}",
            json={
                "applicationId": app_id,
                "locale": "en-GB",
            },
            headers=headers,
            step="SCHEDULE",
            ctx=ctx,
        )

        if not res.ok:
            log_error("SCHEDULE_FAILED", f"{res.status}: {res.message[:200]}")
            return StepResult(
                ok=False,
                status="no_schedule",
                message="Failed to fetch schedules",
            )

        parsed = self.parse_json(res.data["text"], "SCHEDULE", ctx)

        if not parsed.ok:
            log_error("SCHEDULE_PARSE_ERROR", res.data["text"][:200])
            return StepResult(
                ok=False,
                status="no_schedule",
                message="Failed to parse schedules",
            )

        schedules = parsed.data.get("availableSchedules", {}).get("schedules", [])

        if not schedules:
            return StepResult(
                ok=False,
                status="no_schedule",
                message="No shifts available",
            )

        best = sorted(
            schedules,
            key=lambda item: shift_priority(
                item.get("scheduleText", "") or item.get("externalJobTitle", "")
            ),
        )[0]

        schedule_id = best.get("scheduleId") or best.get("scheduleID") or best.get("id")
        schedule_text = best.get("scheduleText") or best.get("externalJobTitle") or "Shift selected"

        if not schedule_id:
            return StepResult(
                ok=False,
                status="no_schedule",
                message="Schedule ID missing",
            )

        return StepResult(
            ok=True,
            data={
                "schedule_id": schedule_id,
                "schedule_text": schedule_text,
            },
        )

    async def advance_workflow(
        self,
        headers: dict[str, str],
        app_id: str,
        ctx: RequestContext,
    ) -> StepResult:
        for step_name in (
            "job-opportunities",
            "additional-information",
            "review-submit",
        ):
            res = await self.request_text(
                "PUT",
                f"{BASE_URL}/update-workflow-step-name",
                json={
                    "applicationId": app_id,
                    "workflowStepName": step_name,
                },
                headers=headers,
                step=f"WORKFLOW_{step_name}",
                ctx=ctx,
            )

            if res.status not in ("200", "204"):
                return StepResult(
                    ok=False,
                    status="workflow_failed",
                    message=f"Workflow step {step_name} failed with {res.status}",
                )

        return StepResult(ok=True)

    async def submit_application(
        self,
        headers: dict[str, str],
        app_id: str,
        candidate_id: str,
        schedule_id: str,
        ctx: RequestContext,
    ) -> StepResult:
        if not all([ctx.job_id, app_id, candidate_id, schedule_id]):
            return StepResult(
                ok=False,
                status="submit_blocked",
                message="Missing submit identifiers",
            )

        res = await self.request_text(
            "POST",
            f"{BASE_URL}/submit-application",
            json={
                "applicationId": app_id,
                "jobId": ctx.job_id,
                "candidateId": candidate_id,
                "scheduleId": schedule_id,
            },
            headers=headers,
            step="SUBMIT",
            ctx=ctx,
        )

        if res.status in ("200", "201", "204"):
            return StepResult(ok=True)

        log_error(
            "SUBMIT_FAILED",
            f"job={ctx.job_id} app={app_id} {res.status}: {res.message[:200]}",
        )
        return StepResult(
            ok=False,
            status="submit_failed",
            message="Submit failed",
        )


class AmazonApplicationService:
    async def prepare(
        self,
        job: dict[str, Any],
        account: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = str(job.get("id", "")).strip()
        acc_id = int(account.get("id", ACCOUNT_ID))

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
            return asdict(result)

        existing = load_application(job_id)

        if existing and existing.get("status") in BLOCKED_EXISTING_STATUSES:
            result.status = "already_handled"
            result.app_id = existing.get("app_id")
            result.message = f"Already {existing.get('status')}"
            return asdict(result)

        if _circuit_open(acc_id):
            result.status = "circuit_open"
            result.message = "Account circuit breaker open"
            return asdict(result)

        ctx = RequestContext(
            request_id=str(uuid.uuid4()),
            account_id=acc_id,
            job_id=job_id,
        )

        cookies_res = load_account_cookies(account)

        if not cookies_res.ok:
            result.status = cookies_res.status
            result.message = cookies_res.message
            _record_failure(acc_id)
            return asdict(result)

        auth_res = extract_auth(cookies_res.data, acc_id)

        if not auth_res.ok:
            result.status = auth_res.status
            result.message = auth_res.message
            _record_failure(acc_id)
            return asdict(result)

        auth: AuthContext = auth_res.data

        async with AmazonApplicationClient() as client:
            headers = client.headers(auth, ctx)

            csrf_res = await client.get_csrf_token(headers, ctx)

            if not csrf_res.ok:
                result.status = csrf_res.status
                result.message = csrf_res.message
                _record_failure(acc_id)
                return asdict(result)

            headers["x-csrf-token"] = csrf_res.data

            candidate_res = await client.resolve_candidate_id(
                headers,
                auth.hvhcid,
                ctx,
            )

            if not candidate_res.ok:
                result.status = candidate_res.status
                result.message = candidate_res.message
                _record_failure(acc_id)
                return asdict(result)

            candidate_id = candidate_res.data

            app_res = await client.find_existing_application(headers, ctx)
            app_id = app_res.data if app_res.ok else None

            if not app_id:
                create_res = await client.create_application(headers, ctx)

                if not create_res.ok:
                    result.status = create_res.status
                    result.message = create_res.message
                    _record_failure(acc_id)
                    return asdict(result)

                app_id = create_res.data

            result.app_id = app_id

            app_headers = client.headers(auth, ctx, app_id=app_id)
            app_headers["x-csrf-token"] = csrf_res.data

            schedule_res = await client.get_best_schedule(
                app_headers,
                app_id,
                ctx,
            )

            if not schedule_res.ok:
                result.status = schedule_res.status
                result.message = schedule_res.message
                save_application(job_id, app_id, "no_schedule")
                return asdict(result)

            schedule_id = schedule_res.data["schedule_id"]
            schedule_text = schedule_res.data["schedule_text"]

            result.schedule_id = schedule_id
            result.message = schedule_text

            workflow_res = await client.advance_workflow(
                app_headers,
                app_id,
                ctx,
            )

            if not workflow_res.ok:
                result.status = workflow_res.status
                result.message = workflow_res.message
                _record_failure(acc_id)
                return asdict(result)

            if not full_submit_enabled():
                result.status = "prepared"
                result.apply_url = (
                    "https://www.jobsatamazon.co.uk/application/uk/"
                    f"?applicationId={app_id}&jobId={job_id}"
                )
                save_application(job_id, app_id, "prepared")
                _reset_failures(acc_id)
                return asdict(result)

            if _cooldown_active(acc_id):
                result.status = "cooldown"
                result.message = "Auto-submit cooldown active"
                save_application(job_id, app_id, "cooldown")
                return asdict(result)

            save_application(job_id, app_id, "applying")

            submit_res = await client.submit_application(
                headers=app_headers,
                app_id=app_id,
                candidate_id=candidate_id,
                schedule_id=schedule_id,
                ctx=ctx,
            )

            if submit_res.ok:
                _mark_submit(acc_id)
                _reset_failures(acc_id)

                result.status = "submitted"
                result.apply_url = f"https://www.jobsatamazon.co.uk/checklist/{job_id}/{app_id}"

                save_application(job_id, app_id, "submitted")
                return asdict(result)

            result.status = submit_res.status
            result.message = submit_res.message

            save_application(job_id, app_id, submit_res.status or "submit_failed")
            _record_failure(acc_id)

            return asdict(result)


async def prepare_application(
    job: dict[str, Any],
    account: dict[str, Any],
) -> dict[str, Any]:
    try:
        return await AmazonApplicationService().prepare(job, account)

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        job_id = str(job.get("id", "")).strip()
        acc_id = account.get("id", ACCOUNT_ID)

        log.exception(
            "[PREPARE_FATAL] job=%s account=%s error=%s",
            job_id,
            acc_id,
            exc,
        )
        log_error("PREPARE_FATAL", f"job={job_id} account={acc_id}: {exc}")

        return asdict(
            PrepareResult(
                status="failed",
                job_id=job_id,
                app_id=None,
                schedule_id=None,
                apply_url=job.get("link", ""),
                message="Unexpected prepare error",
            )
        )


AlertFn = Callable[..., Awaitable[None]]


async def run_auto_prepare(
    job: dict[str, Any],
    account: dict[str, Any],
    alert_fn: AlertFn,
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
    elif status == "submitted":
        await alert_fn(
            job,
            "applied",
            chat_id=chat_id,
            account_id=account.get("id"),
            apply_url=result.get("apply_url"),
        )
    elif status == "prepared":
        await alert_fn(
            job,
            "prepared",
            chat_id=chat_id,
            account_id=account.get("id"),
            apply_url=result.get("apply_url"),
        )
    else:
        await alert_fn(job, "ready", chat_id=chat_id)
