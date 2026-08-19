import httpx
from google.adk.agents.context import Context
from google.adk.workflow import RetryConfig, node

from cascade.agents.schemas import CampaignIntent, CardInputs, CardTrigger, IntakeAnnouncement
from cascade.clients import trello
from cascade.config import get_settings

settings = get_settings()

FETCH_RETRY = RetryConfig(max_attempts=3, backoff_factor=2.0, exceptions=[httpx.HTTPError])


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def fetch_card_and_attachments(ctx: Context, node_input: CardTrigger) -> CardInputs:
    card = await trello.get_card(node_input.card_id)
    attachments = await trello.get_attachments(node_input.card_id)
    return CardInputs(
        card_id=node_input.card_id,
        title=card.get("name", ""),
        description=card.get("desc", ""),
        attachment_names=[a.get("name", "") for a in attachments],
    )


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def announce_intake_on_card(ctx: Context, node_input: IntakeAnnouncement) -> CampaignIntent:
    intent = node_input.intent
    stages = ", ".join(intent.requested_stages) or "none identified"
    await trello.add_comment(
        node_input.card_id,
        f"**CASCADE picked this up.**\n\n"
        f"Target: {intent.target_name or 'not stated'}\n"
        f"Planned stages: {stages}\n\n{intent.rationale}",
    )
    await trello.move_card(node_input.card_id, settings.trello_list_in_progress)
    return intent


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def ask_scientist_for_clarification(
    ctx: Context, node_input: IntakeAnnouncement
) -> CampaignIntent:
    intent = node_input.intent
    questions = "\n".join(f"- {item}" for item in intent.ambiguities)
    await trello.add_comment(
        node_input.card_id,
        f"**CASCADE needs a clarification before starting.**\n\n{intent.rationale}\n\n"
        f"**Open questions**\n{questions}\n\n"
        f"Answer on this card and drag it back to To Do to resume.",
    )
    await trello.move_card(node_input.card_id, settings.trello_list_needs_attention)
    return intent
