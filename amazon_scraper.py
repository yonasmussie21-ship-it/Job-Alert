import json
import logging
import aiohttp
import re
from playwright.async_api import async_playwright

from config import get_proxy_url
from storage import load_cookies, save_cookies, log_error
from job_parser import parse_card

log = logging.getLogger(__name__)

ASSET_BLOCK_PATTERN = "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}"
ACCOUNT_ID = 1


async def block_assets(route):
    await route.abort()


def clean_headers(headers: dict) -> dict:
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


def extract_job_cards(data: dict) -> list:
    return (
        data.get("data", {})
        .get("searchJobCardsByLocation", {})
        .get("jobCards", [])
    )


def extract_next_token(data: dict):
    return (
        data.get("data", {})
        .get("searchJobCardsByLocation", {})
        .get("nextToken")
    )


async def fetch_jobs() -> list:
    log.info("[SCAN_STARTED]")

    all_jobs = {}
    proxy = get_proxy_url()
    captured = {}
    direct_cards = []
    browser = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
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

            saved = load_cookies(ACCOUNT_ID)
            if saved:
                try:
                    await context.add_cookies(saved)
                except Exception as e:
                    log.warning(f"[COOKIE_LOAD_FAILED] {e}")

            page = await context.new_page()

            async def on_request(request):
                if "/graphql" not in request.url or captured:
                    return

                try:
                    body = request.post_data

                    if not body or "searchJobCardsByLocation" not in body:
                        return

                    headers = clean_headers(request.headers)

                    try:
                        body_json = json.loads(body)
                        search_req = (
                            body_json
                            .setdefault("variables", {})
                            .setdefault("searchJobRequest", {})
                        )

                        search_req["locale"] = "en-GB"
                        search_req["country"] = "United Kingdom"
                        search_req["keyWords"] = ""
                        search_req["pageSize"] = 100
                        search_req.pop("nextToken", None)

                        captured["body"] = json.dumps(body_json)

                    except Exception as e:
                        log.warning(f"[GRAPHQL_MODIFY_FAILED] {e}")
                        captured["body"] = body

                    captured["url"] = request.url
                    captured["headers"] = headers

                    log.info(f"[GRAPHQL_CAPTURED] {request.url}")

                except Exception as e:
                    log.warning(f"[CAPTURE_ERROR] {e}")

            async def on_response(response):
                if "/graphql" not in response.url or response.status != 200:
                    return

                try:
                    data = await response.json()
                    cards = extract_job_cards(data)

                    if cards:
                        log.info(f"[BROWSER_JOBS] {len(cards)} intercepted directly")
                        direct_cards.extend(cards)

                except Exception as e:
                    log.debug(f"[RESPONSE_READ_FAILED] {e}")

            page.on("request", on_request)
            page.on("response", on_response)

            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="domcontentloaded",
                timeout=60000,
            )

            await page.wait_for_timeout(3000)

            if not captured:
                log.info("[SCROLL_TRIGGER] No GraphQL yet — scrolling...")
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
                    save_cookies(ACCOUNT_ID, cookies, hvhcid)

            except Exception as e:
                log.warning(f"[COOKIE_SAVE_FAILED] {e}")

    except Exception as e:
        log.error(f"[BROWSER_ERROR] {e}")
        log_error("BROWSER_ERROR", str(e))

    finally:
        if browser:
            try:
                await browser.close()
            except Exception as e:
                log.warning(f"[BROWSER_CLOSE_ERROR] {e}")

    if captured and proxy:
        log.info("[PROXY_REPLAY] Replaying via Decodo UK proxy with pagination...")

        cards = await _replay_via_proxy(
            url=captured["url"],
            headers=captured["headers"],
            body=captured["body"],
            proxy=proxy,
        )

        source_cards = cards if cards else direct_cards

        if cards:
            log.info(f"[PROXY_JOBS] {len(cards)} total job cards returned")
        else:
            log.warning("[PROXY_FAILED] Proxy returned 0 — falling back to browser results")
            log_error("PROXY_FAILED", "Decodo returned 0 jobs")

    else:
        if not captured:
            log.warning("[NO_GRAPHQL] No request captured — using browser results only")
            log_error("NO_GRAPHQL_CAPTURED", "Browser did not fire GraphQL request")

        if not proxy:
            log.warning("[NO_PROXY] No proxy configured — using browser results only")

        source_cards = direct_cards

    for card in source_cards:
        job = parse_card(card)

        if job and job.get("id") not in all_jobs:
            all_jobs[job["id"]] = job

    log.info(f"[SCAN_COMPLETE] {len(all_jobs)} unique UK warehouse jobs found")
    return list(all_jobs.values())


async def _replay_via_proxy(url, headers, body, proxy) -> list:
    all_cards = []
    next_token = None
    safe_headers = clean_headers(headers)

    try:
        body_json = json.loads(body)
    except Exception as e:
        log.warning(f"[PROXY_BODY_PARSE_FAILED] {e}")
        log_error("PROXY_BODY_PARSE_FAILED", str(e))
        return []

    timeout = aiohttp.ClientTimeout(total=40)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for page_num in range(1, 21):
            try:
                search_req = (
                    body_json
                    .setdefault("variables", {})
                    .setdefault("searchJobRequest", {})
                )

                search_req["locale"] = "en-GB"
                search_req["country"] = "United Kingdom"
                search_req["keyWords"] = ""
                search_req["pageSize"] = 100

                if next_token:
                    search_req["nextToken"] = next_token
                else:
                    search_req.pop("nextToken", None)

                payload = json.dumps(body_json)

                async with session.post(
                    url,
                    data=payload,
                    headers=safe_headers,
                    proxy=proxy,
                ) as response:
                    text = await response.text()

                    if response.status != 200:
                        log.warning(f"[PROXY_STATUS] {response.status}: {text[:300]}")
                        log_error("PROXY_STATUS_ERROR", f"{response.status}: {text[:200]}")
                        break

                    try:
                        data = json.loads(text)
                    except Exception:
                        log.warning(f"[PROXY_JSON_INVALID] {text[:300]}")
                        log_error("PROXY_JSON_INVALID", text[:200])
                        break

                    if data.get("errors"):
                        error_text = json.dumps(data["errors"])[:500]
                        log.warning(f"[GRAPHQL_ERROR] {error_text}")
                        log_error("GRAPHQL_ERROR", error_text[:300])
                        break

                    cards = extract_job_cards(data)
                    next_token = extract_next_token(data)

                    log.info(
                        f"[PROXY_PAGE] {page_num}: {len(cards)} jobs | "
                        f"nextToken={'yes' if next_token else 'no'}"
                    )

                    all_cards.extend(cards)

                    if not next_token:
                        break

            except Exception as e:
                log.error(f"[PROXY_PAGE_ERROR] page {page_num}: {e}")
                log_error("PROXY_PAGE_ERROR", f"page {page_num}: {e}")
                break

    return all_cards


async def fetch_job_details(job) -> dict:
    browser = None

    link = job.get("link")
    if not link:
        log.warning(f"[JOB_DETAILS_NO_LINK] {job.get('id', '?')}")
        log_error("JOB_DETAILS_NO_LINK", job.get("id", "?"))
        return job

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
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

            await context.route(ASSET_BLOCK_PATTERN, block_assets)

            saved = load_cookies(ACCOUNT_ID)
            if saved:
                try:
                    await context.add_cookies(saved)
                except Exception as e:
                    log.warning(f"[DETAIL_COOKIE_LOAD_FAILED] {e}")

            page = await context.new_page()
            shifts_data = []

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

                except Exception as e:
                    log.debug(f"[DETAIL_RESPONSE_FAILED] {e}")

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

            for ct in [
                "Permanent",
                "Full-time",
                "Fixed-term",
                "Seasonal",
                "Temporary",
                "Part-time",
            ]:
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
                f"[JOB_DETAILS] day={job.get('firstDay', '?')} "
                f"shifts={len(job.get('shifts', []))} "
                f"hours={job.get('hours', '?')}"
            )

    except Exception as e:
        log.warning(f"[JOB_DETAILS_FAILED] {job.get('id', '?')}: {e}")
        log_error("JOB_DETAILS_FAILED", f"{job.get('id', '?')}: {e}")

    finally:
        if browser:
            try:
                await browser.close()
            except Exception as e:
                log.warning(f"[DETAIL_BROWSER_CLOSE_ERROR] {e}")

    return job
