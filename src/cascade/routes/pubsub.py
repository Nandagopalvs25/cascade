import base64
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from cascade.agents.runtime import start_campaign_for_card
from cascade.dependencies import DbDep, RunnerDep, SettingsDep
from cascade.security import verify_pubsub_oidc

router = APIRouter(tags=["pubsub"], dependencies=[Depends(verify_pubsub_oidc)])


def _decode_envelope(envelope: dict) -> dict:
    try:
        data = base64.b64decode(envelope["message"]["data"]).decode("utf-8")
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Malformed Pub/Sub envelope") from exc
    return json.loads(data)


@router.post("/card-events")
async def start_campaign_on_card_event(
    request: Request, runner: RunnerDep, settings: SettingsDep, db: DbDep
):
    payload = _decode_envelope(await request.json())
    card_id = payload.get("card_id")
    action_id = payload.get("action_id")
    if not card_id or not action_id:
        raise HTTPException(status_code=400, detail="Card event needs card_id and action_id")

    outcome = await start_campaign_for_card(runner, settings, db, card_id, action_id)
    if outcome is None:
        return {"status": "started", "card_id": card_id}
    return {"status": outcome.status, "card_id": card_id, "campaign_id": outcome.campaign_id}


@router.post("/job-completions")
async def acknowledge_job_completion(request: Request):
    payload = _decode_envelope(await request.json())
    return {"status": "accepted", "run_id": payload.get("run_id")}
