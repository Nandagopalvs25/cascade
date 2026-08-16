import json

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from cascade.clients import pubsub, trello
from cascade.dependencies import DbDep, SettingsDep
from cascade.models import Event

router = APIRouter(tags=["trello"])


@router.head("/trello")
async def trello_head():
    return Response(status_code=200)


@router.post("/trello")
async def trello_webhook(request: Request, db: DbDep, settings: SettingsDep):
    if request.headers.get("x-trello-client-identifier") == "cascade-agent":
        return Response(status_code=200)

    raw_body = await request.body()
    signature = request.headers.get("x-trello-webhook", "")
    if not trello.verify_signature(
        raw_body, settings.trello_callback_url, settings.trello_api_secret, signature
    ):
        return Response(status_code=401)

    payload = json.loads(raw_body)
    action = payload.get("action", {})
    data = action.get("data", {})

    action_type = action.get("type")
    if action_type == "updateCard" and "listAfter" in data:
        target_list_id = data["listAfter"]["id"]
        kind = "card.moved_to_todo"
    elif action_type == "createCard" and "list" in data:
        target_list_id = data["list"]["id"]
        kind = "card.created_in_todo"
    else:
        return Response(status_code=200)

    if target_list_id != settings.trello_list_todo:
        return Response(status_code=200)

    action_id = action["id"]
    existing = await db.execute(select(Event).where(Event.trello_action_id == action_id))
    if existing.scalar_one_or_none():
        return Response(status_code=200)

    event = Event(trello_action_id=action_id, kind=kind, payload=payload)
    db.add(event)
    await db.commit()

    await pubsub.publish(
        settings.pubsub_card_events_topic,
        {"card_id": data["card"]["id"], "action_id": action_id, "kind": kind},
    )

    return Response(status_code=200)
