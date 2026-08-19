from google.adk.agents.context import Context
from google.adk.workflow import START, Workflow, node

from cascade.agents.definitions import intake_agent
from cascade.agents.nodes import (
    announce_intake_on_card,
    ask_scientist_for_clarification,
    fetch_card_and_attachments,
)
from cascade.agents.schemas import (
    CampaignIntent,
    CampaignOutcome,
    CardTrigger,
    IntakeAnnouncement,
)


@node(rerun_on_resume=True)
async def run_campaign(ctx: Context, node_input: CardTrigger) -> CampaignOutcome:
    inputs = await ctx.run_node(fetch_card_and_attachments, node_input, run_id="fetch-card")
    intent = CampaignIntent.model_validate(
        await ctx.run_node(intake_agent, inputs, run_id="intake")
    )
    announcement = IntakeAnnouncement(card_id=node_input.card_id, intent=intent)
    if intent.ambiguities:
        await ctx.run_node(ask_scientist_for_clarification, announcement, run_id="clarify")
    else:
        await ctx.run_node(announce_intake_on_card, announcement, run_id="announce-intake")
    return CampaignOutcome(
        status="needs_clarification" if intent.ambiguities else "acknowledged",
        campaign_id=node_input.campaign_id,
        card_id=node_input.card_id,
        target_name=intent.target_name,
        rationale=intent.rationale,
    )


cascade_campaign = Workflow(name="cascade_campaign", edges=[(START, run_campaign)])
