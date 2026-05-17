import asyncio
import copy
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from playwright.async_api import async_playwright, Browser, BrowserContext, Playwright

from config import get_proxy_url
from storage import load_cookies, save_cookies, log_error
from job_parser import parse_card

log = logging.getLogger(__name__)

ASSET_BLOCK_PATTERN = "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}"

DEFAULT_ACCOUNT_ID = 1
MAX_PAGES = 20
MAX_CARDS = 5000

SESSION: Optional[aiohttp.ClientSession] = None
SESSION_LOCK = asyncio.Lock()

SEARCH_RESULT_KEYS = [
    "searchJobCardsByLocation",
    "searchJobsV2",
    "searchJobs",
    "jobSearch",
]


async def block_assets(route):
    await route.abort()


async def get_session() -> aiohttp.ClientSession:
    global SESSION

    async with SESSION_LOCK:
        if SESSION is None or SESSION.closed:
            SESSION = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=40)
            )

    return SESSION


async def close_session() -> None:
    global SESSION

    async with SESSION_LOCK:
        if SESSION and not SESSION.closed:
            await SESSION.close()
        SESSION = None


def clean_headers(headers: Dict[str, str]) -> Dict[str, str]:
    safe_headers = dict(headers)

    for h in [
        "content-length",
        "host",
        "accept-encoding",
        "connection",
        ":method",
        ":path",
        ":scheme",
        ":authority",
    ]:
        safe_headers.pop(h, None)

    return safe_headers


def looks_like_search_body(body: str) -> bool:
    return any(
        term in body
        for term in [
            "searchJobCards",
            "searchJobCardsByLocation",
            "searchJobsV2",
            "searchJobs",
            "jobSearch",
        ]
    )


def extract_search_result(data: Dict[str, Any]) -> Dict[str, Any]:
    root = data.get("data", {})

    if not isinstance(root, dict):
        return {}

    for key in SEARCH_RESULT_KEYS:
        value = root.get(key)
        if isinstance(value, dict):
            return value

    for key, value in root.items():
        if isinstance(value, dict):
            if any(k in value for k in ["jobCards", "jobs", "results", "items"]):
                log.warning("[GRAPHQL_SCHEMA_DRIFT] fallback_result_key=%s", key)
                return value

    log.warning("[GRAPHQL_SCHEMA_UNKNOWN] data_keys=%s", list(root.keys()))
    return {}


def extract_job_cards(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = extract_search_result(data)

    for key in ["jobCards", "jobs", "results", "items"]:
        cards = result.get(key)

        if isinstance(cards, list):
            return cards

    return []


def extract_next_token(data: Dict[str, Any]) -> Optional[str]:
    result = extract_search_result(data)

    for key in ["nextToken", "nextPageToken", "cursor", "paginationToken"]:
        token = result.get(key)

        if token:
            return str(token)

    pagination = result.get("pagination")

    if isinstance(pagination, dict):
        for key in ["nextToken", "nextPageToken", "cursor"]:
            token = pagination.get(key)
            if token:
                return str(token)

    return None


def patch_search_payload(body_json: Dict[str, Any], next_token: Optional[str] = None) -> Dict[str, Any]:
    payload = copy.deepcopy(body_json)

    variables = payload.setdefault("variables", {})
    search_req = variables.setdefault("searchJobRequest", {})

    search_req["locale"] = "en-GB"
    search_req["country"] = "United Kingdom"
    search_req["keyWords"] = ""
    search_req["pageSize"] = 100

    if next_token:
        search_req["nextToken"] = next_token
    else:
        search_req.pop("nextToken", None)

    return payload


async def create_browser_context(account_id: int) -> Tuple[Playwright, Browser, BrowserContext]:
    playwright = await async_playwright().start()

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="en-GB",
    )

    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

    await context.route(ASSET_BLOCK_PATTERN, block_assets)

    saved = load_cookies(account_id)

    if saved:
        try:
            await context.add_cookies(saved)
        except Exception as e:
            log.warning("[COOKIE_LOAD_FAILED] account=%s error=%s", account_id, e)

    return playwright, browser, context


async def close_browser_stack(
    playwright: Optional[Playwright],
    browser: Optional[Browser],
) -> None:
    if browser:
        try:
            await browser.close()
        except Exception as e:
            log.warning("[BROWSER_CLOSE_ERROR] %s", e)

    if playwright:
        try:
            await playwright.stop()
        except Exception as e:
            log.warning("[PLAYWRIGHT_STOP_ERROR] %s", e)


async def fetch_jobs(account_id: int = DEFAULT_ACCOUNT_ID) -> List[Dict[str, Any]]:
    scan_start = time.monotonic()
    log.info("[SCAN_STARTED] account=%s", account_id)

    all_jobs: Dict[str, Dict[str, Any]] = {}
    proxy = get_proxy_url()
    captured: Dict[str, Any] = {}
    direct_cards: List[Dict[str, Any]] = []

    playwright = None
    browser = None
    context = None

    try:
        playwright, browser, context = await create_browser_context(account_id)
        page = await context.new_page()

        async def on_request(request):
            if "/graphql" not in request.url or captured:
                return

            try:
                body = request.post_data

                if not body or not looks_like_search_body(body):
                    return

                headers = clean_headers(request.headers)

                try:
                    body_json = json.loads(body)
                    patched = patch_search_payload(body_json)
                    captured["body"] = json.dumps(patched)
                except Exception as e:
                    log.warning("[GRAPHQL_MODIFY_FAILED] %s", e)
                    captured["body"] = body

                captured["url"] = request.url
                captured["headers"] = headers

                log.info("[GRAPHQL_CAPTURED] %s", request.url)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                log.warning("[CAPTURE_ERROR] %s", e)

        async def on_response(response):
            if "/graphql" not in response.url or response.status != 200:
                return

            try:
                data = await response.json()
                cards = extract_job_cards(data)

                if cards:
                    log.info("[BROWSER_JOBS] %s intercepted directly", len(cards))
                    direct_cards.extend(cards)

                    if len(direct_cards) >= MAX_CARDS:
                        log.warning("[DIRECT_CARD_LIMIT_REACHED] %s", MAX_CARDS)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                log.debug("[RESPONSE_READ_FAILED] %s", e)

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto(
            "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        await page.wait_for_timeout(3000)

        if not captured:
            log.info("[SCROLL_TRIGGER] No GraphQL yet — scrolling")
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(3000)
            await page.mouse.wheel(0, -3000)
            await page.wait_for_timeout(2000)

        try:
            cookies = await context.cookies()

            if cookies:
                hvhcid = next(
                    (c.get("value", "") for c in cookies if c.get("name") == "hvhcid"),
                    "",
                )
                save_cookies(account_id, cookies, hvhcid)

        except Exception as e:
            log.warning("[COOKIE_SAVE_FAILED] account=%s error=%s", account_id, e)

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log.error("[BROWSER_ERROR] %s", e)
        log_error("BROWSER_ERROR", str(e))

    finally:
        await close_browser_stack(playwright, browser)

    if captured and proxy:
        log.info("[PROXY_REPLAY] Replaying via proxy with pagination")

        cards = await _replay_via_proxy(
            url=captured["url"],
            headers=captured["headers"],
            body=captured["body"],
            proxy=proxy,
        )

        source_cards = cards if cards else direct_cards

        if cards:
            log.info("[PROXY_JOBS] %s total job cards returned", len(cards))
        else:
            log.warning("[PROXY_FAILED] Proxy returned 0 — falling back to browser results")
            log_error("PROXY_FAILED", "Proxy returned 0 jobs")

    else:
        if not captured:
            log.warning("[NO_GRAPHQL] No request captured — using browser results only")
            log_error("NO_GRAPHQL_CAPTURED", "Browser did not fire GraphQL request")

        if not proxy:
            log.warning("[NO_PROXY] No proxy configured — using browser results only")

        source_cards = direct_cards

    for card in source_cards[:MAX_CARDS]:
        job = parse_card(card)

        if job and job.get("id") not in all_jobs:
            all_jobs[job["id"]] = job

    duration = round(time.monotonic() - scan_start, 2)

    log.info(
        "[SCAN_COMPLETE] jobs=%s cards=%s duration=%ss account=%s",
        len(all_jobs),
        len(source_cards),
        duration,
        account_id,
    )

    return list(all_jobs.values())


async def _replay_via_proxy(
    url: str,
    headers: Dict[str, str],
    body: str,
    proxy: str,
) -> List[Dict[str, Any]]:
    all_cards: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    safe_headers = clean_headers(headers)

    try:
        original_body_json = json.loads(body)
    except Exception as e:
        log.warning("[PROXY_BODY_PARSE_FAILED] %s", e)
        log_error("PROXY_BODY_PARSE_FAILED", str(e))
        return []

    session = await get_session()

    for page_num in range(1, MAX_PAGES + 1):
        try:
            payload_json = patch_search_payload(original_body_json, next_token)
            payload = json.dumps(payload_json)

            async with session.post(
                url,
                data=payload,
                headers=safe_headers,
                proxy=proxy,
            ) as response:
                text = await response.text()

                if response.status != 200:
                    log.warning("[PROXY_STATUS] %s: %s", response.status, text[:300])
                    log_error("PROXY_STATUS_ERROR", f"{response.status}: {text[:200]}")
                    break

                try:
                    data = json.loads(text)
                except Exception:
                    log.warning("[PROXY_JSON_INVALID] %s", text[:300])
                    log_error("PROXY_JSON_INVALID", text[:200])
                    break

                if data.get("errors"):
                    error_text = json.dumps(data["errors"])[:500]
                    log.warning("[GRAPHQL_ERROR] %s", error_text)
                    log_error("GRAPHQL_ERROR", error_text[:300])
                    break

                cards = extract_job_cards(data)
                next_token = extract_next_token(data)

                log.info(
                    "[PROXY_PAGE] page=%s cards=%s nextToken=%s",
                    page_num,
                    len(cards),
                    "yes" if next_token else "no",
                )

                all_cards.extend(cards)

                if len(all_cards) >= MAX_CARDS:
                    log.warning("[CARD_LIMIT_REACHED] %s", MAX_CARDS)
                    all_cards = all_cards[:MAX_CARDS]
                    break

                if not next_token:
                    break

        except asyncio.CancelledError:
            raise

        except Exception as e:
            log.error("[PROXY_PAGE_ERROR] page=%s error=%s", page_num, e)
            log_error("PROXY_PAGE_ERROR", f"page {page_num}: {e}")
            break

    return all_cards


async def fetch_job_details(
    job: Dict[str, Any],
    account_id: int = DEFAULT_ACCOUNT_ID,
) -> Dict[str, Any]:
    playwright = None
    browser = None
    context = None

    link = job.get("link")

    if not link:
        log.warning("[JOB_DETAILS_NO_LINK] %s", job.get("id", "?"))
        log_error("JOB_DETAILS_NO_LINK", job.get("id", "?"))
        return job

    try:
        playwright, browser, context = await create_browser_context(account_id)
        page = await context.new_page()
        shifts_data: List[Dict[str, Any]] = []

        async def handle_response(response):
            try:
                if "graphql" not in response.url or response.status != 200:
                    return

                data = await response.json()
                job_detail = data.get("data", {}).get("getJobDetailByJobId", {})

                if not job_detail:
                    return

                shifts = (
                    job_detail
                    .get("jobCardDetail", {})
                    .get("scheduleDetails", [])
                )

                if shifts:
                    shifts_data.extend(shifts)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                log.debug("[DETAIL_RESPONSE_FAILED] %s", e)

        page.on("response", handle_response)

        await page.goto(link, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)

        try:
            await page.wait_for_function(
                "() => document.body && document.body.innerText.split('Loading').length < 4",
                timeout=10000,
            )
        except Exception:
            pass

        await page.wait_for_timeout(2000)
        content = await page.inner_text("body")

        for pattern in [
            r"(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+,?\s+\d+\s+[A-Za-z]+\s+\d{4})",
            r"(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+ \d+, \d{4})",
            r"(?:Tentative start date|Start date|First day)[:\s]+(\d{4}-\d{2}-\d{2})",
        ]:
            m = re.search(pattern, content, re.IGNORECASE)

            if m:
                job["firstDay"] = m.group(1).strip()
                break

        shift_patterns = re.findall(
            r"([A-Za-z]{3}(?:,\s*[A-Za-z]{3})*\s+\d{1,2}:\d{2}\s*[-\u2013]\s*\d{1,2}:\d{2})",
            content,
        )

        if shift_patterns:
            unique_shifts = list(dict.fromkeys(shift_patterns))
            job["shifts"] = unique_shifts
            job["schedule"] = unique_shifts[0]

        elif shifts_data:
            job["shifts"] = [
                s.get("scheduleDisplay", "")
                for s in shifts_data
                if s.get("scheduleDisplay")
            ]
            job["schedule"] = job["shifts"][0] if job["shifts"] else None

        m = re.search(
            r"(\d+)\s*(?:hrs?|hours?)\s*(?:per\s*week|/\s*week)",
            content,
            re.IGNORECASE,
        )

        if m:
            job["hours"] = m.group(1)

        for ct in ["Permanent", "Full-time", "Fixed-term", "Seasonal", "Temporary", "Part-time"]:
            if ct.lower() in content.lower():
                job["contract"] = ct
                break

        desc_match = re.search(
            r"((?:Pick|Sort|Process|Receive|Load|Unload|Pack|Ship|Stow)[^.\n]{15,120}\.)",
            content,
            re.IGNORECASE,
        )

        if desc_match:
            desc = desc_match.group(1).strip()

            if "Loading" not in desc and len(desc) >= 20:
                job["description"] = desc

        log.info(
            "[JOB_DETAILS] id=%s day=%s shifts=%s hours=%s",
            job.get("id", "?"),
            job.get("firstDay", "?"),
            len(job.get("shifts", [])),
            job.get("hours", "?"),
        )

    except asyncio.CancelledError:
        raise

    except Exception as e:
        log.warning("[JOB_DETAILS_FAILED] %s: %s", job.get("id", "?"), e)
        log_error("JOB_DETAILS_FAILED", f"{job.get('id', '?')}: {e}")

    finally:
        await close_browser_stack(playwright, browser)

    return job


async def fetch_job_details_batch(
    jobs: List[Dict[str, Any]],
    account_id: int = DEFAULT_ACCOUNT_ID,
    max_concurrent: int = 3,
) -> List[Dict[str, Any]]:
    if not jobs:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)

    async def fetch_with_limit(job: Dict[str, Any]) -> Dict[str, Any]:
        async with semaphore:
            try:
                return await fetch_job_details(job, account_id=account_id)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                log.warning("[BATCH_DETAIL_FAILED] %s: %s", job.get("id", "?"), e)
                return job

    return list(await asyncio.gather(*[fetch_with_limit(job) for job in jobs]))
