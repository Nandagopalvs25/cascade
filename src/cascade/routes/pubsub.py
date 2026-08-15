import base64
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from cascade.security import verify_pubsub_oidc

router = APIRouter(tags=["pubsub"], dependencies=[Depends(verify_pubsub_oidc)])


def _decode_envelope(envelope: dict) -> dict:
    try:
        data = base64.b64decode(envelope["message"]["data"]).decode("utf-8")
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Malformed Pub/Sub envelope") from exc
    return json.loads(data)


@router.post("/card-events")
async def handle_card_event(request: Request):
    payload = _decode_envelope(await request.json())
    return {"status": "accepted", "card_id": payload.get("card_id")}


@router.post("/job-completions")
async def handle_job_completion(request: Request):
    payload = _decode_envelope(await request.json())
    return {"status": "accepted", "run_id": payload.get("run_id")}
