from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from cascade.schemas import BindingSite, TargetSource, TargetStructure, Workload

Stage = Literal[*get_args(Workload), "synthesis"]
LigandSource = Literal["attachment", "smiles_in_text", "url"]
Executor = Literal["cloud_run_job", "cloud_batch"]
QualityHint = Literal["fast", "standard", "thorough"]


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


class IntakeAnnouncement(BaseModel):
    card_id: str
    intent: CampaignIntent


class LigandRequest(BaseModel):
    run_id: str
    card_id: str
    source: LigandSource
    reference: str
    card_description: str


class LigandLibrary(BaseModel):
    ligands_uri: str
    compound_count: int
    source: LigandSource
    compound_names: list[str] = Field(default_factory=list)
    skipped_lines: int = 0


class PlanRequest(BaseModel):
    intent: CampaignIntent
    stage: Workload
    compound_count: int
    target_has_cocrystal_ligand: bool


class WorkloadPlan(BaseModel):
    workload: Workload
    binding_site: BindingSite | None = None
    binding_site_method: Literal["co_crystal", "described_pocket", "predicted_structure", "none"]
    binding_site_confidence: Literal["high", "medium", "low"]
    params: dict = Field(default_factory=dict)
    control_compound: str | None = None
    rationale: str


class JobLaunch(BaseModel):
    campaign_id: str
    card_id: str
    run_id: str
    workload: Workload
    attempt: int = 1
    plan: WorkloadPlan
    target: TargetStructure
    library: LigandLibrary
    executor: Executor


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


class StageOutcome(BaseModel):
    run_id: str
    status: Literal["succeeded", "failed"]
    artifacts_recorded: int


class CampaignOutcome(BaseModel):
    status: Literal[
        "acknowledged",
        "needs_clarification",
        "duplicate",
        "already_complete",
        "submitted",
        "completed",
        "failed",
        "unsupported_executor",
    ]
    campaign_id: str
    card_id: str
    target_name: str | None = None
    rationale: str


class UnsupportedExecutor(BaseModel):
    campaign_id: str
    card_id: str
    rationale: str
