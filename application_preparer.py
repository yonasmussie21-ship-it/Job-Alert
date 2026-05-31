import asyncio
import copy
import json
import logging
import random
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp
from playwright.async_api import async_playwright

BASE_URL = "https://www.jobsatamazon.co.uk"
SEARCH_URL = f"{BASE_URL}/app#/jobSearch?locale=en-GB&country=GBR"
CANDIDATE_GRAPHQL = f"{BASE_URL}/candidate/graphql"

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "amazon_bot.db"

PAGE_SIZE = 100
MAX_PAGES = 20
MAX_CONCURRENT_CITIES = 3

log = logging.getLogger("amazon_pro_core")
logging.basicConfig(level=logging.INFO)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "bb-ui-version": "bb-ui-v2",
}


@dataclass(frozen=True)
class City:
    name: str
    lat: float
    lng: float
    distance: int


@dataclass(frozen=True)
class Account:
    account_id: int
    bb_candidate_id: str
    candidate_id: str
    csrf_token: str
    cookies: dict[str, str]


@dataclass
class CapturedRequest:
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    operation_name: str
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class CircuitBreaker:
    failures: int = 0
    opened_until: float = 0.0
    last_reason: str = ""

    def is_open(self) -> bool:
        return time.time() < self.opened_until

    def record_success(self) -> None:
        self.failures = 0
        self.opened_until = 0.0
        self.last_reason = ""

    def record_failure(self, reason: str) -> None:
        self.failures += 1
        self.last_reason = reason

        if self.failures >= 3:
            cooldown = min(300, 30 * self.failures)
            self.opened_until = time.time() + cooldown


@dataclass
class Metrics:
    requests_total: int = 0
    requests_ok: int = 0
    requests_failed: int = 0
    rate_limited: int = 0
    forbidden: int = 0
    jobs_found: int = 0
    jobs_new: int = 0
    replay_pages: int = 0
    replay_failures: int = 0
    capture_candidates: int = 0
    capture_selected_score: int = 0
    submit_attempts: int = 0
    submit_success: int = 0
    submit_failed: int = 0

    by_city: dict[str, dict[str, int]] = field(default_factory=dict)
    by_account: dict[str, dict[str, int]] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)

    def inc_city(self, city: str, key: str, amount: int = 1) -> None:
        self.by_city.setdefault(city, {})
        self.by_city[city][key] = self.by_city[city].get(key, 0) + amount

    def inc_account(self, account_id: int, key: str, amount: int = 1) -> None:
        acc = str(account_id)
        self.by_account.setdefault(acc, {})
        self.by_account[acc][key] = self.by_account[acc].get(key, 0) + amount

    def inc_status(self, status: int) -> None:
        key = str(status)
        self.by_status[key] = self.by_status.get(key, 0) + 1


CITIES = [
    City("birmingham", 52.4862, -1.8904, 40),
    City("coventry", 52.4068, -1.5197, 35),
    City("london", 51.5074, -0.1278, 50),
    City("leeds", 53.8008, -1.5491, 50),
    City("swindon", 51.5558, -1.7797, 45),
    City("manchester", 53.4808, -2.2426, 45),
]


QUERY_APPLICATIONS_V2 = """
query queryApplicationsByBBCandidateIdV2($locale: String!, $bbCandidateId: String!) {
  queryApplicationsByBBCandidateIdV2(locale: $locale, bbCandidateId: $bbCandidateId) {
    didAllApplicationsLoaded
    applications {
      active
      submitted
      applicationId
      applicationState
      candidateId
      sfCandidateId
      workflowName
      step
      subStep
      continueApplicationLink
      changeShiftLink
      allowedActionsList
      scheduleInfo {
        scheduleId
        city
        postalCode
        basePay
        employmentType
        externalJobTitle
        hoursPerWeek
        startTime
        firstDayOnSite
      }
      jobDetail {
        jobId
        jobTitle
        employmentType
        city
        postalCode
        totalPayRateMin
        totalPayRateMax
      }
      jobData {
        jobId
        scheduleId
        basePay
        totalPayRate
        city
        postalCode
        employmentType
        siteId
      }
    }
  }
}
"""


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS seen_jobs (
                dedupe_key TEXT PRIMARY KEY,
                first_seen_at REAL,
                last_seen_at REAL,
                score REAL,
                city TEXT,
                raw_json TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                account_id INTEGER,
                event_type TEXT,
                entity_id TEXT,
                before_state TEXT,
                after_state TEXT,
                success INTEGER,
                payload_json TEXT
            )
        """)
        db.commit()


def clean_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {
        "content-length",
        "host",
        "connection",
        "accept-encoding",
        ":method",
        ":path",
        ":scheme",
        ":authority",
    }
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


def operation_name(body: dict[str, Any]) -> str:
    return str(body.get("operationName") or "")


def has_search_request(body: dict[str, Any]) -> bool:
    variables = body.get("variables")
    if not isinstance(variables, dict):
        return False
    return any(k in variables for k in ["searchJobRequest", "input", "request"])


def score_capture_candidate(url: str, body: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons = []

    body_text = json.dumps(body)

    op = operation_name(body)
    if op:
        score += 2
        reasons.append(f"operationName={op}")

    if any(x in op for x in ["searchJobCardsByLocation", "searchJobCards", "searchJobs"]):
        score += 8
        reasons.append("search operationName")

    if has_search_request(body):
        score += 8
        reasons.append("has searchJobRequest/input/request")

    if "geoQueryClause" in body_text:
        score += 5
        reasons.append("has geoQueryClause")

    if "jobCards" in body_text or "jobs" in body_text:
        score += 3
        reasons.append("query asks for job cards")

    if "/graphql" in url:
        score += 2
        reasons.append("graphql url")

    return score, reasons


def patch_payload(
    base: dict[str, Any],
    city: City,
    next_token: Optional[str] = None,
) -> dict[str, Any]:
    payload = copy.deepcopy(base)
    variables = payload.setdefault("variables", {})

    req = (
        variables.get("searchJobRequest")
        or variables.get("input")
        or variables.get("request")
        or {}
    )

    req["locale"] = "en-GB"
    req["country"] = "GBR"
    req["keyWords"] = ""
    req["pageSize"] = PAGE_SIZE
    req["geoQueryClause"] = {
        "lat": city.lat,
        "lng": city.lng,
        "unit": "mi",
        "distance": city.distance,
    }

    req["containFilters"] = [
        {"key": "isPrivateSchedule", "val": ["true", "false"]}
    ]
    req["orFilters"] = [
        {"key": "bonusJob", "val": ["true"]},
        {"key": "featuredJob", "val": ["true"]},
    ]
    req["equalFilters"] = []
    req["rangeFilters"] = []
    req["dateFilters"] = []
    req["sorters"] = []
    req["consolidateSchedule"] = True

    for old in ["lat", "lng", "radius", "distance", "latitude", "longitude"]:
        req.pop(old, None)

    if next_token:
        req["nextToken"] = next_token
    else:
        req.pop("nextToken", None)
        req.pop("nextPageToken", None)

    variables["searchJobRequest"] = req
    return payload


def extract_search_root(data: dict[str, Any]) -> dict[str, Any]:
    root = data.get("data", {})
    if not isinstance(root, dict):
        return {}

    for key in [
        "searchJobCardsByLocation",
        "searchJobCards",
        "searchJobsV2",
        "searchJobs",
        "jobSearch",
    ]:
        if isinstance(root.get(key), dict):
            return root[key]

    for value in root.values():
        if isinstance(value, dict) and any(k in value for k in ["jobCards", "jobs", "results"]):
            return value

    return {}


def extract_cards(data: dict[str, Any]) -> list[dict[str, Any]]:
    root = extract_search_root(data)

    for key in ["jobCards", "jobs", "results", "items"]:
        value = root.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    return []


def extract_next_token(data: dict[str, Any]) -> Optional[str]:
    root = extract_search_root(data)

    for key in ["nextToken", "nextPageToken", "cursor", "paginationToken"]:
        token = root.get(key)
        if token:
            return str(token)

    return None


def job_key(job: dict[str, Any]) -> str:
    return f"{job.get('jobId') or job.get('id')}:{job.get('scheduleId') or job.get('shiftCode')}"


def score_job(job: dict[str, Any]) -> float:
    score = 0.0
    text = json.dumps(job).lower()

    if "full" in text:
        score += 3
    if "permanent" in text:
        score += 3
    if "seasonal" in text or "temporary" in text or "part-time" in text:
        score -= 3

    hours = job.get("hoursPerWeek") or job.get("hours") or 0
    try:
        hours = float(hours)
        if 36 <= hours <= 40:
            score += 4
        elif hours >= 30:
            score += 2
    except Exception:
        pass

    pay = job.get("totalPayRateMin") or job.get("basePay") or 0
    try:
        pay = float(pay)
        if pay >= 14:
            score += 3
        elif pay >= 12:
            score += 1
    except Exception:
        pass

    return round(score, 2)


def mark_seen(job: dict[str, Any]) -> bool:
    key = job_key(job)
    now = time.time()
    score = float(job.get("score") or 0)
    city = str(job.get("city") or "")

    with sqlite3.connect(DB_PATH) as db:
        row = db.execute("SELECT dedupe_key FROM seen_jobs WHERE dedupe_key = ?", (key,)).fetchone()
        if row:
            db.execute(
                "UPDATE seen_jobs SET last_seen_at = ?, score = ?, raw_json = ? WHERE dedupe_key = ?",
                (now, score, json.dumps(job), key),
            )
            db.commit()
            return False

        db.execute(
            """
            INSERT INTO seen_jobs (
                dedupe_key, first_seen_at, last_seen_at, score, city, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, now, now, score, city, json.dumps(job)),
        )
        db.commit()
        return True


def audit_event(
    account_id: int,
    event_type: str,
    entity_id: str,
    before_state: str,
    after_state: str,
    success: bool,
    payload: dict[str, Any],
) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO audit_events (
                ts, account_id, event_type, entity_id,
                before_state, after_state, success, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                account_id,
                event_type,
                entity_id,
                before_state,
                after_state,
                int(success),
                json.dumps(payload),
            ),
        )
        db.commit()


class AmazonProCore:
    def __init__(self) -> None:
        init_db()
        self.session: Optional[aiohttp.ClientSession] = None
        self.playwright = None
        self.browser = None
        self.metrics = Metrics()
        self.breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, scope: str) -> CircuitBreaker:
        if scope not in self.breakers:
            self.breakers[scope] = CircuitBreaker()
        return self.breakers[scope]

    async def get_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        return self.session

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def capture_search_request(self, cookies: Optional[dict[str, str]] = None) -> Optional[CapturedRequest]:
        self.playwright = self.playwright or await async_playwright().start()
        self.browser = self.browser or await self.playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )

        context = await self.browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1365, "height": 768},
        )

        if cookies:
            await context.add_cookies([
                {
                    "name": k,
                    "value": v,
                    "domain": ".jobsatamazon.co.uk",
                    "path": "/",
                }
                for k, v in cookies.items()
            ])

        page = await context.new_page()
        candidates: list[CapturedRequest] = []

        async def on_request(req):
            if "/graphql" not in req.url:
                return

            raw = req.post_data or ""
            if not raw:
                return

            try:
                body = json.loads(raw)
            except Exception:
                return

            score, reasons = score_capture_candidate(req.url, body)

            if score <= 0:
                return

            candidates.append(
                CapturedRequest(
                    url=req.url,
                    headers=clean_headers(dict(req.headers)),
                    body=body,
                    operation_name=operation_name(body),
                    score=score,
                    reasons=reasons,
                )
            )

        page.on("request", on_request)

        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(3500)
        await page.mouse.wheel(0, 2400)
        await page.wait_for_timeout(1500)

        await context.close()

        self.metrics.capture_candidates += len(candidates)

        if not candidates:
            return None

        candidates.sort(key=lambda c: c.score, reverse=True)
        selected = candidates[0]
        self.metrics.capture_selected_score = selected.score

        log.info(
            "selected GraphQL op=%s score=%s reasons=%s",
            selected.operation_name,
            selected.score,
            selected.reasons,
        )

        return selected

    async def replay_once(
        self,
        captured: CapturedRequest,
        city: City,
        token: Optional[str],
    ) -> tuple[int, str]:
        session = await self.get_session()
        payload = patch_payload(captured.body, city, token)

        self.metrics.requests_total += 1
        self.metrics.inc_city(city.name, "requests")

        async with session.post(
            captured.url,
            headers={**HEADERS, **captured.headers},
            json=payload,
        ) as resp:
            text = await resp.text()

        self.metrics.inc_status(resp.status)

        if resp.status == 200:
            self.metrics.requests_ok += 1
        else:
            self.metrics.requests_failed += 1

        if resp.status == 403:
            self.metrics.forbidden += 1

        if resp.status == 429:
            self.metrics.rate_limited += 1

        return resp.status, text

    async def validate_replay(self, captured: CapturedRequest) -> bool:
        city = CITIES[0]
        status, text = await self.replay_once(captured, city, None)

        if status != 200:
            return False

        try:
            data = json.loads(text)
        except Exception:
            return False

        cards = extract_cards(data)
        return len(cards) > 0

    async def replay_city(self, captured: CapturedRequest, city: City) -> list[dict[str, Any]]:
        scope = f"city:{city.name}"
        breaker = self.breaker(scope)

        if breaker.is_open():
            log.warning("breaker open for %s reason=%s", scope, breaker.last_reason)
            return []

        all_cards: list[dict[str, Any]] = []
        token: Optional[str] = None

        for page_num in range(1, MAX_PAGES + 1):
            await asyncio.sleep(random.uniform(0.25, 0.75))

            try:
                status, text = await self.replay_once(captured, city, token)

                if status in (403, 429):
                    breaker.record_failure(f"HTTP {status}")
                    break

                if status != 200:
                    breaker.record_failure(f"HTTP {status}")
                    break

                data = json.loads(text)
                cards = extract_cards(data)
                token = extract_next_token(data)

                self.metrics.replay_pages += 1
                self.metrics.jobs_found += len(cards)
                self.metrics.inc_city(city.name, "jobs", len(cards))

                log.info(
                    "city=%s page=%s cards=%s next=%s",
                    city.name,
                    page_num,
                    len(cards),
                    bool(token),
                )

                if cards:
                    breaker.record_success()

                all_cards.extend(cards)

                if not cards or not token:
                    break

            except Exception as exc:
                self.metrics.replay_failures += 1
                breaker.record_failure(str(exc))
                log.warning("replay failed city=%s page=%s error=%s", city.name, page_num, exc)
                break

        return all_cards

    async def scan_jobs(self, account: Optional[Account] = None) -> list[dict[str, Any]]:
        captured = await self.capture_search_request(account.cookies if account else None)
        if not captured:
            return []

        valid = await self.validate_replay(captured)
        if not valid:
            log.warning("captured request failed replay validation")
            return []

        sem = asyncio.Semaphore(MAX_CONCURRENT_CITIES)

        async def run_city(city: City) -> list[dict[str, Any]]:
            async with sem:
                return await self.replay_city(captured, city)

        groups = await asyncio.gather(*(run_city(city) for city in CITIES))

        dedupe: dict[str, dict[str, Any]] = {}

        for group in groups:
            for job in group:
                key = job_key(job)
                if key and key != "None:None":
                    dedupe[key] = job

        jobs = list(dedupe.values())

        new_jobs = []
        for job in jobs:
            job["score"] = score_job(job)
            if mark_seen(job):
                self.metrics.jobs_new += 1
                new_jobs.append(job)

        new_jobs.sort(key=lambda j: j["score"], reverse=True)
        return new_jobs

    async def query_applications(self, account: Account) -> list[dict[str, Any]]:
        session = await self.get_session()

        self.metrics.inc_account(account.account_id, "query_applications")

        async with session.post(
            CANDIDATE_GRAPHQL,
            headers={**HEADERS, "Content-Type": "application/json"},
            cookies=account.cookies,
            json={
                "operationName": "queryApplicationsByBBCandidateIdV2",
                "variables": {
                    "locale": "en-GB",
                    "bbCandidateId": account.bb_candidate_id,
                },
                "query": QUERY_APPLICATIONS_V2,
            },
        ) as resp:
            text = await resp.text()

        if resp.status != 200:
            raise RuntimeError(f"queryApplications failed {resp.status}: {text[:300]}")

        data = json.loads(text)
        return (
            data.get("data", {})
            .get("queryApplicationsByBBCandidateIdV2", {})
            .get("applications", [])
        )

    async def update_workflow_step(
        self,
        account: Account,
        application_id: str,
        step: str,
    ) -> dict[str, Any]:
        session = await self.get_session()

        async with session.put(
            f"{BASE_URL}/application/api/candidate-application/update-workflow-step-name",
            headers={
                **HEADERS,
                "Content-Type": "application/json;charset=UTF-8",
                "x-csrf-token": account.csrf_token,
            },
            cookies=account.cookies,
            json={
                "applicationId": application_id,
                "workflowStepName": step,
            },
        ) as resp:
            text = await resp.text()

        if resp.status not in (200, 201):
            raise RuntimeError(f"workflow step failed {resp.status}: {text[:300]}")

        return json.loads(text)

    async def check_assessment_eligibility(
        self,
        account: Account,
        application_id: str,
        job_id: str,
    ) -> dict[str, Any]:
        session = await self.get_session()

        async with session.post(
            f"{BASE_URL}/application/api/candidate-application/assessment-eligibility",
            headers={
                **HEADERS,
                "Content-Type": "application/json;charset=UTF-8",
                "x-csrf-token": account.csrf_token,
            },
            cookies=account.cookies,
            json={
                "applicationId": application_id,
                "candidateId": account.candidate_id,
                "jobId": job_id,
            },
        ) as resp:
            text = await resp.text()

        if resp.status not in (200, 201):
            raise RuntimeError(f"assessment failed {resp.status}: {text[:300]}")

        return json.loads(text)

    async def submit_after_approval(
        self,
        account: Account,
        application_id: str,
        job_id: str,
        approved: bool,
    ) -> dict[str, Any]:
        self.metrics.submit_attempts += 1
        self.metrics.inc_account(account.account_id, "submit_attempts")

        before_apps = await self.query_applications(account)
        before = next((a for a in before_apps if a.get("applicationId") == application_id), {})
        before_state = str(before.get("applicationState") or before.get("step") or "UNKNOWN")

        if before.get("submitted") is True:
            result = {
                "success": True,
                "already_submitted": True,
                "state_before": before_state,
                "state_after": before_state,
            }
            audit_event(
                account.account_id,
                "submit",
                application_id,
                before_state,
                before_state,
                True,
                result,
            )
            return result

        if not approved:
            result = {
                "success": False,
                "error": "approval_required",
                "state_before": before_state,
            }
            audit_event(
                account.account_id,
                "submit_blocked",
                application_id,
                before_state,
                before_state,
                False,
                result,
            )
            return result

        try:
            await self.check_assessment_eligibility(account, application_id, job_id)
            await asyncio.sleep(0.4)

            review_response = await self.update_workflow_step(account, application_id, "review-submit")
            await asyncio.sleep(0.7)

            thank_you_response = await self.update_workflow_step(account, application_id, "thank-you")
            await asyncio.sleep(1.2)

            after_apps = await self.query_applications(account)
            after = next((a for a in after_apps if a.get("applicationId") == application_id), {})
            after_state = str(after.get("applicationState") or after.get("step") or "UNKNOWN")
            submitted = after.get("submitted") is True

            success = submitted or after_state == "APPLICATION_SUBMITTED"

            if success:
                self.metrics.submit_success += 1
            else:
                self.metrics.submit_failed += 1

            result = {
                "success": success,
                "state_before": before_state,
                "state_after": after_state,
                "submitted": submitted,
                "review_response": review_response,
                "thank_you_response": thank_you_response,
                "application_after": after,
            }

            audit_event(
                account.account_id,
                "submit",
                application_id,
                before_state,
                after_state,
                success,
                result,
            )

            return result

        except Exception as exc:
            self.metrics.submit_failed += 1
            result = {
                "success": False,
                "error": str(exc),
                "state_before": before_state,
                "state_after": "ERROR",
            }

            audit_event(
                account.account_id,
                "submit_error",
                application_id,
                before_state,
                "ERROR",
                False,
                result,
            )

            return result


async def main():
    core = AmazonProCore()

    try:
        jobs = await core.scan_jobs()

        print("\nTOP NEW JOBS\n")
        for job in jobs[:10]:
            print({
                "jobId": job.get("jobId"),
                "scheduleId": job.get("scheduleId"),
                "city": job.get("city"),
                "hours": job.get("hoursPerWeek"),
                "pay": job.get("totalPayRateMin") or job.get("basePay"),
                "score": job.get("score"),
            })

        print("\nMETRICS\n")
        print(json.dumps(core.metrics.__dict__, indent=2))

    finally:
        await core.close()


if __name__ == "__main__":
    asyncio.run(main())
