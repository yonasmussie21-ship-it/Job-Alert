"""
@Amazonjobs100_bot — CONFIRMED Submit Flow
==========================================
From HAR12/HAR13 analysis — the submit is NOT a separate endpoint!

CONFIRMED FLOW:
  State: CONTINGENT_OFFER_ACCEPTED
  → PUT update-workflow-step-name → "additional-information"
  → PUT update-application (type: "additional-information", payload: {candidate: {...}})
  → State: ADDITIONAL_BACKGROUND_INFO_SAVED
  → PUT update-workflow-step-name → "nhe"
  → PUT update-application (type: "nhe", payload: {nheAppointment: {...}})
  → State: NHE_APPOINTMENT_SELECTED
  → POST assessment-eligibility (returns assessmentEligibility: false)
  → PUT update-workflow-step-name → "review-submit"
  → PUT update-workflow-step-name → "thank-you"  ← THIS IS THE SUBMIT!
  → State: APPLICATION_SUBMITTED ✅
  → submitted: true ✅

KEY INSIGHT: There is no separate submit-application endpoint.
Submitting = updating workflow step to "thank-you"
"""

import aiohttp
import asyncio
import aiosqlite
import json
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("submit_flow")

BASE_URL = "https://www.jobsatamazon.co.uk"
GRAPHQL_URL = f"{BASE_URL}/candidate/graphql"
DB_PATH = "/tmp/amazonjobs.db"

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
    "bb-ui-version": "bb-ui-v2",
}


# ─────────────────────────────────────────────────────────
# CORE: Update Workflow Step
# ─────────────────────────────────────────────────────────

async def update_workflow_step(
    session: aiohttp.ClientSession,
    application_id: str,
    step_name: str,
    csrf_token: str,
    cookies: dict
) -> dict:
    """
    PUT /update-workflow-step-name
    Confirmed steps: additional-information, nhe, review-submit, thank-you
    """
    async with session.put(
        f"{BASE_URL}/application/api/candidate-application"
        f"/update-workflow-step-name",
        headers={
            **HEADERS_BASE,
            "Content-Type": "application/json;charset=UTF-8",
            "x-csrf-token": csrf_token,
        },
        json={
            "applicationId": application_id,
            "workflowStepName": step_name
        },
        cookies=cookies
    ) as resp:
        status = resp.status
        data = await resp.json()
        current_state = (
            data.get("data", {}).get("currentState", "")
        )
        submitted = data.get("data", {}).get("submitted", False)
        logger.info(
            f"Workflow step '{step_name}': "
            f"HTTP={status} "
            f"state={current_state} "
            f"submitted={submitted}"
        )
        return data.get("data", {})


# ─────────────────────────────────────────────────────────
# CORE: Check Assessment Eligibility
# ─────────────────────────────────────────────────────────

async def check_assessment_eligibility(
    session: aiohttp.ClientSession,
    application_id: str,
    candidate_id: str,
    job_id: str,
    csrf_token: str,
    cookies: dict
) -> bool:
    """
    POST /assessment-eligibility
    Confirmed response: {"assessmentEligibility": false}
    Must be called before review-submit step.
    """
    async with session.post(
        f"{BASE_URL}/application/api/candidate-application"
        f"/assessment-eligibility",
        headers={
            **HEADERS_BASE,
            "Content-Type": "application/json;charset=UTF-8",
            "x-csrf-token": csrf_token,
        },
        json={
            "applicationId": application_id,
            "candidateId": candidate_id,
            "jobId": job_id
        },
        cookies=cookies
    ) as resp:
        data = await resp.json()
        eligible = (
            data.get("data", {})
            .get("assessmentEligibility", False)
        )
        logger.info(f"Assessment eligibility: {eligible}")
        return eligible


# ─────────────────────────────────────────────────────────
# CORE: Get Application State
# ─────────────────────────────────────────────────────────

async def get_application_state(
    session: aiohttp.ClientSession,
    application_id: str,
    cookies: dict
) -> dict:
    """Get current application state"""
    async with session.get(
        f"{BASE_URL}/application/api/candidate-application"
        f"/applications/{application_id}",
        headers=HEADERS_BASE,
        cookies=cookies
    ) as resp:
        data = await resp.json()
        return data.get("data", {})


# ─────────────────────────────────────────────────────────
# MAIN SUBMIT FUNCTION — Confirmed Flow
# ─────────────────────────────────────────────────────────

async def submit_application(
    session: aiohttp.ClientSession,
    application_id: str,
    candidate_id: str,
    job_id: str,
    csrf_token: str,
    cookies: dict
) -> dict:
    """
    Submit application using confirmed flow from HAR12/HAR13.

    The "submit" is simply updating workflow step to "thank-you".
    No separate submit-application endpoint needed!

    Returns:
        {
            "success": True/False,
            "state_before": "NHE_APPOINTMENT_SELECTED",
            "state_after": "APPLICATION_SUBMITTED",
            "submitted": True,
            "step": "thank-you"
        }
    """
    # Step 1: Capture BEFORE state
    before = await get_application_state(
        session, application_id, cookies
    )
    state_before = before.get("currentState", "")
    submitted_before = before.get("submitted", False)

    logger.info(
        f"BEFORE: state={state_before} "
        f"submitted={submitted_before}"
    )

    if submitted_before:
        logger.info("Already submitted — skipping")
        return {
            "success": True,
            "already_submitted": True,
            "state_before": state_before,
            "state_after": state_before,
            "submitted": True
        }

    await asyncio.sleep(0.5)

    # Step 2: Check assessment eligibility
    await check_assessment_eligibility(
        session, application_id,
        candidate_id, job_id,
        csrf_token, cookies
    )
    await asyncio.sleep(0.5)

    # Step 3: Move to review-submit
    await update_workflow_step(
        session, application_id,
        "review-submit", csrf_token, cookies
    )
    await asyncio.sleep(0.8)

    # Step 4: THE SUBMIT — move to thank-you
    logger.info(f"🚀 SUBMITTING: {application_id}")
    after_data = await update_workflow_step(
        session, application_id,
        "thank-you", csrf_token, cookies
    )
    await asyncio.sleep(1)

    # Step 5: Verify
    after = await get_application_state(
        session, application_id, cookies
    )
    state_after = after.get("currentState", "")
    submitted_after = after.get("submitted", False)

    success = (
        submitted_after is True
        or state_after == "APPLICATION_SUBMITTED"
        or after_data.get("workflowStepName") == "thank-you"
    )

    logger.info(
        f"AFTER: state={state_after} "
        f"submitted={submitted_after} "
        f"success={success}"
    )

    return {
        "success": success,
        "state_before": state_before,
        "state_after": state_after,
        "submitted": submitted_after,
        "step": after_data.get("workflowStepName", ""),
    }


# ─────────────────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────────────────

async def save_audit_log(log: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS submit_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                chat_id TEXT,
                candidate_id TEXT,
                application_id TEXT,
                job_id TEXT,
                job_title TEXT,
                city TEXT,
                pay REAL,
                state_before TEXT,
                state_after TEXT,
                submitted INTEGER,
                success INTEGER,
                error TEXT
            )
        """)
        await db.execute("""
            INSERT INTO submit_audit (
                timestamp, chat_id, candidate_id,
                application_id, job_id, job_title,
                city, pay, state_before, state_after,
                submitted, success, error
            ) VALUES (
                :timestamp, :chat_id, :candidate_id,
                :application_id, :job_id, :job_title,
                :city, :pay, :state_before, :state_after,
                :submitted, :success, :error
            )
        """, log)
        await db.commit()


# ─────────────────────────────────────────────────────────
# BOT INTEGRATION
# ─────────────────────────────────────────────────────────

async def run_submit_for_subscriber(
    bot,
    chat_id: str,
    subscriber: dict,
    application_details: dict
) -> bool:
    """
    Run confirmed submit flow for a subscriber.

    application_details = {
        "applicationId": "...",
        "candidateId": "...",
        "jobId": "...",
        "title": "Warehouse Operative",
        "city": "Exeter",
        "pay": 14.30
    }
    """
    cookies = subscriber.get("amazon_cookies", {})
    csrf_token = subscriber.get("csrf_token", "")

    await bot.send_message(
        chat_id=int(chat_id),
        text=(
            f"⚡ Submitting application...\n"
            f"📦 {application_details.get('title')}\n"
            f"📍 {application_details.get('city')}\n"
            f"💰 £{application_details.get('pay')}/hr"
        )
    )

    try:
        async with aiohttp.ClientSession() as session:
            result = await submit_application(
                session=session,
                application_id=application_details["applicationId"],
                candidate_id=application_details["candidateId"],
                job_id=application_details["jobId"],
                csrf_token=csrf_token,
                cookies=cookies
            )

        # Save audit log
        await save_audit_log({
            "timestamp": datetime.now().isoformat(),
            "chat_id": chat_id,
            "candidate_id": application_details["candidateId"],
            "application_id": application_details["applicationId"],
            "job_id": application_details["jobId"],
            "job_title": application_details.get("title"),
            "city": application_details.get("city"),
            "pay": application_details.get("pay"),
            "state_before": result.get("state_before"),
            "state_after": result.get("state_after"),
            "submitted": int(result.get("submitted", False)),
            "success": int(result.get("success", False)),
            "error": None
        })

        if result["success"]:
            await bot.send_message(
                chat_id=int(chat_id),
                text=(
                    f"✅ *Application Submitted!*\n\n"
                    f"📦 {application_details.get('title')}\n"
                    f"📍 {application_details.get('city')}\n"
                    f"💰 £{application_details.get('pay')}/hr\n\n"
                    f"📊 Status: `APPLICATION_SUBMITTED`\n"
                    f"📝 Step: `thank-you`\n\n"
                    f"Check jobsatamazon.co.uk for next steps!"
                ),
                parse_mode="Markdown"
            )
            return True
        else:
            await bot.send_message(
                chat_id=int(chat_id),
                text=(
                    f"❌ Submit failed for "
                    f"{application_details.get('title')}\n"
                    f"State: {result.get('state_after')}\n\n"
                    f"Please apply manually at jobsatamazon.co.uk"
                )
            )
            return False

    except Exception as e:
        logger.error(f"Submit error: {e}")
        await bot.send_message(
            chat_id=int(chat_id),
            text=f"❌ Error: {str(e)}\n\nPlease apply manually."
        )
        await save_audit_log({
            "timestamp": datetime.now().isoformat(),
            "chat_id": chat_id,
            "candidate_id": application_details.get("candidateId"),
            "application_id": application_details.get("applicationId"),
            "job_id": application_details.get("jobId"),
            "job_title": application_details.get("title"),
            "city": application_details.get("city"),
            "pay": application_details.get("pay"),
            "state_before": "UNKNOWN",
            "state_after": "ERROR",
            "submitted": 0,
            "success": 0,
            "error": str(e)
        })
        return False
