from google.adk.agents.context import Context
from google.adk.workflow import START, Workflow, node

from cascade.agents.card_text import (
    blocked_stages_note,
    parent_run_id_in_card_description,
    unproposed_stage_note,
)
from cascade.agents.compound_library import unusable_library_questions
from cascade.agents.definitions import (
    intake_agent,
    planner_agent,
    proposer_agent,
    triage_agent,
)
from cascade.agents.nodes import (
    announce_control_failure_rerun_on_card,
    announce_intake_on_card,
    announce_job_submitted_on_card,
    apply_triage_verdict,
    archive_target_structure,
    ask_scientist_for_clarification,
    await_job_completion,
    build_proposal_request,
    build_triage_request,
    complete_campaign_on_card,
    create_recommended_card,
    escalate_failed_job_on_card,
    escalate_untrustworthy_run_on_card,
    fetch_card_and_attachments,
    load_campaign_state,
    prepare_ligand_library,
    record_job_outcome,
    report_unsupported_executor_on_card,
    submit_workload_job,
)
from cascade.agents.policy import (
    deterministic_run_id,
    executor_for_workload,
    plan_escalated_after_control_failure,
    unsupported_stage_rationale,
)
from cascade.agents.schemas import (
    CampaignCompletion,
    CampaignIntent,
    CampaignOutcome,
    CampaignState,
    CardInputs,
    CardTrigger,
    CompletedStage,
    IntakeAnnouncement,
    JobLaunch,
    JobOutcome,
    JobResult,
    JobSubmission,
    LigandLibrary,
    LigandRequest,
    PlanRequest,
    ProposalDecision,
    ProposalRequest,
    ProposedFollowup,
    StageProposal,
    TriageDecision,
    TriagedJobResult,
    TriageRequest,
    TriageVerdict,
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
    parent_run_id = parent_run_id_in_card_description(card.description)

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

    attempt = FIRST_ATTEMPT
    while True:
        submission = JobSubmission.model_validate(
            await ctx.run_node(
                submit_workload_job,
                JobLaunch(
                    campaign_id=node_input.campaign_id,
                    card_id=node_input.card_id,
                    run_id=run_id,
                    workload=stage,
                    attempt=attempt,
                    plan=plan,
                    target=target,
                    library=library,
                    executor=executor_for_workload(stage, library.compound_count),
                    parent_run_id=parent_run_id,
                ),
                run_id=f"submit-{stage}-{attempt}",
            )
        )
        await ctx.run_node(
            announce_job_submitted_on_card,
            submission,
            run_id=f"announce-submit-{stage}-{attempt}",
        )

        outcome = JobOutcome.model_validate(
            await ctx.run_node(await_job_completion, submission, run_id=f"await-{stage}-{attempt}")
        )
        result = JobResult(submission=submission, outcome=outcome)
        await ctx.run_node(record_job_outcome, result, run_id=f"record-{stage}-{attempt}")

        if outcome.status != "succeeded":
            return CampaignOutcome.model_validate(
                await ctx.run_node(
                    escalate_failed_job_on_card, result, run_id=f"escalate-{stage}-{attempt}"
                )
            )

        request = TriageRequest.model_validate(
            await ctx.run_node(
                build_triage_request, result, run_id=f"triage-inputs-{stage}-{attempt}"
            )
        )
        verdict = TriageVerdict.model_validate(
            await ctx.run_node(triage_agent, request, run_id=f"triage-{stage}-{attempt}")
        )
        triaged = TriagedJobResult.model_validate(
            await ctx.run_node(
                apply_triage_verdict,
                TriageDecision(result=result, request=request, verdict=verdict),
                run_id=f"triage-verdict-{stage}-{attempt}",
            )
        )

        if triaged.verdict.next_action == "rerun_with_more_effort":
            await ctx.run_node(
                announce_control_failure_rerun_on_card,
                triaged,
                run_id=f"announce-rerun-{stage}-{attempt}",
            )
            plan = plan_escalated_after_control_failure(plan)
            attempt += 1
            continue

        if triaged.verdict.next_action == "escalate_to_scientist":
            return CampaignOutcome.model_validate(
                await ctx.run_node(
                    escalate_untrustworthy_run_on_card,
                    triaged,
                    run_id=f"escalate-triage-{stage}-{attempt}",
                )
            )

        completed = CompletedStage(
            triaged=triaged,
            target_name=intent.target_name,
            target_source=intent.target_source,
            target_reference=intent.target_reference,
        )
        proposal_request = ProposalRequest.model_validate(
            await ctx.run_node(
                build_proposal_request, completed, run_id=f"propose-inputs-{stage}-{attempt}"
            )
        )
        followup = ProposedFollowup(
            note=unproposed_stage_note(proposal_request),
            blocked_note=blocked_stages_note(proposal_request.blocked_next_stages),
        )
        if proposal_request.runnable_next_stages and proposal_request.carried_compounds:
            proposal = StageProposal.model_validate(
                await ctx.run_node(
                    proposer_agent, proposal_request, run_id=f"propose-{stage}-{attempt}"
                )
            )
            followup = ProposedFollowup.model_validate(
                await ctx.run_node(
                    create_recommended_card,
                    ProposalDecision(
                        completed=completed, request=proposal_request, proposal=proposal
                    ),
                    run_id=f"propose-card-{stage}-{attempt}",
                )
            )

        return CampaignOutcome.model_validate(
            await ctx.run_node(
                complete_campaign_on_card,
                CampaignCompletion(triaged=triaged, followup=followup),
                run_id=f"complete-{stage}-{attempt}",
            )
        )


cascade_campaign = Workflow(name="cascade_campaign", edges=[(START, run_campaign)])
