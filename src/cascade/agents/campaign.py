from google.adk.agents.context import Context
from google.adk.workflow import START, Workflow, node

from cascade.agents.definitions import intake_agent, planner_agent
from cascade.agents.nodes import (
    announce_intake_on_card,
    announce_job_submitted_on_card,
    archive_target_structure,
    ask_scientist_for_clarification,
    await_job_completion,
    complete_campaign_on_card,
    deterministic_run_id,
    escalate_failed_job_on_card,
    executor_for_workload,
    fetch_card_and_attachments,
    load_campaign_state,
    prepare_ligand_library,
    record_job_outcome,
    report_unsupported_executor_on_card,
    submit_workload_job,
    unsupported_stage_rationale,
    unusable_library_questions,
)
from cascade.agents.schemas import (
    CampaignIntent,
    CampaignOutcome,
    CampaignState,
    CardInputs,
    CardTrigger,
    IntakeAnnouncement,
    JobLaunch,
    JobOutcome,
    JobResult,
    JobSubmission,
    LigandLibrary,
    LigandRequest,
    PlanRequest,
    UnsupportedExecutor,
    WorkloadPlan,
)
from cascade.schemas import TargetRequest

DEFAULT_STAGE = "dock"
FIRST_ATTEMPT = 1


def missing_campaign_inputs(intent: CampaignIntent) -> list[str]:
    missing = []
    if not intent.target_source or not intent.target_reference:
        missing.append("Which protein structure should CASCADE screen against?")
    if not intent.ligand_source or not intent.ligand_reference:
        missing.append("Which compounds should CASCADE screen?")
    return missing


@node(rerun_on_resume=True)
async def run_campaign(ctx: Context, node_input: CardTrigger) -> CampaignOutcome:
    state = CampaignState.model_validate(
        await ctx.run_node(load_campaign_state, node_input, run_id="load-state")
    )
    if state.is_complete:
        return CampaignOutcome(
            status="already_complete",
            campaign_id=node_input.campaign_id,
            card_id=node_input.card_id,
            rationale=(
                f"Run {state.completed_run_id} already completed the "
                f"{state.completed_workload} stage for this card."
            ),
        )

    action_id = node_input.action_id

    card = CardInputs.model_validate(
        await ctx.run_node(fetch_card_and_attachments, node_input, run_id=f"fetch-card-{action_id}")
    )
    intent = CampaignIntent.model_validate(
        await ctx.run_node(intake_agent, card, run_id=f"intake-{action_id}")
    )

    blocking = intent.ambiguities + missing_campaign_inputs(intent)
    if blocking:
        intent = intent.model_copy(update={"ambiguities": blocking})
        await ctx.run_node(
            ask_scientist_for_clarification,
            IntakeAnnouncement(card_id=node_input.card_id, intent=intent),
            run_id=f"clarify-{action_id}",
        )
        return CampaignOutcome(
            status="needs_clarification",
            campaign_id=node_input.campaign_id,
            card_id=node_input.card_id,
            target_name=intent.target_name,
            rationale=intent.rationale,
        )

    await ctx.run_node(
        announce_intake_on_card,
        IntakeAnnouncement(card_id=node_input.card_id, intent=intent),
        run_id=f"announce-intake-{action_id}",
    )

    stage = intent.requested_stages[0] if intent.requested_stages else DEFAULT_STAGE
    run_id = deterministic_run_id(node_input.card_id, stage)

    library = LigandLibrary.model_validate(
        await ctx.run_node(
            prepare_ligand_library,
            LigandRequest(
                run_id=run_id,
                card_id=node_input.card_id,
                source=intent.ligand_source,
                reference=intent.ligand_reference,
                card_description=card.description,
            ),
            run_id=f"ligands-{stage}-{action_id}",
        )
    )
    library_problems = unusable_library_questions(intent, library)
    if library_problems:
        await ctx.run_node(
            ask_scientist_for_clarification,
            IntakeAnnouncement(
                card_id=node_input.card_id,
                intent=intent.model_copy(update={"ambiguities": library_problems}),
            ),
            run_id=f"clarify-library-{stage}-{action_id}",
        )
        return CampaignOutcome(
            status="needs_clarification",
            campaign_id=node_input.campaign_id,
            card_id=node_input.card_id,
            target_name=intent.target_name,
            rationale=library_problems[0],
        )

    target = await ctx.run_node(
        archive_target_structure,
        TargetRequest(
            run_id=run_id,
            card_id=node_input.card_id,
            source=intent.target_source,
            reference=intent.target_reference,
        ),
        run_id=f"target-{stage}",
    )

    plan = WorkloadPlan.model_validate(
        await ctx.run_node(
            planner_agent,
            PlanRequest(
                intent=intent,
                stage=stage,
                compound_count=library.compound_count,
                target_has_cocrystal_ligand=intent.target_source == "rcsb",
            ),
            run_id=f"plan-{stage}",
        )
    )
    unsupported = unsupported_stage_rationale(stage, library.compound_count)
    if unsupported is not None:
        return CampaignOutcome.model_validate(
            await ctx.run_node(
                report_unsupported_executor_on_card,
                UnsupportedExecutor(
                    campaign_id=node_input.campaign_id,
                    card_id=node_input.card_id,
                    rationale=unsupported,
                ),
                run_id=f"unsupported-{stage}",
            )
        )

    submission = JobSubmission.model_validate(
        await ctx.run_node(
            submit_workload_job,
            JobLaunch(
                campaign_id=node_input.campaign_id,
                card_id=node_input.card_id,
                run_id=run_id,
                workload=stage,
                attempt=FIRST_ATTEMPT,
                plan=plan,
                target=target,
                library=library,
                executor=executor_for_workload(stage, library.compound_count),
            ),
            run_id=f"submit-{stage}-{FIRST_ATTEMPT}",
        )
    )
    await ctx.run_node(
        announce_job_submitted_on_card,
        submission,
        run_id=f"announce-submit-{stage}-{FIRST_ATTEMPT}",
    )

    outcome = JobOutcome.model_validate(
        await ctx.run_node(
            await_job_completion, submission, run_id=f"await-{stage}-{FIRST_ATTEMPT}"
        )
    )
    result = JobResult(submission=submission, outcome=outcome)
    await ctx.run_node(record_job_outcome, result, run_id=f"record-{stage}-{FIRST_ATTEMPT}")

    if outcome.status != "succeeded":
        return CampaignOutcome.model_validate(
            await ctx.run_node(escalate_failed_job_on_card, result, run_id=f"escalate-{stage}")
        )

    return CampaignOutcome.model_validate(
        await ctx.run_node(complete_campaign_on_card, result, run_id=f"complete-{stage}")
    )


cascade_campaign = Workflow(name="cascade_campaign", edges=[(START, run_campaign)])
