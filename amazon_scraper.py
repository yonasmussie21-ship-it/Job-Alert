"""
amazon_scraper.py

Production-grade Amazon UK Jobs scraper.

Responsibilities:
- Open the Amazon UK jobs SPA with Playwright.
- Capture the live GraphQL search request.
- Replay the captured request via aiohttp/proxy for pagination.
- Fall back to browser-captured cards if replay fails.
- Fetch job detail pages to enrich schedules/start date/hours/description.
- Avoid aggressive filtering here; parser decides final job validity.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import aiohttp
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from config import get_proxy_url
from job_parser import parse_card
from storage import load_cookies, log_error, save_cookies

log = logging.getLogger(__name__)

ASSET_BLOCK_PATTERN = "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}"
DEFAULT_ACCOUNT_ID = 1
MAX_PAGES = 20
MAX_CARDS = 5000
PAGE_SIZE = 100

SEARCH_URL = "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR"

SEARCH_RESULT_KEYS = (
    "searchJobCardsByLocation",
    "searchJobCards",
    "searchJobsV2",
    "searchJobs",
    "jobSearch",
)

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=45, connect=12)
RAW_DEBUG_DIR = Path("data/debug")


@dataclass(frozen=True)
class CapturedGraphQL:
    url: str
    headers: dict[str, str]
    body: str


class AmazonScanner:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._browser_lock = asyncio.Lock()

    async def get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
            return self._session

    async def close_session(self) -> None:
        async with self._session_lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None

    async def get_browser(self) -> Browser:
        async with self._browser_lock:
            if self._playwright is None:
                self._playwright = await async_playwright().start()

            if self._browser is None or not self._browser.is_connected():
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            return self._browser

    async def close_browser(self) -> None:
        async with self._browser_lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception as exc:
                    log.warning("[BROWSER_CLOSE_ERROR] %s", exc)
                self._browser = None

            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as exc:
                    log.warning("[PLAYWRIGHT_STOP_ERROR] %s", exc)
                self._playwright = None

    async def close(self) -> None:
        await self.close_session()
        await self.close_browser()

    async def create_context(self, account_id: int = DEFAULT_ACCOUNT_ID) -> BrowserContext:
        browser = await self.get_browser()
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1365, "height": 768},
        )

        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-GB', 'en']});
            """
        )

        await context.route(ASSET_BLOCK_PATTERN, block_assets)

        cookies = load_cookies(account_id)
        if cookies:
            try:
                await context.add_cookies(cookies)
                log.info("[COOKIES_LOADED] account=%s count=%s", account_id, len(cookies))
            except Exception as exc:
                log.warning("[COOKIE_LOAD_FAILED] account=%s error=%s", account_id, exc)

        return context

    async def fetch_jobs(self, account_id: int = DEFAULT_ACCOUNT_ID) -> list[dict[str, Any]]:
        started = time.monotonic()
        log.info("[SCAN_STARTED] account=%s", account_id)

        captured: Optional[CapturedGraphQL] = None
        browser_cards: list[dict[str, Any]] = []
        context: Optional[BrowserContext] = None

        try:
            context = await self.create_context(account_id)
            page = await context.new_page()

            async def on_request(request: Any) -> None:
                nonlocal captured
                if captured is not None or "/graphql" not in request.url:
                    return

                try:
                    body = request.post_data or ""
                    if not looks_like_search_body(body):
                        return

                    headers = clean_headers(request.headers)
                    try:
                        body_json = json.loads(body)
                        body = json.dumps(patch_search_payload(body_json), separators=(",", ":"))
                    except Exception as exc:
                        log.warning("[GRAPHQL_PATCH_FAILED] %s", exc)

                    captured = CapturedGraphQL(url=request.url, headers=headers, body=body)
                    log.info("[GRAPHQL_CAPTURED] url=%s", request.url)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("[GRAPHQL_CAPTURE_ERROR] %s", exc)

            async def on_response(response: Any) -> None:
                if "/graphql" not in response.url or response.status != 200:
                    return

                try:
                    data = await response.json()
                    cards = extract_job_cards(data)
                    if cards:
                        browser_cards.extend(cards)
                        log.info("[BROWSER_GRAPHQL_JOBS] cards=%s total=%s", len(cards), len(browser_cards))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("[GRAPHQL_RESPONSE_READ_FAILED] %s", exc)

            page.on("request", on_request)
            page.on("response", on_response)

            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(2500)
            await trigger_search_loading(page)

            await self.save_context_cookies(context, account_id)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("[BROWSER_SCAN_ERROR] %s", exc)
            log_error("BROWSER_SCAN_ERROR", str(exc))
        finally:
            if context:
                await context.close()

        source_cards = await self.resolve_source_cards(captured, browser_cards)

        jobs_by_id: dict[str, dict[str, Any]] = {}
        rejected = 0

        for index, card in enumerate(source_cards[:MAX_CARDS], start=1):
            job = parse_card(card)
            if not job:
                rejected += 1
                log.debug("[CARD_REJECTED] index=%s keys=%s", index, sorted(card.keys())[:25])
                continue

            job_id = str(job.get("id") or "").strip()
            if not job_id:
                rejected += 1
                log.info("[CARD_REJECTED] reason=missing_parsed_id index=%s", index)
                continue

            jobs_by_id.setdefault(job_id, job)

        duration = round(time.monotonic() - started, 2)
        log.info(
            "[SCAN_COMPLETE] accepted=%s rejected=%s source_cards=%s duration=%ss account=%s",
            len(jobs_by_id),
            rejected,
            len(source_cards),
            duration,
            account_id,
        )
        return list(jobs_by_id.values())

    async def resolve_source_cards(
        self,
        captured: Optional[CapturedGraphQL],
        browser_cards: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        proxy = get_proxy_url()

        if captured and proxy:
            cards = await self.replay_via_proxy(captured, proxy)
            if cards:
                log.info("[SOURCE_SELECTED] proxy cards=%s", len(cards))
                return cards

            log.warning("[PROXY_EMPTY] falling back to browser cards=%s", len(browser_cards))
            log_error("PROXY_EMPTY", "Proxy replay returned zero cards")

        if captured and not proxy:
            log.warning("[NO_PROXY] using browser-captured cards only")

        if not captured:
            log.warning("[NO_GRAPHQL_CAPTURED] using browser-captured cards=%s", len(browser_cards))
            log_error("NO_GRAPHQL_CAPTURED", "Browser did not expose a search GraphQL request")

        return browser_cards

    async def replay_via_proxy(self, captured: CapturedGraphQL, proxy: str) -> list[dict[str, Any]]:
        try:
            original_payload = json.loads(captured.body)
        except Exception as exc:
            log.warning("[PROXY_BODY_PARSE_FAILED] %s", exc)
            log_error("PROXY_BODY_PARSE_FAILED", str(exc))
            return []

        session = await self.get_session()
        headers = clean_headers(captured.headers)
        all_cards: list[dict[str, Any]] = []
        next_token: Optional[str] = None

        for page_num in range(1, MAX_PAGES + 1):
            await asyncio.sleep(random.uniform(0.15, 0.55))
            payload = patch_search_payload(original_payload, next_token=next_token)

            try:
                async with session.post(
                    captured.url,
                    json=payload,
                    headers=headers,
                    proxy=proxy,
                ) as response:
                    text = await response.text()

                if response.status != 200:
                    log.warning("[PROXY_STATUS] page=%s status=%s body=%s", page_num, response.status, text[:500])
                    log_error("PROXY_STATUS_ERROR", f"page={page_num} status={response.status} body={text[:300]}")
                    break

                try:
                    data = json.loads(text)
                except Exception:
                    log.warning("[PROXY_JSON_INVALID] page=%s body=%s", page_num, text[:500])
                    log_error("PROXY_JSON_INVALID", text[:300])
                    break

                if page_num == 1:
                    save_debug_payload("graphql_first_page.json", data)

                if data.get("errors"):
                    error_text = json.dumps(data["errors"])[:700]
                    log.warning("[GRAPHQL_ERRORS] page=%s errors=%s", page_num, error_text)
                    log_error("GRAPHQL_ERRORS", error_text[:300])
                    break

                cards = extract_job_cards(data)
                next_token = extract_next_token(data)
                all_cards.extend(cards)

                log.info(
                    "[PROXY_PAGE] page=%s cards=%s total=%s nextToken=%s",
                    page_num,
                    len(cards),
                    len(all_cards),
                    "yes" if next_token else "no",
                )

                if len(all_cards) >= MAX_CARDS:
                    return all_cards[:MAX_CARDS]

                if not next_token:
                    break

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("[PROXY_PAGE_ERROR] page=%s error=%s", page_num, exc)
                log_error("PROXY_PAGE_ERROR", f"page={page_num} error={exc}")
                break

        return all_cards

    async def save_context_cookies(self, context: BrowserContext, account_id: int) -> None:
        try:
            cookies = await context.cookies()
            if not cookies:
                return

            hvhcid = next((c.get("value", "") for c in cookies if c.get("name") == "hvhcid"), "")
            save_cookies(account_id, cookies, hvhcid)
            log.info("[COOKIES_SAVED] account=%s count=%s hvhcid=%s", account_id, len(cookies), "yes" if hvhcid else "no")
        except Exception as exc:
            log.warning("[COOKIE_SAVE_FAILED] account=%s error=%s", account_id, exc)

    async def fetch_job_details(
        self,
        job: dict[str, Any],
        account_id: int = DEFAULT_ACCOUNT_ID,
    ) -> dict[str, Any]:
        link = job.get("link") or build_job_link(job.get("id"))
        if not link:
            log.warning("[JOB_DETAILS_NO_LINK] id=%s", job.get("id", "?"))
            return job

        context: Optional[BrowserContext] = None
        try:
            context = await self.create_context(account_id)
            page = await context.new_page()
            shifts_data: list[dict[str, Any]] = []

            async def handle_response(response: Any) -> None:
                if "graphql" not in response.url or response.status != 200:
                    return
                try:
                    data = await response.json()
                    detail = extract_detail_result(data)
                    schedule_details = deep_find_lists_by_key(detail, ("scheduleDetails", "schedules", "shiftDetails"))
                    for schedule_list in schedule_details:
                        for item in schedule_list:
                            if isinstance(item, dict):
                                shifts_data.append(item)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("[DETAIL_RESPONSE_FAILED] %s", exc)

            page.on("response", handle_response)
            await page.goto(link, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(4000)

            try:
                await page.wait_for_function(
                    "() => document.body && document.body.innerText.length > 100",
                    timeout=10_000,
                )
            except Exception:
                pass

            content = await page.inner_text("body")
            parse_detail_content(job, content, shifts_data)

            log.info(
                "[JOB_DETAILS_COMPLETE] id=%s firstDay=%s shifts=%s hours=%s",
                job.get("id", "?"),
                job.get("firstDay") or "?",
                len(job.get("shifts") or []),
                job.get("hours") or "?",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[JOB_DETAILS_FAILED] id=%s error=%s", job.get("id", "?"), exc)
            log_error("JOB_DETAILS_FAILED", f"{job.get('id', '?')}: {exc}")
        finally:
            if context:
                await context.close()

        return job

    async def fetch_job_details_batch(
        self,
        jobs: list[dict[str, Any]],
        account_id: int = DEFAULT_ACCOUNT_ID,
        max_concurrent: int = 3,
    ) -> list[dict[str, Any]]:
        if not jobs:
            return []

        semaphore = asyncio.Semaphore(max(1, max_concurrent))

        async def run_one(job: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await self.fetch_job_details(job, account_id=account_id)

        return list(await asyncio.gather(*(run_one(job) for job in jobs)))


scanner = AmazonScanner()


async def block_assets(route: Any) -> None:
    await route.abort()


async def trigger_search_loading(page: Page) -> None:
    for delta in (1800, 2600, -1200, 3200):
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(1200)


def clean_headers(headers: dict[str, str]) -> dict[str, str]:
    blocked = {
        "content-length",
        "host",
        "accept-encoding",
        "connection",
        ":method",
        ":path",
        ":scheme",
        ":authority",
    }
    return {k: v for k, v in dict(headers).items() if k.lower() not in blocked}


def looks_like_search_body(body: str) -> bool:
    if not body:
        return False
    return any(
        term in body
        for term in (
            "searchJobCards",
            "searchJobCardsByLocation",
            "searchJobsV2",
            "searchJobs",
            "jobSearch",
        )
    )


def extract_search_result(data: dict[str, Any]) -> dict[str, Any]:
    root = data.get("data")
    if not isinstance(root, dict):
        return {}

    for key in SEARCH_RESULT_KEYS:
        value = root.get(key)
        if isinstance(value, dict):
            return value

    for key, value in root.items():
        if isinstance(value, dict) and any(k in value for k in ("jobCards", "jobs", "results", "items", "edges")):
            log.warning("[GRAPHQL_SCHEMA_DRIFT] result_key=%s", key)
            return value

    return {}


def extract_job_cards(data: dict[str, Any]) -> list[dict[str, Any]]:
    result = extract_search_result(data)
    cards = cards_from_result(result)
    if cards:
        return cards

    candidates: list[dict[str, Any]] = []
    collect_likely_cards(data, candidates)

    deduped: dict[str, dict[str, Any]] = {}
    for card in candidates:
        job_id = str(
            card.get("jobId")
            or card.get("id")
            or card.get("job_id")
            or card.get("job", {}).get("id", "")
        ).strip()
        if job_id:
            deduped.setdefault(job_id, card)

    if deduped:
        log.warning("[GRAPHQL_DEEP_FALLBACK] cards=%s", len(deduped))

    return list(deduped.values())


def cards_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("jobCards", "jobs", "results", "items"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    edges = result.get("edges")
    if isinstance(edges, list):
        cards = []
        for edge in edges:
            if isinstance(edge, dict):
                node = edge.get("node")
                if isinstance(node, dict):
                    cards.append(node)
        if cards:
            return cards

    return []


def collect_likely_cards(value: Any, output: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 7:
        return

    if isinstance(value, dict):
        keys = set(value.keys())
        if keys & {"jobId", "jobTitle", "postalCode", "employmentType", "totalPayRateMax"}:
            output.append(value)
            return

        for child in value.values():
            collect_likely_cards(child, output, depth + 1)

    elif isinstance(value, list):
        for child in value:
            collect_likely_cards(child, output, depth + 1)


def extract_next_token(data: dict[str, Any]) -> Optional[str]:
    result = extract_search_result(data)

    for key in ("nextToken", "nextPageToken", "cursor", "paginationToken"):
        token = result.get(key)
        if token:
            return str(token)

    pagination = result.get("pagination")
    if isinstance(pagination, dict):
        for key in ("nextToken", "nextPageToken", "cursor"):
            token = pagination.get(key)
            if token:
                return str(token)

    page_info = result.get("pageInfo")
    if isinstance(page_info, dict):
        for key in ("endCursor", "nextCursor", "nextToken"):
            token = page_info.get(key)
            if token:
                return str(token)

    return None


def patch_search_payload(body_json: dict[str, Any], next_token: Optional[str] = None) -> dict[str, Any]:
    payload = copy.deepcopy(body_json)
    variables = payload.setdefault("variables", {})

    search_req = (
        variables.get("searchJobRequest")
        or variables.get("input")
        or variables.get("request")
        or {}
    )

    # ── Core fields ───────────────────────────────────────
    search_req["locale"] = "en-GB"
    search_req["country"] = "United Kingdom"  # FIXED: was "GBR"
    search_req["keyWords"] = ""
    search_req["pageSize"] = PAGE_SIZE

    # ── CONFIRMED geoQueryClause from HAR analysis ────────
    # Old wrong approach used top-level lat/lng/radius.
    # Confirmed correct structure is nested geoQueryClause.
    if "geoQueryClause" not in search_req:
        # Only inject if not already present from browser capture
        search_req["geoQueryClause"] = {
            "lat": search_req.pop("lat", 52.4862),
            "lng": search_req.pop("lng", -1.8904),
            "unit": "mi",
            "distance": search_req.pop("radius", search_req.pop("distance", 30)),
        }

    # ── CONFIRMED filters from HAR analysis ──────────────
    search_req.setdefault("containFilters", [
        {"key": "isPrivateSchedule", "val": ["true", "false"]}
    ])
    search_req.setdefault("orFilters", [
        {"key": "bonusJob", "val": ["true"]},
        {"key": "featuredJob", "val": ["true"]}
    ])
    search_req.setdefault("equalFilters", [])
    search_req.setdefault("rangeFilters", [])
    search_req.setdefault("dateFilters", [])
    search_req.setdefault("sorters", [])
    search_req.setdefault("consolidateSchedule", True)

    # ── Pagination ────────────────────────────────────────
    if next_token:
        search_req["nextToken"] = next_token
        search_req["nextPageToken"] = next_token
    else:
        search_req.pop("nextToken", None)
        search_req.pop("nextPageToken", None)

    variables["searchJobRequest"] = search_req
    return payload


def extract_detail_result(data: dict[str, Any]) -> dict[str, Any]:
    root = data.get("data")
    if not isinstance(root, dict):
        return {}

    for key in ("getJobDetailByJobId", "jobDetail", "getJobDetail"):
        value = root.get(key)
        if isinstance(value, dict):
            return value

    for value in root.values():
        if isinstance(value, dict) and any(k in value for k in ("jobCardDetail", "scheduleDetails", "title")):
            return value

    return root


def deep_find_lists_by_key(value: Any, keys: tuple[str, ...], depth: int = 0) -> list[list[Any]]:
    if depth > 8:
        return []

    found: list[list[Any]] = []

    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, list):
                found.append(child)
            else:
                found.extend(deep_find_lists_by_key(child, keys, depth + 1))

    elif isinstance(value, list):
        for child in value:
            found.extend(deep_find_lists_by_key(child, keys, depth + 1))

    return found


def build_job_link(job_id: Any) -> Optional[str]:
    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    return f"https://www.jobsatamazon.co.uk/app#/jobDetail?jobId={job_id}"


@lru_cache(maxsize=1)
def detail_patterns() -> dict[str, re.Pattern[str]]:
    return {
        "start_1": re.compile(
            r"(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+,?\s+\d+\s+[A-Za-z]+\s+\d{4})",
            re.IGNORECASE,
        ),
        "start_2": re.compile(
            r"(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+ \d{1,2}, \d{4})",
            re.IGNORECASE,
        ),
        "start_3": re.compile(
            r"(?:Tentative start date|Start date|First day)[:\s]+(\d{4}-\d{2}-\d{2})",
            re.IGNORECASE,
        ),
        "shifts": re.compile(
            r"([A-Za-z]{3}(?:,\s*[A-Za-z]{3})*\s+\d{1,2}:\d{2}\s*[-\u2013]\s*\d{1,2}:\d{2})",
            re.IGNORECASE,
        ),
        "hours": re.compile(
            r"(\d{1,2})\s*(?:hrs?|hours?)\s*(?:per\s*week|/\s*week|weekly)?",
            re.IGNORECASE,
        ),
        "description": re.compile(
            r"((?:Pick|Sort|Process|Receive|Load|Unload|Pack|Ship|Stow|Deliver)[^.\n]{15,160}\.)",
            re.IGNORECASE,
        ),
    }


def parse_detail_content(job: dict[str, Any], content: str, shifts_data: list[dict[str, Any]]) -> None:
    content = str(content or "")
    patterns = detail_patterns()

    for key in ("start_1", "start_2", "start_3"):
        match = patterns[key].search(content)
        if match:
            job["firstDay"] = match.group(1).strip()
            break

    shift_matches = patterns["shifts"].findall(content)
    shifts = list(dict.fromkeys(s.strip() for s in shift_matches if s and s.strip()))

    if not shifts and shifts_data:
        for item in shifts_data:
            shift_text = (
                item.get("scheduleDisplay")
                or item.get("schedule")
                or item.get("shiftCode")
                or item.get("displayText")
                or ""
            )
            if shift_text:
                shifts.append(str(shift_text).strip())
        shifts = list(dict.fromkeys(shifts))

    if shifts:
        job["shifts"] = shifts
        job["schedule"] = job.get("schedule") or shifts[0]

    hours_match = patterns["hours"].search(content)
    if hours_match:
        job["hours"] = hours_match.group(1)

    content_lower = content.lower()
    for contract_type in ("Permanent", "Full-time", "Full time", "Fixed-term", "Seasonal", "Temporary", "Part-time", "Part time"):
        if contract_type.lower() in content_lower:
            job["contract"] = contract_type.replace("Full time", "Full-time").replace("Part time", "Part-time")
            break

    desc_match = patterns["description"].search(content)
    if desc_match:
        desc = desc_match.group(1).strip()
        if "loading" not in desc.lower() and len(desc) >= 20:
            job["description"] = desc


def save_debug_payload(filename: str, data: Any) -> None:
    try:
        RAW_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = RAW_DEBUG_DIR / filename
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False)[:500_000], encoding="utf-8")
    except Exception as exc:
        log.debug("[DEBUG_PAYLOAD_SAVE_FAILED] %s", exc)


async def fetch_jobs(account_id: int = DEFAULT_ACCOUNT_ID) -> list[dict[str, Any]]:
    return await scanner.fetch_jobs(account_id=account_id)


async def fetch_job_details(job: dict[str, Any], account_id: int = DEFAULT_ACCOUNT_ID) -> dict[str, Any]:
    return await scanner.fetch_job_details(job, account_id=account_id)


async def fetch_job_details_batch(
    jobs: list[dict[str, Any]],
    account_id: int = DEFAULT_ACCOUNT_ID,
    max_concurrent: int = 3,
) -> list[dict[str, Any]]:
    return await scanner.fetch_job_details_batch(jobs, account_id=account_id, max_concurrent=max_concurrent)


async def close_session() -> None:
    await scanner.close_session()


async def close_scanner() -> None:
    await scanner.close()
