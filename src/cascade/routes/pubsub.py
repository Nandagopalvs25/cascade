import base64
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from cascade.agents.runtime import resume_campaign_on_job_completion, start_campaign_for_card
from cascade.dependencies import DbDep, RunnerDep, SettingsDep
from cascade.security import verify_pubsub_oidc

router = APIRouter(prefix="/pubsub", tags=["pubsub"], dependencies=[Depends(verify_pubsub_oidc)])

LOGGER = logging.getLogger("cascade.pubsub")


def _decode_pubsub_message_payload(envelope: dict) -> dict:
    try:
        return json.loads(base64.b64decode(envelope["message"]["data"]).decode("utf-8"))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Malformed Pub/Sub envelope") from exc


@router.post("/card-events")
async def start_campaign_on_card_event(
    request: Request, runner: RunnerDep, settings: SettingsDep, db: DbDep
):
    started = time.perf_counter()
    envelope = await request.json()
    payload = _decode_pubsub_message_payload(envelope)
    card_id = payload.get("card_id")
    action_id = payload.get("action_id")
    if not card_id or not action_id:
        raise HTTPException(status_code=400, detail="Card event needs card_id and action_id")

    message_id = (envelope.get("message") or {}).get("messageId")
    delivery_attempt = envelope.get("deliveryAttempt")
    LOGGER.info(
        "pubsub push delivered a card event, starting the campaign in this container",
        extra={
            "card_id": card_id,
            "action_id": action_id,
            "kind": payload.get("kind"),
            "pubsub_message_id": message_id,
            "delivery_attempt": delivery_attempt,
        },
    )

    outcome = await start_campaign_for_card(runner, settings, db, card_id, action_id)

    LOGGER.info(
        "campaign invocation finished, the workflow is now suspended or complete",
        extra={
            "card_id": card_id,
            "action_id": action_id,
            "pubsub_message_id": message_id,
            "outcome_status": outcome.status if outcome else "suspended_awaiting_job",
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        },
    )

    if outcome is None:
        return {"status": "started", "card_id": card_id}
    return {"status": outcome.status, "card_id": card_id, "campaign_id": outcome.campaign_id}


@router.post("/job-completions")
async def resume_campaign_on_completion_event(
    request: Request, runner: RunnerDep, settings: SettingsDep, db: DbDep
):
    started = time.perf_counter()
    payload = _decode_pubsub_message_payload(await request.json())
    run_id = payload.get("run_id")
    if not run_id:
        raise HTTPException(status_code=400, detail="Completion event needs run_id")

    LOGGER.info(
        "pubsub push delivered a job completion, resuming the suspended campaign here",
        extra={
            "run_id": run_id,
            "workload": payload.get("workload"),
            "job_status": payload.get("status"),
        },
    )

    status = await resume_campaign_on_job_completion(runner, settings, db, payload)

    LOGGER.info(
        "campaign resume finished",
        extra={
            "run_id": run_id,
            "resume_status": status,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
        },
    )
    return {"status": status, "run_id": run_id}
