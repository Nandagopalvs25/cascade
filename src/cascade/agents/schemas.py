from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cascade.agents.capabilities import InputKind
from cascade.schemas import BindingSite, TargetSource, TargetStructure, Workload

LigandSource = Literal[
    "attachment", "smiles_in_text", "url", "parent_run_poses", "parent_run_library"
]
Executor = Literal["cloud_run_job", "cloud_run_gpu_job", "cloud_batch"]
QualityHint = Literal["fast", "standard", "thorough"]
ControlVerdict = Literal["passed", "pose_sampled_not_top_ranked", "failed", "not_measured"]
TriageDisposition = Literal["promote", "hold", "reject"]
TriageAction = Literal["complete", "rerun_with_more_effort", "escalate_to_scientist"]


class CardTrigger(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    campaign_id: str
    card_id: str
    action_id: str


class CardInputs(BaseModel):
    card_id: str
    title: str
    description: str
    attachment_names: list[str] = Field(default_factory=list)


class CampaignIntent(BaseModel):
    target_name: str | None = None
    target_source: TargetSource | None = None
    target_reference: str | None = None
    ligand_source: LigandSource | None = None
    ligand_reference: str | None = None
    control_compound: str | None = None
    expected_compound_count: int | None = None
    quality_hint: QualityHint = "standard"
    requested_stages: list[Workload] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    rationale: str


class CampaignState(BaseModel):
    is_complete: bool
    completed_run_id: str | None = None
    completed_workload: Workload | None = None


class LineageRun(BaseModel):
    run_id: str
    workload: str
    state: str
    target: TargetStructure | None = None
    ligands_uri: str | None = None


class IntakeAnnouncement(BaseModel):
    card_id: str
    intent: CampaignIntent


class LigandLibrary(BaseModel):
    ligands_uri: str
    compound_count: int
    source: LigandSource
    compound_names: list[str] = Field(default_factory=list)
    skipped_lines: int = 0


class UnmetRequirement(BaseModel):
    kind: InputKind
    question: str


class StageInputRequest(BaseModel):
    run_id: str
    card_id: str
    stage: Workload
    intent: CampaignIntent
    card_description: str
    attachment_names: list[str] = Field(default_factory=list)
    parent_run_id: str | None = None


class ResolvedStageInputs(BaseModel):
    library: LigandLibrary | None = None
    target: TargetStructure | None = None
    unmet: list[UnmetRequirement] = Field(default_factory=list)


class PlanRequest(BaseModel):
    intent: CampaignIntent
    stage: Workload
    compound_count: int
    target_has_cocrystal_ligand: bool


class WorkloadParams(BaseModel):
    exhaustiveness: int | None = Field(default=None, ge=1, le=512)
    num_modes: int | None = Field(default=None, ge=9, le=50)
    cpu: int | None = Field(default=None, ge=0, le=8)
    ligand_ph: float | None = Field(default=None, ge=0.0, le=14.0)
    conformers_per_ligand: int | None = Field(default=None, ge=1, le=32)
    max_compounds: int | None = Field(default=None, ge=1)
    herg_logp_threshold: float | None = Field(default=None, ge=0.0)
    herg_minimum_aromatic_rings: int | None = Field(default=None, ge=0)
    brenk_alerts_that_fail: int | None = Field(default=None, ge=1)
    lipinski_violations_that_fail: int | None = Field(default=None, ge=1)
    model_name: str | None = None
    seeds: list[int] | None = None
    samples_per_seed: int | None = Field(default=None, ge=1, le=25)
    max_complexes: int | None = Field(default=None, ge=1, le=64)
    use_msa: bool | None = None
    equilibration_steps: int | None = Field(default=None, ge=0, le=500_000)
    production_steps: int | None = Field(default=None, ge=1000, le=5_000_000)
    timestep_femtoseconds: float | None = Field(default=None, ge=0.5, le=4.0)
    temperature_kelvin: float | None = Field(default=None, ge=100.0, le=400.0)
    frames_recorded: int | None = Field(default=None, ge=5, le=1000)
    pose_drift_threshold_angstrom: float | None = Field(default=None, ge=0.5, le=10.0)
    contact_retention_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class WorkloadPlan(BaseModel):
    workload: Workload
    binding_site: BindingSite | None = None
    binding_site_method: Literal["co_crystal", "described_pocket", "predicted_structure", "none"]
    binding_site_confidence: Literal["high", "medium", "low"]
    params: WorkloadParams
    control_compound: str | None = None
    rationale: str


class JobLaunch(BaseModel):
    campaign_id: str
    card_id: str
    run_id: str
    workload: Workload
    attempt: int = 1
    plan: WorkloadPlan
    target: TargetStructure | None = None
    library: LigandLibrary
    executor: Executor
    parent_run_id: str | None = None


class JobSubmission(BaseModel):
    campaign_id: str
    card_id: str
    run_id: str
    workload: Workload
    attempt: int
    execution_name: str
    spec_uri: str
    output_uri: str
    library: LigandLibrary
    plan: WorkloadPlan


class JobOutcome(BaseModel):
    run_id: str
    workload: Workload
    status: Literal["succeeded", "failed"]
    exit_code: int
    results_uri: str | None = None
    results_manifest_uri: str | None = None
    results_archive_uri: str | None = None
    summary: dict = Field(default_factory=dict)
    error: str | None = None
    error_type: str | None = None


class JobResult(BaseModel):
    submission: JobSubmission
    outcome: JobOutcome


class ControlCheck(BaseModel):
    verdict: ControlVerdict
    compound_name: str | None = None
    status: str | None = None
    top_pose_rmsd_angstrom: float | None = None
    lowest_mode_rmsd_angstrom: float | None = None
    lowest_mode_rank: int | None = None
    threshold_angstrom: float | None = None
    detail: str


class TriageRequest(BaseModel):
    run_id: str
    workload: Workload
    attempt: int
    attempts_remaining: int
    control: ControlCheck
    score_analysis: dict = Field(default_factory=dict)
    scores: list[dict] = Field(default_factory=list)
    job_summary: dict = Field(default_factory=dict)


class CompoundJudgement(BaseModel):
    compound_id: str
    disposition: TriageDisposition
    reason: str


class TriageVerdict(BaseModel):
    run_is_trustworthy: bool
    results_discriminate: bool
    next_action: TriageAction
    headline: str
    compounds: list[CompoundJudgement]
    rationale: str


class TriageDecision(BaseModel):
    result: JobResult
    request: TriageRequest
    verdict: TriageVerdict


class TriagedJobResult(BaseModel):
    result: JobResult
    verdict: TriageVerdict
    control: ControlCheck


StageDecisionPoint = Literal["first_stage", "next_stage"]


class StageInputStatus(BaseModel):
    kind: InputKind
    resolves: bool


class StageInputReadiness(BaseModel):
    stage: Workload
    inputs_resolve: bool
    inputs: list[StageInputStatus] = Field(default_factory=list)
    blocking_reason: str | None = None
    already_run_as: str | None = None


class PreviousStageSummary(BaseModel):
    run_id: str
    workload: str
    state: str
    headline: str | None = None
    run_is_trustworthy: bool | None = None
    results_discriminate: bool | None = None


class RejectedStageChoice(BaseModel):
    stage: Workload
    reason: str


class BlockedStage(BaseModel):
    workload: Workload
    reason: str


class CompletedStage(BaseModel):
    triaged: TriagedJobResult
    target_name: str | None = None
    target_source: TargetSource | None = None
    target_reference: str | None = None


class ProposalRequest(BaseModel):
    completed_stage: Workload
    target_name: str | None = None
    run_is_trustworthy: bool = True
    results_discriminate: bool
    triage_headline: str
    carried_compounds: list[CompoundJudgement] = Field(default_factory=list)
    carried_disposition: TriageDisposition = "promote"
    runnable_next_stages: list[Workload] = Field(default_factory=list)
    blocked_next_stages: list[BlockedStage] = Field(default_factory=list)


class StageProposal(BaseModel):
    next_stage: Workload | None = None
    card_title: str
    reason: str
    rationale: str


class ProposalDecision(BaseModel):
    completed: CompletedStage
    request: ProposalRequest
    proposal: StageProposal


class StageDecisionRequest(BaseModel):
    decision_point: StageDecisionPoint
    card_title: str
    card_description: str
    intent: CampaignIntent
    compound_count: int
    campaign_history: list[PreviousStageSummary] = Field(default_factory=list)
    stage_readiness: list[StageInputReadiness] = Field(default_factory=list)
    completed_stage: Workload | None = None
    completed_run_id: str | None = None
    triage_headline: str | None = None
    run_is_trustworthy: bool | None = None
    results_discriminate: bool | None = None
    carried_compounds: list[CompoundJudgement] = Field(default_factory=list)
    carried_disposition: TriageDisposition | None = None
    rejected_choices: list[RejectedStageChoice] = Field(default_factory=list)
    choices_remaining: int = 1


class StageDecision(BaseModel):
    chosen_stage: Workload | None = None
    question_it_answers: str
    card_title: str
    reason: str
    rationale: str


class StageDecisionInputs(BaseModel):
    decision_point: StageDecisionPoint
    card: CardInputs
    intent: CampaignIntent
    parent_run_id: str | None = None
    attachment_names: list[str] = Field(default_factory=list)
    completed: CompletedStage | None = None


class StageDecisionRecord(BaseModel):
    run_id: str
    request: StageDecisionRequest
    decision: StageDecision


class NoStageChosen(BaseModel):
    campaign_id: str
    card_id: str
    decision: StageDecision


class ProposedFollowup(BaseModel):
    next_stage: Workload | None = None
    created_card_id: str | None = None
    created_card_url: str | None = None
    carried_compounds: list[str] = Field(default_factory=list)
    note: str
    blocked_note: str = ""


class CampaignCompletion(BaseModel):
    triaged: TriagedJobResult
    followup: ProposedFollowup


class StageOutcome(BaseModel):
    run_id: str
    status: Literal["succeeded", "failed"]
    artifacts_recorded: int


class CampaignOutcome(BaseModel):
    status: Literal[
        "needs_clarification",
        "duplicate",
        "already_complete",
        "needs_attention",
        "completed",
        "failed",
        "unsupported_executor",
    ]
    campaign_id: str
    card_id: str
    rationale: str


class UnsupportedExecutor(BaseModel):
    campaign_id: str
    card_id: str
    rationale: str
