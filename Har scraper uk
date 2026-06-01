"""
HAR-based Amazon UK Job Scraper - Full UK Coverage
====================================================
Covers all known Amazon warehouse locations across the UK.
Direct GraphQL call using HAR-confirmed payload - no Playwright needed.
"""

import asyncio
import json
import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://www.jobsatamazon.co.uk/graphql"

GRAPHQL_QUERY = """query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {
  searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {
    nextToken
    jobCards {
      jobId
      jobTitle
      jobType
      employmentType
      city
      state
      postalCode
      locationName
      totalPayRateMin
      totalPayRateMax
      distance
      featuredJob
      bonusJob
      scheduleCount
      geoClusterDescription
      virtualLocation
      payFrequency
      jobLocationType
    }
  }
}"""

# All known Amazon warehouse locations across the UK
# Covers England, Scotland, Wales and Northern Ireland
UK_WAREHOUSE_CITIES = {
    # Midlands
    "birmingham":     (52.4862, -1.8904),
    "coventry":       (52.4068, -1.5197),
    "dudley":         (52.5126, -2.0813),
    "wolverhampton":  (52.5862, -2.1288),
    "walsall":        (52.5860, -1.9820),
    "worcester":      (52.1936, -2.2212),
    "droitwich":      (52.2681, -2.1543),
    "rugby":          (52.3707, -1.2655),
    "tamworth":       (52.6340, -1.6960),
    "burton":         (52.8019, -1.6367),
    "nottingham":     (52.9548, -1.1581),
    "derby":          (52.9225, -1.4746),
    "leicester":      (52.6369, -1.1398),
    "northampton":    (52.2405, -0.9027),
    "milton_keynes":  (52.0406, -0.7594),
    "luton":          (51.8787, -0.4200),
    "stoke":          (53.0027, -2.1794),
    "shrewsbury":     (52.7071, -2.7540),

    # London & South East
    "london_east":    (51.5344, -0.0408),
    "london_north":   (51.6000, -0.1000),
    "london_west":    (51.5200, -0.2800),
    "dartford":       (51.4462, 0.2175),
    "barking":        (51.5362, 0.0798),
    "enfield":        (51.6538, -0.0799),
    "croydon":        (51.3714, -0.0977),
    "neasden":        (51.5536, -0.2496),
    "harlow":         (51.7760, 0.1020),
    "dunstable":      (51.8863, -0.5219),
    "reading":        (51.4543, -0.9781),
    "slough":         (51.5105, -0.5950),
    "guildford":      (51.2362, -0.5704),

    # South
    "portsmouth":     (50.8198, -1.0880),
    "southampton":    (50.9097, -1.4044),
    "bognor_regis":   (50.7820, -0.6740),
    "poole":          (50.7157, -1.9872),
    "bournemouth":    (50.7192, -1.8808),

    # South West
    "bristol":        (51.4545, -2.5879),
    "swindon":        (51.5558, -1.7797),
    "exeter":         (50.7184, -3.5339),
    "plymouth":       (50.3755, -4.1427),
    "gloucester":     (51.8642, -2.2380),

    # East
    "norwich":        (52.6309, 1.2974),
    "ipswich":        (52.0567, 1.1482),
    "cambridge":      (52.2053, 0.1218),
    "peterborough":   (52.5695, -0.2405),

    # North West
    "manchester":     (53.4808, -2.2426),
    "bolton":         (53.5777, -2.4294),
    "liverpool":      (53.4084, -2.9916),
    "warrington":     (53.3900, -2.5970),
    "wigan":          (53.5450, -2.6370),
    "preston":        (53.7632, -2.7031),
    "carlisle":       (54.8951, -2.9382),

    # Yorkshire
    "leeds":          (53.8008, -1.5491),
    "sheffield":      (53.3811, -1.4701),
    "bradford":       (53.7960, -1.7594),
    "hull":           (53.7457, -0.3367),
    "york":           (53.9590, -1.0815),
    "doncaster":      (53.5228, -1.1285),

    # North East
    "newcastle":      (54.9783, -1.6178),
    "sunderland":     (54.9069, -1.3838),
    "middlesbrough":  (54.5742, -1.2350),

    # Scotland
    "glasgow":        (55.8642, -4.2518),
    "edinburgh":      (55.9533, -3.1883),
    "aberdeen":       (57.1497, -2.0943),
    "dundee":         (56.4620, -2.9707),

    # Wales
    "cardiff":        (51.4816, -3.1791),
    "swansea":        (51.6214, -3.9436),
    "garden_city":    (53.2000, -3.0500),
    "newport":        (51.5842, -2.9977),

    # Northern Ireland
    "belfast":        (54.5973, -5.9301),
    "portadown":      (54.4180, -6.4500),
}


def _build_headers(cookies: list) -> dict:
    cookie_str = "; ".join(
        c.get("name", "") + "=" + c.get("value", "")
        for c in cookies
        if c.get("name") and c.get("value")
    )
    token = next(
        (c.get("value", "") for c in cookies if c.get("name") == "HVH_ACCESS_TOKEN"),
        ""
    )
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "country": "United Kingdom",
        "iscanary": "false",
        "origin": "https://www.jobsatamazon.co.uk",
        "referer": "https://www.jobsatamazon.co.uk/app",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "cookie": cookie_str,
        "authorization": token,
    }


def _build_payload(lat: float, lng: float, distance: int = 50, next_token: Optional[str] = None) -> dict:
    req = {
        "locale": "en-GB",
        "country": "United Kingdom",
        "keyWords": "",
        "equalFilters": [],
        "containFilters": [
            {"key": "isPrivateSchedule", "val": ["true", "false"]}
        ],
        "rangeFilters": [],
        "orFilters": [
            {"key": "bonusJob", "val": ["true"]},
            {"key": "featuredJob", "val": ["true"]},
        ],
        "dateFilters": [],
        "sorters": [],
        "pageSize": 100,
        "geoQueryClause": {
            "lat": lat,
            "lng": lng,
            "unit": "mi",
            "distance": distance,
        },
        "consolidateSchedule": True,
    }
    if next_token:
        req["nextToken"] = next_token
    return {
        "operationName": "searchJobCardsByLocation",
        "variables": {"searchJobRequest": req},
        "query": GRAPHQL_QUERY,
    }


async def search_jobs_for_city(
    session: aiohttp.ClientSession,
    city: str,
    cookies: list,
    distance: int = 50,
    proxy: Optional[str] = None,
    max_pages: int = 5,
) -> list:
    coords = UK_WAREHOUSE_CITIES.get(city.lower())
    if not coords:
        return []

    lat, lng = coords
    headers = _build_headers(cookies)
    all_cards = []
    next_token = None

    for page_num in range(1, max_pages + 1):
        payload = _build_payload(lat, lng, distance, next_token)
        try:
            async with session.post(
                GRAPHQL_URL,
                json=payload,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 401:
                    log.warning("[HAR_SCRAPER] 401 city=%s — cookies expired", city)
                    break
                if resp.status != 200:
                    log.warning("[HAR_SCRAPER] status=%s city=%s", resp.status, city)
                    break

                data = await resp.json()
                result = data.get("data", {}).get("searchJobCardsByLocation", {})
                cards = result.get("jobCards", []) or []
                next_token = result.get("nextToken")

                if cards:
                    log.info("[HAR_SCRAPER] city=%s page=%s cards=%s", city, page_num, len(cards))

                all_cards.extend(cards)

                if not cards or not next_token:
                    break

        except Exception as exc:
            log.warning("[HAR_SCRAPER] city=%s error=%s", city, exc)
            break

        await asyncio.sleep(0.3)

    return all_cards


async def scan_uk(
    cookies: list,
    proxy: Optional[str] = None,
) -> list:
    """
    Scan all UK Amazon warehouse cities.
    Returns deduplicated list of all job cards found.
    """
    all_jobs = {}

    # Use 75 mile radius — wide enough to catch jobs between cities
    distance = 75

    async with aiohttp.ClientSession() as session:
        for city in UK_WAREHOUSE_CITIES:
            cards = await search_jobs_for_city(session, city, cookies, distance, proxy)
            for card in cards:
                job_id = card.get("jobId")
                if job_id:
                    all_jobs[job_id] = card
            await asyncio.sleep(0.5)

    jobs = list(all_jobs.values())
    log.info("[HAR_SCRAPER] TOTAL unique UK jobs=%s", len(jobs))
    return jobs


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    accounts = json.load(open("/home/ubuntu/Job-Alert/data/accounts.json"))
    cookies = json.loads(accounts[0]["cookies"])
    proxy = os.environ.get("PROXY_URL")

    async def test():
        jobs = await scan_uk(cookies=cookies, proxy=proxy)
        print(f"\n{'='*50}")
        print(f"TOTAL UK JOBS FOUND: {len(jobs)}")
        print(f"{'='*50}")
        for j in sorted(jobs, key=lambda x: x.get("city", "")):
            print(f"  {j.get('jobTitle')} | {j.get('city')} | £{j.get('totalPayRateMin')}/hr | {j.get('employmentType')}")

    asyncio.run(test())
