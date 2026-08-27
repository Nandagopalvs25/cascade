from collections.abc import AsyncGenerator

import httpx
from google.adk.agents.context import Context
from google.adk.events import RequestInput
from google.adk.workflow import RetryConfig, node
from google.api_core import exceptions as google_api_exceptions
from google.auth.exceptions import GoogleAuthError

from cascade.agents.card_text import (
    agent_prose_without_runaway_text,
    blocked_stages_note,
    control_compound_line,
    followup_card_comment,
    job_outcome_headline,
    library_lines_for_compounds,
    planner_decision_lines,
    proposed_card_description,
    skipped_lines_note,
    triage_card_comment,
)
from cascade.agents.compound_library import (
    DEFAULT_LIGAND_FILENAME,
    compound_count_in_library,
    compound_names_from_library_lines,
    ligand_filename_for_reference,
    smiles_library_lines_from_text,
)
from cascade.agents.persistence import (
    attach_execution_name_to_job,
    job_interrupt_id,
    load_succeeded_run_for_card,
    record_decision,
    record_job_completion,
    record_run_and_reserve_job_attempt,
    workloads_already_run_in_lineage,
)
from cascade.agents.policy import (
    allowed_job_params_for_workload,
    attempts_remaining_after,
    compound_records_from_manifest,
    compounds_carried_to_next_stage,
    control_check_for_job,
    enforce_control_gate,
    hold_every_scored_compound_when_triage_judged_none,
    next_stage_options,
)
from cascade.agents.schemas import (
    BlockedStage,
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
    ProposalDecision,
    ProposalRequest,
    ProposedFollowup,
    StageOutcome,
    TriageDecision,
    TriagedJobResult,
    TriageRequest,
    UnsupportedExecutor,
)
from cascade.clients import campaign_inputs, cloud_run_jobs, gcs, trello
from cascade.clients.gcs import (
    authenticated_browser_url,
    run_inputs_prefix,
    run_outputs_prefix,
    run_spec_path,
    storage_console_url,
)
from cascade.config import get_settings
from cascade.schemas import JobSpec, TargetRequest, TargetStructure

settings = get_settings()

FETCH_RETRY = RetryConfig(max_attempts=3, backoff_factor=2.0, exceptions=[httpx.HTTPError])
SUBMIT_RETRY = RetryConfig(
    max_attempts=3, backoff_factor=2.0, exceptions=[google_api_exceptions.ServerError]
)
MANIFEST_READ_FAILURES = (
    google_api_exceptions.GoogleAPICallError,
    GoogleAuthError,
    ValueError,
    OSError,
)
SIGNING_FAILURES = (AttributeError, GoogleAuthError, google_api_exceptions.GoogleAPICallError)


settings = get_settings()

FETCH_RETRY = RetryConfig(max_attempts=3, backoff_factor=2.0, exceptions=[httpx.HTTPError])

SUBMIT_RETRY = RetryConfig(
    max_attempts=3, backoff_factor=2.0, exceptions=[google_api_exceptions.ServerError]
)


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


async def downloadable_results_link(results_archive_uri: str | None) -> str:
    if not results_archive_uri:
        return "no results archive was produced"
    try:
        return await gcs.generate_signed_url_for_uri(
            results_archive_uri,
            settings.results_link_expiry_minutes,
            download_filename="results.zip",
        )
    except SIGNING_FAILURES:
        return authenticated_browser_url(results_archive_uri)


@node(timeout=30.0)
async def load_campaign_state(ctx: Context, node_input: CardTrigger) -> CampaignState:
    completed = await load_succeeded_run_for_card(node_input.card_id)
    return CampaignState(
        is_complete=completed is not None,
        completed_run_id=completed[0] if completed else None,
        completed_workload=completed[1] if completed else None,
    )


@node(retry_config=FETCH_RETRY, timeout=60.0)
async def prepare_ligand_library(ctx: Context, node_input: LigandRequest) -> LigandLibrary:
    skipped = 0
    compound_names: list[str] = []
    if node_input.source == "smiles_in_text":
        lines, skipped = smiles_library_lines_from_text(node_input.card_description)
        if not lines:
            raise ValueError("no SMILES could be read from the card description")
        filename = DEFAULT_LIGAND_FILENAME
        content = ("\n".join(lines) + "\n").encode()
        compound_names = compound_names_from_library_lines(lines)
    else:
        if node_input.source == "attachment":
            content = await campaign_inputs.download_named_card_attachment(
                node_input.card_id, node_input.reference
            )
        else:
            content = await campaign_inputs.download_from_url(node_input.reference)
        if not content.strip():
            raise ValueError(f"ligand library {node_input.reference!r} was empty")
        filename = ligand_filename_for_reference(node_input.reference)

    ligands_uri = await gcs.upload_bytes(
        f"{run_inputs_prefix(node_input.run_id)}/{filename}", content, "text/plain"
    )
    return LigandLibrary(
        ligands_uri=ligands_uri,
        compound_count=compound_count_in_library(filename, content.decode("utf-8", "replace")),
        source=node_input.source,
        compound_names=compound_names,
        skipped_lines=skipped,
    )


@node(retry_config=FETCH_RETRY, timeout=60.0)
async def archive_target_structure(ctx: Context, node_input: TargetRequest) -> TargetStructure:
    return await campaign_inputs.resolve_target_structure(node_input)


@node(retry_config=SUBMIT_RETRY, timeout=120.0)
async def submit_workload_job(ctx: Context, node_input: JobLaunch) -> JobSubmission:
    output_uri = gcs.uri_for_path(run_outputs_prefix(node_input.run_id, node_input.attempt))
    spec = JobSpec(
        run_id=node_input.run_id,
        workload=node_input.workload,
        target=node_input.target,
        ligands_uri=node_input.library.ligands_uri,
        binding_site=node_input.plan.binding_site,
        params={
            key: value
            for key, value in node_input.plan.params.model_dump(exclude_none=True).items()
            if key in allowed_job_params_for_workload(node_input.workload)
        },
        output_uri=output_uri,
        control_compound=node_input.plan.control_compound,
    )

    execution_name = await record_run_and_reserve_job_attempt(
        node_input.run_id,
        node_input.card_id,
        spec,
        node_input.executor,
        node_input.attempt,
        node_input.parent_run_id,
    )
    await record_decision(
        node_input.run_id,
        "planner",
        f"{node_input.workload}_plan",
        node_input.plan.rationale,
        {"attempt": node_input.attempt, "compound_count": node_input.library.compound_count},
        node_input.plan.model_dump(exclude_none=True),
    )
    if execution_name is None:
        execution_name = await cloud_run_jobs.submit_workload_execution(
            spec, gcs, settings, attempt=node_input.attempt
        )
        await attach_execution_name_to_job(node_input.run_id, node_input.attempt, execution_name)

    return JobSubmission(
        campaign_id=node_input.campaign_id,
        card_id=node_input.card_id,
        run_id=node_input.run_id,
        workload=node_input.workload,
        attempt=node_input.attempt,
        execution_name=execution_name,
        spec_uri=gcs.uri_for_path(run_spec_path(node_input.run_id, node_input.attempt)),
        output_uri=output_uri,
        library=node_input.library,
        plan=node_input.plan,
    )


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def announce_job_submitted_on_card(ctx: Context, node_input: JobSubmission) -> JobSubmission:
    decisions = "\n".join(planner_decision_lines(node_input.plan, node_input.workload))
    await trello.add_comment(
        node_input.card_id,
        f"**{node_input.workload} job submitted.**\n\n"
        f"Run: `{node_input.run_id}` (attempt {node_input.attempt})\n"
        f"{node_input.library.compound_count} compounds queued"
        f"{skipped_lines_note(node_input.library.skipped_lines)}.\n\n"
        f"{decisions}\n\n"
        f"[Results folder]({storage_console_url(node_input.output_uri)})",
    )
    return node_input


@node
async def await_job_completion(
    ctx: Context, node_input: JobSubmission
) -> AsyncGenerator[JobOutcome | RequestInput, None]:
    yield RequestInput(
        interrupt_id=job_interrupt_id(node_input.run_id, node_input.attempt),
        message=f"Awaiting {node_input.workload} job {node_input.execution_name}",
        payload={"run_id": node_input.run_id, "attempt": node_input.attempt},
        response_schema=JobOutcome,
    )


@node(timeout=60.0)
async def record_job_outcome(ctx: Context, node_input: JobResult) -> StageOutcome:
    outcome = node_input.outcome
    recorded = await record_job_completion(
        outcome.run_id,
        node_input.submission.attempt,
        outcome.status == "succeeded",
        outcome.exit_code,
        {
            "results_prefix": outcome.results_uri or "",
            "results_manifest": outcome.results_manifest_uri or "",
            "results_archive": outcome.results_archive_uri or "",
        },
    )
    return StageOutcome(run_id=outcome.run_id, status=outcome.status, artifacts_recorded=recorded)


@node(retry_config=FETCH_RETRY, timeout=60.0)
async def build_triage_request(ctx: Context, node_input: JobResult) -> TriageRequest:
    outcome = node_input.outcome
    manifest: dict = {}
    if outcome.results_manifest_uri:
        try:
            manifest = await gcs.download_json_from_uri(outcome.results_manifest_uri)
        except MANIFEST_READ_FAILURES:
            manifest = {}
    control_summary = (
        manifest.get("control_compound") or outcome.summary.get("control_compound") or {}
    )
    attempt = node_input.submission.attempt
    return TriageRequest(
        run_id=outcome.run_id,
        workload=outcome.workload,
        attempt=attempt,
        attempts_remaining=attempts_remaining_after(attempt),
        control=control_check_for_job(outcome.workload, control_summary),
        score_analysis=manifest.get("score_analysis") or {},
        scores=compound_records_from_manifest(outcome.workload, manifest),
        job_summary=outcome.summary,
    )


@node(timeout=60.0)
async def apply_triage_verdict(ctx: Context, node_input: TriageDecision) -> TriagedJobResult:
    request = node_input.request
    verdict = enforce_control_gate(node_input.verdict, request.control, request.attempts_remaining)
    verdict = hold_every_scored_compound_when_triage_judged_none(verdict, request.scores)
    verdict = verdict.model_copy(
        update={
            "rationale": agent_prose_without_runaway_text(verdict.rationale, verdict.headline),
        }
    )
    await record_decision(
        request.run_id,
        "triage",
        f"{request.workload}_triage",
        verdict.rationale,
        request.model_dump(exclude_none=True),
        verdict.model_dump(),
    )
    return TriagedJobResult(result=node_input.result, verdict=verdict, control=request.control)


@node(timeout=30.0)
async def build_proposal_request(ctx: Context, node_input: CompletedStage) -> ProposalRequest:
    verdict = node_input.triaged.verdict
    completed_stage = node_input.triaged.result.outcome.workload
    carried, disposition = compounds_carried_to_next_stage(verdict)
    if not verdict.run_is_trustworthy:
        runnable: list[str] = []
        blocked: list[BlockedStage] = []
    else:
        already_run = await workloads_already_run_in_lineage(
            node_input.triaged.result.outcome.run_id
        )
        runnable, blocked = next_stage_options(completed_stage, len(carried), already_run)
    return ProposalRequest(
        completed_stage=completed_stage,
        target_name=node_input.target_name,
        run_is_trustworthy=verdict.run_is_trustworthy,
        results_discriminate=verdict.results_discriminate,
        triage_headline=verdict.headline,
        carried_compounds=carried,
        carried_disposition=disposition,
        runnable_next_stages=runnable,
        blocked_next_stages=blocked,
    )


@node(timeout=60.0)
async def create_recommended_card(ctx: Context, node_input: ProposalDecision) -> ProposedFollowup:
    request = node_input.request
    proposal = node_input.proposal.model_copy(
        update={
            "reason": agent_prose_without_runaway_text(
                node_input.proposal.reason, request.triage_headline
            ),
            "rationale": agent_prose_without_runaway_text(
                node_input.proposal.rationale, request.triage_headline
            ),
        }
    )
    submission = node_input.completed.triaged.result.submission
    blocked_note = blocked_stages_note(request.blocked_next_stages)
    if proposal.next_stage is None or proposal.next_stage not in request.runnable_next_stages:
        return ProposedFollowup(
            note=f"CASCADE is not proposing a follow-up stage. {' '.join(proposal.reason.split())}",
            blocked_note=blocked_note,
        )

    carried_names = [judgement.compound_id for judgement in request.carried_compounds]
    try:
        library_text = await gcs.download_text_from_uri(submission.library.ligands_uri)
    except MANIFEST_READ_FAILURES:
        library_text = ""
    library_lines = library_lines_for_compounds(library_text, carried_names)
    if not library_lines:
        return ProposedFollowup(
            note=(
                f"A {proposal.next_stage} stage is the right next step, but CASCADE could not "
                f"recover the structures of the carried compounds from "
                f"`{submission.library.ligands_uri}`, so it did not create a card that the next "
                f"campaign would be unable to read. Attach the library to a new card instead."
            ),
            blocked_note=blocked_note,
        )

    description = proposed_card_description(
        node_input.model_copy(update={"proposal": proposal}), library_lines, submission.run_id
    )
    card = await trello.create_card(
        settings.trello_list_recommended, proposal.card_title, description
    )
    followup = ProposedFollowup(
        next_stage=proposal.next_stage,
        created_card_id=card["id"],
        created_card_url=card.get("url"),
        carried_compounds=carried_names,
        note=" ".join(proposal.reason.split()),
        blocked_note=blocked_note,
    )
    await record_decision(
        submission.run_id,
        "proposer",
        f"{request.completed_stage}_followup",
        proposal.rationale,
        request.model_dump(exclude_none=True),
        followup.model_dump(),
    )
    return followup


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def announce_control_failure_rerun_on_card(
    ctx: Context, node_input: TriagedJobResult
) -> TriagedJobResult:
    await trello.add_comment(
        node_input.result.submission.card_id,
        f"**Control compound check failed.** {node_input.control.detail}\n\n"
        "The pipeline could not reproduce a pose it is known to get right, so nothing in this run "
        "can be read as a result yet. Ring conformations are fixed before docking starts, so the "
        "re-run generates more starting conformers per compound "
        f"({settings.control_rerun_conformers_per_ligand} instead of the default) rather than "
        "searching harder from the same geometry.",
    )
    return node_input


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def escalate_untrustworthy_run_on_card(
    ctx: Context, node_input: TriagedJobResult
) -> CampaignOutcome:
    submission = node_input.result.submission
    await trello.add_comment(
        submission.card_id,
        f"{triage_card_comment(node_input)}\n\n"
        f"Run `{submission.run_id}`, attempt {submission.attempt}. This needs a scientist.",
    )
    await trello.move_card(submission.card_id, settings.trello_list_needs_attention)
    return CampaignOutcome(
        status="needs_attention",
        campaign_id=submission.campaign_id,
        card_id=submission.card_id,
        rationale=node_input.verdict.rationale,
    )


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def complete_campaign_on_card(
    ctx: Context, node_input: CampaignCompletion
) -> CampaignOutcome:
    triaged = node_input.triaged
    submission = triaged.result.submission
    outcome = triaged.result.outcome
    control_line = control_compound_line(
        outcome.workload, outcome.summary.get("control_compound") or {}
    )
    results_link = await downloadable_results_link(outcome.results_archive_uri)
    headline = job_outcome_headline(outcome)
    await trello.add_comment(
        submission.card_id,
        f"**{outcome.workload} finished.**\n\n{headline}{control_line}\n\n"
        f"[Download results]({results_link})\n\n"
        f"Archived at `{outcome.results_uri}`\n\n---\n\n"
        f"{triage_card_comment(triaged)}\n\n---\n\n"
        f"{followup_card_comment(node_input.followup)}",
    )
    await trello.move_card(submission.card_id, settings.trello_list_done)
    return CampaignOutcome(
        status="completed",
        campaign_id=submission.campaign_id,
        card_id=submission.card_id,
        rationale=triaged.verdict.rationale,
    )


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def escalate_failed_job_on_card(ctx: Context, node_input: JobResult) -> CampaignOutcome:
    outcome = node_input.outcome
    await trello.add_comment(
        node_input.submission.card_id,
        f"**{outcome.workload} job failed.**\n\n"
        f"Exit code {outcome.exit_code}. {outcome.error_type or 'error'}: {outcome.error}\n\n"
        f"Run `{outcome.run_id}`, attempt {node_input.submission.attempt}.",
    )
    await trello.move_card(node_input.submission.card_id, settings.trello_list_needs_attention)
    return CampaignOutcome(
        status="failed",
        campaign_id=node_input.submission.campaign_id,
        card_id=node_input.submission.card_id,
        rationale=job_outcome_headline(outcome),
    )


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def report_unsupported_executor_on_card(
    ctx: Context, node_input: UnsupportedExecutor
) -> CampaignOutcome:
    await trello.add_comment(
        node_input.card_id,
        f"**CASCADE cannot run this stage yet.**\n\n{node_input.rationale}\n\n"
        f"Cloud Batch submission is not implemented, so this campaign stops here.",
    )
    await trello.move_card(node_input.card_id, settings.trello_list_needs_attention)
    return CampaignOutcome(
        status="unsupported_executor",
        campaign_id=node_input.campaign_id,
        card_id=node_input.card_id,
        rationale=node_input.rationale,
    )
