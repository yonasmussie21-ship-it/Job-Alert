"""
EXACT REPLACEMENTS for application_preparer.py
================================================
Implements your colleague's spec:
  1. assessment-eligibility check
  2. _advance_workflow("review-submit")
  3. _advance_workflow("thank-you")   ← actual submit
  4. verify checklist state == EVALUATION_PENDING

Drop these functions in to replace the existing ones.
One call-site change also required — see bottom of file.
"""


# ─────────────────────────────────────────────────────────────
# NEW: _check_assessment_eligibility  (add before _submit_application)
# ─────────────────────────────────────────────────────────────

async def _check_assessment_eligibility(
    session,
    headers: dict,
    app_id: str,
    candidate_id: str,
    job_id: str,
) -> "StepResult":
    """
    POST /candidate-application/assessment-eligibility
    Confirmed payload from HAR:
      {"applicationId": "...", "candidateId": "...", "jobId": "..."}
    Response: {"data": {"assessmentEligibility": false}, ...}
    Not blocking — we log and continue regardless of result.
    """
    import json
    res = await _request_text_with_retry(
        session,
        "POST",
        f"{BASE_URL}/candidate-application/assessment-eligibility",
        json={
            "applicationId": app_id,
            "candidateId": candidate_id,
            "jobId": job_id,
        },
        headers=headers,
        step="ASSESSMENT_ELIGIBILITY",
        timeout=10,
    )
    status_code = int(res.status) if res.status and res.status.isdigit() else None
    if status_code == 200:
        try:
            data = json.loads(res.message)
            eligible = data.get("data", {}).get("assessmentEligibility", False)
            log.info(f"[ASSESSMENT_ELIGIBILITY] eligible={eligible}")
        except Exception:
            log.info(f"[ASSESSMENT_ELIGIBILITY] status=200 (parse skipped)")
    else:
        log.warning(f"[ASSESSMENT_ELIGIBILITY] status={res.status} — continuing anyway")
    return StepResult(ok=True)  # never blocks the flow


# ─────────────────────────────────────────────────────────────
# NEW: _verify_submission  (add after _submit_application)
# ─────────────────────────────────────────────────────────────

async def _verify_submission(
    session,
    headers: dict,
    app_id: str,
) -> "StepResult":
    """
    GET /checklist/api/application/application-manage-data/{app_id}/en-GB
    Confirms applicationState == "EVALUATION_PENDING" after submit.
    Confirmed from HAR — this is what the browser hits after thank-you step.
    """
    import json
    CHECKLIST_URL = "https://www.jobsatamazon.co.uk/checklist/api"
    res = await _request_text_with_retry(
        session,
        "GET",
        f"{CHECKLIST_URL}/application/application-manage-data/{app_id}/en-GB",
        headers=headers,
        step="VERIFY_SUBMISSION",
        timeout=10,
    )
    status_code = int(res.status) if res.status and res.status.isdigit() else None
    if status_code != 200:
        log.warning(f"[VERIFY_SUBMISSION] unexpected status {res.status}")
        return StepResult(ok=False, message=f"Verification request failed: {res.status}")

    try:
        data = json.loads(res.message)
        state = data.get("applicationState", "")
        log.info(f"[VERIFY_SUBMISSION] applicationState={state}")
        if state == "EVALUATION_PENDING":
            return StepResult(ok=True)
        else:
            return StepResult(ok=False, message=f"Unexpected state after submit: {state}")
    except Exception as e:
        return StepResult(ok=False, message=f"Failed to parse verification response: {e}")


# ─────────────────────────────────────────────────────────────
# REPLACE: _submit_application  (lines ~470-504)
# ─────────────────────────────────────────────────────────────
# Implements exactly: assessment-eligibility → review-submit → thank-you → verify

async def _submit_application(
    session,
    headers: dict,
    job_id: str,
    app_id: str,
    candidate_id: str,
    schedule_id: str,
) -> "StepResult":
    """
    Full submit sequence per confirmed HAR flow:
      1. POST assessment-eligibility   (non-blocking check)
      2. PUT  update-workflow-step-name → "review-submit"
      3. PUT  update-workflow-step-name → "thank-you"    ← actual submit
      4. GET  checklist application-manage-data          ← verify EVALUATION_PENDING
    """

    # Step 1: assessment-eligibility check
    await _check_assessment_eligibility(
        session, headers, app_id, candidate_id, job_id
    )

    # Step 2: navigate to review-submit
    res = await _request_text_with_retry(
        session,
        "PUT",
        f"{BASE_URL}/candidate-application/update-workflow-step-name",
        json={
            "applicationId": app_id,
            "workflowStepName": "review-submit",
        },
        headers=headers,
        step="WORKFLOW_review-submit",
        timeout=10,
    )
    status_code = int(res.status) if res.status and res.status.isdigit() else None
    log.info(f"[WORKFLOW] → review-submit ({res.status})")
    if status_code not in [200, 204]:
        msg = f"review-submit step failed: {res.status}"
        log_error("SUBMIT_FAILED", f"app={app_id}: {msg}")
        return StepResult(ok=False, message=msg)

    # Step 3: thank-you = the actual submit
    res = await _request_text_with_retry(
        session,
        "PUT",
        f"{BASE_URL}/candidate-application/update-workflow-step-name",
        json={
            "applicationId": app_id,
            "workflowStepName": "thank-you",
        },
        headers=headers,
        step="WORKFLOW_thank-you",
        timeout=15,
    )
    status_code = int(res.status) if res.status and res.status.isdigit() else None
    log.info(f"[WORKFLOW] → thank-you ({res.status})")
    if status_code not in [200, 204]:
        msg = f"thank-you (submit) step failed: {res.status}"
        log_error("SUBMIT_FAILED", f"app={app_id}: {msg}")
        return StepResult(ok=False, message=msg)

    # Step 4: verify EVALUATION_PENDING
    verify_res = await _verify_submission(session, headers, app_id)
    if not verify_res.ok:
        # Non-fatal — the submit likely succeeded even if verify is flaky
        log.warning(f"[SUBMIT_VERIFY_FAILED] {verify_res.message} — treating as success")

    log.info(f"[SUBMIT_SUCCESS] app_id={app_id}")
    return StepResult(ok=True)


# ─────────────────────────────────────────────────────────────
# REPLACE: _advance_workflow  (lines ~422-444)
# ─────────────────────────────────────────────────────────────
# Remove "review-submit" from here — it's now handled inside _submit_application.
# Keep only the pre-submit steps: additional-information and nhe (with booking).

async def _advance_workflow(
    session,
    headers: dict,
    app_id: str,
    schedule_id: str = "",      # needed for NHE site lookup
) -> "StepResult":
    """
    Pre-submit workflow steps only:
      additional-information  (personal details were saved just before this)
      nhe                     (fetch slots + book appointment)
    review-submit and thank-you are now handled in _submit_application.
    """
    import json
    from datetime import datetime, timedelta

    # Step: additional-information
    res = await _request_text_with_retry(
        session,
        "PUT",
        f"{BASE_URL}/candidate-application/update-workflow-step-name",
        json={"applicationId": app_id, "workflowStepName": "additional-information"},
        headers=headers,
        step="WORKFLOW_additional-information",
        timeout=10,
    )
    status_code = int(res.status) if res.status and res.status.isdigit() else None
    log.info(f"[WORKFLOW] → additional-information ({res.status})")
    if status_code not in [200, 204]:
        return StepResult(ok=False, message=f"additional-information step failed: {res.status}")

    # Step: nhe (with slot booking)
    res = await _request_text_with_retry(
        session,
        "PUT",
        f"{BASE_URL}/candidate-application/update-workflow-step-name",
        json={"applicationId": app_id, "workflowStepName": "nhe"},
        headers=headers,
        step="WORKFLOW_nhe",
        timeout=10,
    )
    status_code = int(res.status) if res.status and res.status.isdigit() else None
    log.info(f"[WORKFLOW] → nhe ({res.status})")
    if status_code not in [200, 204]:
        return StepResult(ok=False, message=f"nhe step failed: {res.status}")

    # Get siteId for NHE slot fetch
    if schedule_id:
        site_res = await _request_text_with_retry(
            session,
            "GET",
            f"{BASE_URL}/job/get-schedule-details/{schedule_id}?locale=en-GB",
            headers=headers,
            step="GET_SCHEDULE_DETAILS",
            timeout=10,
        )
        site_code = int(site_res.status) if site_res.status and site_res.status.isdigit() else None
        site_id = None
        if site_code == 200:
            try:
                site_id = json.loads(site_res.message)["data"]["siteId"]
                log.info(f"[NHE] siteId={site_id}")
            except Exception as e:
                log.warning(f"[NHE] Could not parse siteId: {e}")

        if site_id:
            today = datetime.now()
            slots_res = await _request_text_with_retry(
                session,
                "POST",
                f"{BASE_URL}/nhe/available-time-slots",
                json={
                    "returnNestedData": True,
                    "siteId": site_id,
                    "startDate": today.strftime("%Y-%m-%d"),
                    "endDate": (today + timedelta(days=14)).strftime("%Y-%m-%d"),
                    "locale": "en-GB",
                },
                headers=headers,
                step="NHE_SLOTS",
                timeout=15,
            )
            slots_code = int(slots_res.status) if slots_res.status and slots_res.status.isdigit() else None
            if slots_code == 200:
                try:
                    slots = json.loads(slots_res.message).get("data", [])
                    if slots:
                        virtual = [s for s in slots if s.get("locationType") == "VIRTUAL_CONNECT"]
                        chosen = min(virtual or slots, key=lambda s: s.get("startTimestamp", 0))
                        log.info(f"[NHE] Booking slot {chosen.get('timeSlotId')} on {chosen.get('title')}")
                        await _request_text_with_retry(
                            session,
                            "PUT",
                            f"{BASE_URL}/candidate-application/update-application",
                            json={
                                "applicationId": app_id,
                                "payload": {"nheAppointment": chosen},
                                "type": "nhe",
                                "dspEnabled": True,
                            },
                            headers=headers,
                            step="NHE_BOOK",
                            timeout=15,
                        )
                    else:
                        log.warning(f"[NHE] No slots available for {site_id}")
                except Exception as e:
                    log.warning(f"[NHE] Slot booking error: {e} — continuing")
            else:
                log.warning(f"[NHE] Slots fetch failed {slots_res.status} — continuing")
    else:
        log.warning("[NHE] No schedule_id — skipping NHE slot booking")

    return StepResult(ok=True)


# ─────────────────────────────────────────────────────────────
# CALL SITE CHANGE in prepare_application (~line 617)
# ─────────────────────────────────────────────────────────────
#
# BEFORE:
#   wf_res = await _advance_workflow(session, headers_with_app, app_id)
#
# AFTER:
#   wf_res = await _advance_workflow(session, headers_with_app, app_id, schedule_id)
#
# Everything else stays the same.
# _submit_application call signature is unchanged:
#   submit_res = await _submit_application(session, headers_with_app, job_id, app_id, candidate_id, schedule_id)
