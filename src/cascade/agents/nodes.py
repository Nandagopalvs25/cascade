import re
import uuid
from collections.abc import AsyncGenerator
from pathlib import PurePosixPath

import httpx
from google.adk.agents.context import Context
from google.adk.events import RequestInput
from google.adk.workflow import RetryConfig, node
from google.api_core import exceptions as google_api_exceptions
from google.auth.exceptions import GoogleAuthError

from cascade.agents.persistence import (
    attach_execution_name_to_job,
    job_interrupt_id,
    load_succeeded_run_for_card,
    record_job_completion,
    record_run_and_reserve_job_attempt,
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
    StageOutcome,
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


LIGAND_SUFFIXES = frozenset({".sdf", ".sd", ".mol", ".smi", ".smiles", ".txt", ".csv"})
DEFAULT_LIGAND_FILENAME = "ligands.smi"
SDF_RECORD_SEPARATOR = "$$$$"
BRACKET_ATOM_PATTERN = re.compile(r"\[[^\]]*\]")
SMILES_BODY_PATTERN = re.compile(r"^[BCNOPSFIHlrbcnops0-9()=#@+\-/\\%.*]{2,}$")
ELEMENT_CHARACTERS = frozenset("BCNOPSFIHbcnops")
ALLOWED_JOB_PARAMS = frozenset(
    {"exhaustiveness", "num_modes", "seed", "cpu", "receptor_ph", "binding_site_padding"}
)
GPU_WORKLOADS = frozenset({"md_stability", "fold_affinity"})
IMPLEMENTED_WORKLOADS = frozenset({"dock"})


SIGNING_FAILURES = (AttributeError, GoogleAuthError, google_api_exceptions.GoogleAPICallError)


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


def unusable_library_questions(intent: CampaignIntent, library: LigandLibrary) -> list[str]:
    questions = []
    named_control = (intent.control_compound or "").strip().lower()
    known_names = {name.lower() for name in library.compound_names}
    if named_control and known_names and named_control not in known_names:
        questions.append(
            f"The control compound {intent.control_compound!r} is not among the "
            f"{library.compound_count} compounds CASCADE could read "
            f"({', '.join(library.compound_names)}). Without it the run cannot be validated. "
            f"Trello rewrites SMILES containing bracketed stereocentres because [X](Y) is "
            f"Markdown link syntax — attach a .smi file or wrap each SMILES in backticks."
        )
    if (
        intent.expected_compound_count is not None
        and intent.expected_compound_count != library.compound_count
    ):
        questions.append(
            f"The card lists {intent.expected_compound_count} compounds but CASCADE could only "
            f"read {library.compound_count}. Check the SMILES that did not survive: Trello "
            f"rewrites [X](Y) sequences as Markdown links."
        )
    return questions


def skipped_lines_note(skipped_lines: int) -> str:
    if skipped_lines == 0:
        return ""
    return f", {skipped_lines} unreadable line(s) skipped"


def executor_for_workload(workload: str, compound_count: int) -> str:
    if workload in GPU_WORKLOADS or compound_count > settings.max_ligands_per_cloud_run_job:
        return "cloud_batch"
    return "cloud_run_job"


def unsupported_stage_rationale(workload: str, compound_count: int) -> str | None:
    if workload not in IMPLEMENTED_WORKLOADS:
        return (
            f"{workload} has no workload container yet, so CASCADE cannot run this stage. "
            f"Only {', '.join(sorted(IMPLEMENTED_WORKLOADS))} is implemented."
        )
    if executor_for_workload(workload, compound_count) != "cloud_run_job":
        return (
            f"{compound_count} compounds exceeds the "
            f"{settings.max_ligands_per_cloud_run_job}-ligand Cloud Run Job ceiling, and "
            f"Cloud Batch submission is not implemented yet."
        )
    return None


def deterministic_run_id(card_id: str, stage: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cascade:run:{card_id}:{stage}"))


def looks_like_smiles(token: str) -> bool:
    outside_brackets = BRACKET_ATOM_PATTERN.sub("", token)
    if not SMILES_BODY_PATTERN.match(outside_brackets):
        return False
    return bool(set(outside_brackets) & ELEMENT_CHARACTERS)


def smiles_library_lines_from_text(text: str) -> tuple[list[str], int]:
    kept: list[str] = []
    skipped = 0
    for line in text.splitlines():
        stripped = line.replace("`", "").strip().lstrip("-*•").strip()
        if not stripped or stripped.startswith("#"):
            continue
        token, *name_fields = stripped.split()
        if not looks_like_smiles(token):
            skipped += 1
            continue
        name = "_".join(name_fields) if name_fields else f"compound_{len(kept) + 1}"
        kept.append(f"{token}\t{name}")
    return kept, skipped


def compound_names_from_library_lines(lines: list[str]) -> list[str]:
    return [line.split("\t")[1] for line in lines if "\t" in line]


def ligand_filename_for_reference(reference: str) -> str:
    suffix = PurePosixPath(reference).suffix.lower()
    if suffix in LIGAND_SUFFIXES:
        return f"ligands{suffix}"
    return DEFAULT_LIGAND_FILENAME


def compound_count_in_library(filename: str, text: str) -> int:
    if filename.endswith((".sdf", ".sd", ".mol")):
        return len([block for block in text.split(SDF_RECORD_SEPARATOR) if block.strip()])
    if filename.endswith(".csv"):
        return max(len([line for line in text.splitlines() if line.strip()]) - 1, 0)
    return len(
        [line.strip() for line in text.splitlines() if line.strip() and line.strip()[0] != "#"]
    )


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
            key: value for key, value in node_input.plan.params.items() if key in ALLOWED_JOB_PARAMS
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
    )


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def announce_job_submitted_on_card(ctx: Context, node_input: JobSubmission) -> JobSubmission:
    await trello.add_comment(
        node_input.card_id,
        f"**{node_input.workload} job submitted.**\n\n"
        f"Run: `{node_input.run_id}` (attempt {node_input.attempt})\n"
        f"{node_input.library.compound_count} compounds queued"
        f"{skipped_lines_note(node_input.library.skipped_lines)}.\n"
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


def job_outcome_headline(outcome: JobOutcome) -> str:
    if outcome.status != "succeeded":
        return f"{outcome.workload} failed with exit code {outcome.exit_code}: {outcome.error}"
    summary = outcome.summary
    docked = summary.get("ligands_docked", "unknown")
    failed = summary.get("ligands_failed", 0)
    best = summary.get("best_compound_id", "unknown")
    affinity = summary.get("best_affinity_kcal_per_mol", "unknown")
    return (
        f"{docked} compounds docked ({failed} failed to load); best was {best} "
        f"at {affinity} kcal/mol"
    )


@node(retry_config=FETCH_RETRY, timeout=30.0)
async def complete_campaign_on_card(ctx: Context, node_input: JobResult) -> CampaignOutcome:
    outcome = node_input.outcome
    control = outcome.summary.get("control_compound") or {}
    control_line = ""
    if control:
        control_line = (
            f"\nControl `{control.get('requested_name')}`: {control.get('status')}"
            f" (best-mode RMSD {control.get('lowest_mode_rmsd_angstrom')} A"
            f" at rank {control.get('lowest_mode_rank')})"
        )
    results_link = await downloadable_results_link(outcome.results_archive_uri)
    headline = job_outcome_headline(outcome)
    await trello.add_comment(
        node_input.submission.card_id,
        f"**{outcome.workload} finished.**\n\n{headline}{control_line}\n\n"
        f"[Download results]({results_link})\n\n"
        f"Archived at `{outcome.results_uri}`",
    )
    await trello.move_card(node_input.submission.card_id, settings.trello_list_done)
    return CampaignOutcome(
        status="completed",
        campaign_id=node_input.submission.campaign_id,
        card_id=node_input.submission.card_id,
        rationale=headline,
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
