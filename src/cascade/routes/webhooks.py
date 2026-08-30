import json
import logging
import time

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from cascade.clients import pubsub, trello
from cascade.dependencies import DbDep, SettingsDep
from cascade.models import CardEvent

router = APIRouter(prefix="/webhooks", tags=["trello"])

LOGGER = logging.getLogger("cascade.webhooks")


@router.head("/trello")
async def trello_head():
    return Response(status_code=200)


@router.post("/trello")
async def trello_webhook(request: Request, db: DbDep, settings: SettingsDep):
    started = time.perf_counter()

    def elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    if request.headers.get("x-trello-client-identifier") == "cascade-agent":
        LOGGER.info(
            "trello webhook ignored: cascade's own board change, suppressed to avoid a loop",
            extra={"decision": "suppressed_own_change", "elapsed_ms": elapsed_ms()},
        )
        return Response(status_code=200)

    raw_body = await request.body()
    signature = request.headers.get("x-trello-webhook", "")
    if not trello.verify_signature(
        raw_body, settings.trello_callback_url, settings.trello_api_secret, signature
    ):
        LOGGER.warning(
            "trello webhook rejected: signature did not verify",
            extra={"decision": "bad_signature", "elapsed_ms": elapsed_ms()},
        )
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
        LOGGER.info(
            "trello webhook ignored: action does not put a card into a list",
            extra={
                "decision": "not_a_list_placement",
                "action_type": action_type,
                "elapsed_ms": elapsed_ms(),
            },
        )
        return Response(status_code=200)

    if target_list_id != settings.trello_list_todo:
        LOGGER.info(
            "trello webhook ignored: card did not land in To Do",
            extra={
                "decision": "not_the_todo_list",
                "action_type": action_type,
                "target_list_id": target_list_id,
                "elapsed_ms": elapsed_ms(),
            },
        )
        return Response(status_code=200)

    action_id = action["id"]
    card_id = data["card"]["id"]
    existing = await db.execute(select(CardEvent).where(CardEvent.trello_action_id == action_id))
    if existing.scalar_one_or_none():
        LOGGER.info(
            "trello webhook ignored: this action id is already recorded",
            extra={
                "decision": "duplicate_action",
                "card_id": card_id,
                "action_id": action_id,
                "elapsed_ms": elapsed_ms(),
            },
        )
        return Response(status_code=200)

    event = CardEvent(trello_action_id=action_id, kind=kind, payload=payload)
    db.add(event)
    await db.commit()

    await pubsub.publish(
        settings.pubsub_card_events_topic,
        {"card_id": card_id, "action_id": action_id, "kind": kind},
    )

    LOGGER.info(
        "card event accepted and published to pubsub, returning 200 without doing the work",
        extra={
            "decision": "published",
            "card_id": card_id,
            "action_id": action_id,
            "kind": kind,
            "topic": settings.pubsub_card_events_topic,
            "elapsed_ms": elapsed_ms(),
        },
    )
    return Response(status_code=200)
