# scheduler.py

import asyncio
import logging
from typing import Dict

from config import is_peak_time
from scraper import fetch_jobs
from storage import save_known_job

log = logging.getLogger(__name__)

_shutdown_event = None


def set_shutdown_event(event):
    global _shutdown_event
    _shutdown_event = event


async def check_jobs(state: Dict):
    jobs = await fetch_jobs()

    for job in jobs:
        if job["id"] not in state["known_jobs"]:
            save_known_job(job)
            state["known_jobs"][job["id"]] = job

            log.info(
                "[NEW_JOB] %s",
                job["title"]
            )


async def scan_loop(state: Dict):
    while not _shutdown_event.is_set():

        delay = 3 if is_peak_time() else 10

        try:
            await check_jobs(state)

        except asyncio.CancelledError:
            raise

        except Exception:
            log.exception(
                "[SCAN_ERROR]"
            )

        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=delay
            )

        except asyncio.TimeoutError:
            pass
