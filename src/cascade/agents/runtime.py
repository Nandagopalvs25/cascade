import uuid
from datetime import UTC, datetime

from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cascade.agents.app import cascade_app
from cascade.agents.schemas import CampaignOutcome, CardTrigger
from cascade.config import Settings
from cascade.models import CardEvent


def build_campaign_runner(settings: Settings) -> Runner:
    return Runner(
        app=cascade_app,
        session_service=DatabaseSessionService(db_url=settings.database_url),
    )


async def card_event_already_processed(db: AsyncSession, action_id: str) -> bool:
    result = await db.execute(
        select(CardEvent.processed_at).where(CardEvent.trello_action_id == action_id)
    )
    processed_at = result.scalar_one_or_none()
    return processed_at is not None


async def mark_card_event_processed(db: AsyncSession, action_id: str) -> None:
    await db.execute(
        update(CardEvent)
        .where(CardEvent.trello_action_id == action_id)
        .values(processed_at=datetime.now(UTC))
    )
    await db.commit()


async def start_campaign_for_card(
    runner: Runner, settings: Settings, db: AsyncSession, card_id: str, action_id: str
) -> CampaignOutcome | None:
    campaign_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cascade:card:{card_id}"))

    if await card_event_already_processed(db, action_id):
        return CampaignOutcome(
            status="duplicate",
            campaign_id=campaign_id,
            card_id=card_id,
            rationale="This Trello action has already been processed.",
        )

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=settings.adk_user_id, session_id=campaign_id
    )
    if session is None:
        await runner.session_service.create_session(
            app_name=runner.app_name,
            user_id=settings.adk_user_id,
            session_id=campaign_id,
        )

    trigger = CardTrigger(campaign_id=campaign_id, card_id=card_id, action_id=action_id)
    outcome = None
    async for event in runner.run_async(
        user_id=settings.adk_user_id,
        session_id=campaign_id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=trigger.model_dump_json())]
        ),
        run_config=RunConfig(max_llm_calls=settings.max_llm_calls_per_invocation),
    ):
        if isinstance(event.output, dict) and "status" in event.output:
            outcome = CampaignOutcome.model_validate(event.output)

    await mark_card_event_processed(db, action_id)
    return outcome
