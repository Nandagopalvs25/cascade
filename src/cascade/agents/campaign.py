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
    stage_decision_agent,
    triage_agent,
)
from cascade.agents.nodes import (
    announce_control_failure_rerun_on_card,
    announce_intake_on_card,
    announce_job_submitted_on_card,
    apply_triage_verdict,
    ask_scientist_for_clarification,
    await_job_completion,
    build_stage_decision_request,
    build_triage_request,
    complete_campaign_on_card,
    create_recommended_card,
    escalate_failed_job_on_card,
    escalate_untrustworthy_run_on_card,
    fetch_card_and_attachments,
    load_campaign_state,
    record_job_outcome,
    record_stage_choice_in_decision_log,
    report_no_stage_chosen_on_card,
    report_unsupported_executor_on_card,
    resolve_inputs_for_stage,
    submit_workload_job,
)
from cascade.agents.policy import (
    deterministic_run_id,
    executor_for_workload,
    plan_escalated_after_control_failure,
    proposal_decision_from_stage_decision,
    stage_blocking_reasons_for_requested_stages,
    stage_choice_rejection_reason,
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
    NoStageChosen,
    PlanRequest,
    ProposedFollowup,
    RejectedStageChoice,
    ResolvedStageInputs,
    StageDecision,
    StageDecisionInputs,
    StageDecisionRecord,
    StageDecisionRequest,
    StageInputRequest,
    TriageDecision,
    TriagedJobResult,
    TriageRequest,
    TriageVerdict,
    UnsupportedExecutor,
    WorkloadPlan,
)
from cascade.config import get_settings
from cascade.schemas import Workload

settings = get_settings()

FIRST_ATTEMPT = 1


async def validated_stage_decision(
    ctx: Context, request: StageDecisionRequest, run_id_prefix: str
) -> tuple[StageDecision, StageDecisionRequest]:
    decision = StageDecision(
        chosen_stage=None,
        question_it_answers="",
        card_title="",
        reason="",
        rationale="",
    )
    for choice in range(1, settings.max_stage_decision_attempts + 1):
        request = request.model_copy(
            update={"choices_remaining": settings.max_stage_decision_attempts - choice}
        )
        decision = StageDecision.model_validate(
            await ctx.run_node(
                stage_decision_agent, request, run_id=f"{run_id_prefix}-choice{choice}"
            )
        )
        rejection = stage_choice_rejection_reason(decision, request)
        if rejection is None:
            return decision, request
        request = request.model_copy(
            update={
                "rejected_choices": [
                    *request.rejected_choices,
                    RejectedStageChoice(stage=decision.chosen_stage, reason=rejection),
                ]
            }
        )
    return decision.model_copy(update={"chosen_stage": None}), request


async def propose_follow_up_and_finish(
    ctx: Context,
    card: CardInputs,
    intent: CampaignIntent,
    triaged: TriagedJobResult,
    stage: Workload,
    attempt: int,
) -> CampaignOutcome:
    completed = CompletedStage(
        triaged=triaged,
        target_name=intent.target_name,
        target_source=intent.target_source,
        target_reference=intent.target_reference,
    )
    next_request = StageDecisionRequest.model_validate(
        await ctx.run_node(
            build_stage_decision_request,
            StageDecisionInputs(
                decision_point="next_stage",
                card=card,
                intent=intent,
                completed=completed,
                attachment_names=card.attachment_names,
            ),
            run_id=f"next-stage-inputs-{stage}-{attempt}",
        )
    )
    next_decision, next_request = await validated_stage_decision(
        ctx, next_request, run_id_prefix=f"next-stage-{stage}-{attempt}"
    )
    await ctx.run_node(
        record_stage_choice_in_decision_log,
        StageDecisionRecord(
            run_id=triaged.result.outcome.run_id,
            request=next_request,
            decision=next_decision,
        ),
        run_id=f"record-next-stage-{stage}-{attempt}",
    )
    proposal = proposal_decision_from_stage_decision(completed, next_request, next_decision)
    followup = ProposedFollowup(
        note=unproposed_stage_note(proposal.request),
        blocked_note=blocked_stages_note(proposal.request.blocked_next_stages),
    )
    if next_decision.chosen_stage is not None:
        followup = ProposedFollowup.model_validate(
            await ctx.run_node(
                create_recommended_card, proposal, run_id=f"propose-card-{stage}-{attempt}"
            )
        )
    return CampaignOutcome.model_validate(
        await ctx.run_node(
            complete_campaign_on_card,
            CampaignCompletion(triaged=triaged, followup=followup),
            run_id=f"complete-{stage}-{attempt}",
        )
    )


async def ask_for_clarification_and_stop(
    ctx: Context,
    node_input: CardTrigger,
    intent: CampaignIntent,
    questions: list[str],
    run_id: str,
) -> CampaignOutcome:
    await ctx.run_node(
        ask_scientist_for_clarification,
        IntakeAnnouncement(
            card_id=node_input.card_id,
            intent=intent.model_copy(update={"ambiguities": questions}),
        ),
        run_id=run_id,
    )
    return CampaignOutcome(
        status="needs_clarification",
        campaign_id=node_input.campaign_id,
        card_id=node_input.card_id,
        rationale=questions[0],
    )


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

    if intent.ambiguities:
        return await ask_for_clarification_and_stop(
            ctx, node_input, intent, intent.ambiguities, f"clarify-intake-{action_id}"
        )

    parent_run_id = parent_run_id_in_card_description(card.description)

    first_stage_request = StageDecisionRequest.model_validate(
        await ctx.run_node(
            build_stage_decision_request,
            StageDecisionInputs(
                decision_point="first_stage",
                card=card,
                intent=intent,
                parent_run_id=parent_run_id,
                attachment_names=card.attachment_names,
            ),
            run_id=f"stage-decision-inputs-{action_id}",
        )
    )
    first_decision, first_stage_request = await validated_stage_decision(
        ctx, first_stage_request, run_id_prefix=f"first-stage-{action_id}"
    )
    if first_decision.chosen_stage is None:
        unrunnable = stage_blocking_reasons_for_requested_stages(intent, first_stage_request)
        if unrunnable:
            return await ask_for_clarification_and_stop(
                ctx, node_input, intent, unrunnable, f"clarify-no-stage-{action_id}"
            )
        return CampaignOutcome.model_validate(
            await ctx.run_node(
                report_no_stage_chosen_on_card,
                NoStageChosen(
                    campaign_id=node_input.campaign_id,
                    card_id=node_input.card_id,
                    decision=first_decision,
                ),
                run_id=f"no-stage-{action_id}",
            )
        )

    stage = first_decision.chosen_stage
    run_id = deterministic_run_id(node_input.card_id, stage)

    resolved = ResolvedStageInputs.model_validate(
        await ctx.run_node(
            resolve_inputs_for_stage,
            StageInputRequest(
                run_id=run_id,
                card_id=node_input.card_id,
                stage=stage,
                intent=intent,
                card_description=card.description,
                attachment_names=card.attachment_names,
                parent_run_id=parent_run_id,
            ),
            run_id=f"inputs-{stage}-{action_id}",
        )
    )

    blocking = [
        *intent.ambiguities,
        *[requirement.question for requirement in resolved.unmet],
        *unusable_library_questions(intent, resolved.library),
    ]
    if blocking:
        return await ask_for_clarification_and_stop(
            ctx, node_input, intent, blocking, f"clarify-{stage}-{action_id}"
        )

    await ctx.run_node(
        announce_intake_on_card,
        IntakeAnnouncement(card_id=node_input.card_id, intent=intent),
        run_id=f"announce-intake-{action_id}",
    )

    library = resolved.library
    target = resolved.target

    plan = WorkloadPlan.model_validate(
        await ctx.run_node(
            planner_agent,
            PlanRequest(
                intent=intent,
                stage=stage,
                compound_count=library.compound_count,
                target_has_cocrystal_ligand=target is not None and target.source == "rcsb",
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

        return await propose_follow_up_and_finish(ctx, card, intent, triaged, stage, attempt)


cascade_campaign = Workflow(name="cascade_campaign", edges=[(START, run_campaign)])
