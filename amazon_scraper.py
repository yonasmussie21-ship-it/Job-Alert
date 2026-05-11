import json
import logging
import aiohttp
import re
from playwright.async_api import async_playwright
from config import get_proxy_url
from storage import load_cookies, save_cookies
from job_parser import parse_card

log = logging.getLogger(__name__)

# ─── CORE SCRAPER ─────────────────────────────────────────────────────────────
async def fetch_jobs() -> list:
    """
    1. Load jobsatamazon.co.uk via Playwright
    2. Capture the GraphQL request
    3. Replay via Decodo UK proxy → gets ALL UK jobs
    4. Parse and return results
    """
    all_jobs     = {}
    proxy        = get_proxy_url()
    captured     = {}
    direct_cards = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled","--disable-gpu"]
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
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            await context.route(
                "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}",
                lambda route: route.abort()
            )
            saved = load_cookies()
            if saved:
                await context.add_cookies(saved)

            page = await context.new_page()

            async def on_request(request):
                if "/graphql" in request.url and not captured:
                    try:
                        body = request.post_data
                        if body and "searchJobCardsByLocation" in body:
                            headers = dict(request.headers)
                            for h in ["content-length","host",":method",
                                      ":path",":scheme",":authority"]:
                                headers.pop(h, None)
                            try:
                                body_json  = json.loads(body)
                                search_req = body_json.get("variables", {}).get("searchJobRequest", {})
                                search_req["country"]  = "United Kingdom"
                                search_req["keyWords"] = ""
                                search_req["pageSize"] = 100
                                captured["body"] = json.dumps(body_json)
                            except:
                                captured["body"] = body
                            captured["url"]     = request.url
                            captured["headers"] = headers
                            log.info(f"✅ Captured GraphQL — URL: {request.url}")
                    except Exception as e:
                        log.warning(f"Capture error: {e}")

            async def on_response(response):
                if "/graphql" in response.url and response.status == 200:
                    try:
                        data  = await response.json()
                        cards = (data.get("data", {})
                                     .get("searchJobCardsByLocation", {})
                                     .get("jobCards", []))
                        if cards:
                            log.info(f"🎯 Browser intercepted {len(cards)} jobs directly")
                            direct_cards.extend(cards)
                    except:
                        pass

            page.on("request",  on_request)
            page.on("response", on_response)

            await page.goto(
                "https://www.jobsatamazon.co.uk/app#/jobSearch?locale=en-GB&country=GBR",
                wait_until="domcontentloaded", timeout=60000
            )
            await page.wait_for_timeout(3000)

            if not captured:
                log.info("⚡ Scrolling to trigger GraphQL...")
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(3000)
                await page.mouse.wheel(0, -3000)
                await page.wait_for_timeout(2000)

            cookies = await context.cookies()
            if cookies:
                save_cookies(cookies)
            await browser.close()

    except Exception as e:
        log.error(f"Browser error: {e}")

    # Replay via Decodo to get ALL UK jobs
    if captured and proxy:
        log.info("🌐 Replaying via Decodo UK proxy (all UK jobs)...")
        cards = await _replay_via_proxy(
            url=captured["url"],
            headers=captured["headers"],
            body=captured["body"],
            proxy=proxy
        )
        if cards:
            log.info(f"🎯 Decodo returned {len(cards)} jobs!")
            for card in cards:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job
        else:
            log.warning("⚠️ Proxy returned 0 — using browser results")
            for card in direct_cards:
                job = parse_card(card)
                if job and job["id"] not in all_jobs:
                    all_jobs[job["id"]] = job
    else:
        for card in direct_cards:
            job = parse_card(card)
            if job and job["id"] not in all_jobs:
                all_jobs[job["id"]] = job

    log.info(f"👑 Total unique UK warehouse jobs: {len(all_jobs)}")
    return list(all_jobs.values())


async def _replay_via_proxy(url, headers, body, proxy) -> list:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, data=body, headers=headers, proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                status = response.status
                text   = await response.text()
                if status != 200:
                    log.warning(f"⚠️ Proxy status {status}: {text[:300]}")
                    return []
                try:
                    data = json.loads(text)
                except:
                    log.warning(f"⚠️ Invalid JSON: {text[:200]}")
                    return []
                if "errors" in data:
                    log.warning(f"⚠️ GraphQL errors: {json.dumps(data['errors'])[:300]}")
                    return []
                return (data.get("data", {})
                            .get("searchJobCardsByLocation", {})
                            .get("jobCards", []))
    except Exception as e:
        log.error(f"Proxy replay error: {e}")
        return []


# ─── FETCH FULL JOB DETAILS ──────────────────────────────────────────────────
async def fetch_job_details(job) -> dict:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox","--disable-gpu"]
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
            )
            await context.route(
                "**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webp}",
                lambda route: route.abort()
            )
            saved = load_cookies()
            if saved:
                await context.add_cookies(saved)

            page       = await context.new_page()
            shifts_data = []

            async def handle_response(response):
                try:
                    if "graphql" in response.url and response.status == 200:
                        data       = await response.json()
                        job_detail = data.get("data", {}).get("getJobDetailByJobId", {})
                        if job_detail:
                            shifts = (job_detail.get("jobCardDetail", {})
                                               .get("scheduleDetails", []))
                            if shifts:
                                shifts_data.extend(shifts)
                except:
                    pass

            page.on("response", handle_response)
            await page.goto(job["link"], wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.split('Loading').length < 4",
                    timeout=10000
                )
            except:
                pass
            await page.wait_for_timeout(2000)

            content = await page.inner_text("body")

            # Extract First Day
            for pattern in [
                r'(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+,?\s+\d+\s+[A-Za-z]+\s+\d{4})',
                r'(?:Tentative start date|Start date|First day)[:\s]+([A-Za-z]+ \d+, \d{4})',
                r'(?:Tentative start date|Start date|First day)[:\s]+(\d{4}-\d{2}-\d{2})',
            ]:
                m = re.search(pattern, content, re.IGNORECASE)
                if m:
                    job["firstDay"] = m.group(1).strip()
                    break

            # Extract Shifts
            shift_patterns = re.findall(
                r'([A-Za-z]{3}(?:,\s*[A-Za-z]{3})*\s+\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})',
                content
            )
            if shift_patterns:
                unique_shifts   = list(dict.fromkeys(shift_patterns))
                job["shifts"]   = unique_shifts
                job["schedule"] = unique_shifts[0]
                log.info(f"✅ Found {len(unique_shifts)} shifts")
            elif shifts_data:
                job["shifts"]   = [s.get("scheduleDisplay","") for s in shifts_data if s.get("scheduleDisplay")]
                job["schedule"] = job["shifts"][0] if job["shifts"] else None

            # Extract Hours
            m = re.search(r'(\d+)\s*(?:hrs?|hours?)\s*(?:per\s*week|/\s*week)', content, re.IGNORECASE)
            if m:
                job["hours"] = m.group(1)

            # Extract Contract
            for ct in ["Permanent","Full-time","Fixed-term","Seasonal","Temporary","Part-time"]:
                if ct.lower() in content.lower():
                    job["contract"] = ct
                    break

            # Extract Description
            desc_match = re.search(
                r'((?:Pick|Sort|Process|Receive|Load|Unload|Pack|Ship|Stow)[^.\n]{15,120}\.)',
                content, re.IGNORECASE
            )
            if desc_match:
                desc = desc_match.group(1).strip()
                if "Loading" not in desc and len(desc) >= 20:
                    job["description"] = desc

            await browser.close()
            log.info(f"✅ Details: day={job.get('firstDay','?')} "
                     f"shifts={len(job.get('shifts',[]))} "
                     f"hrs={job.get('hours','?')}")

    except Exception as e:
        log.warning(f"Detail fetch error: {e}")
    return job
