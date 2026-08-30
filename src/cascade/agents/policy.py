import uuid
from typing import get_args

from cascade.agents.capabilities import (
    InputKind,
    required_inputs_for_stage,
    unmet_inputs_for_stage,
    unmet_requirement_question,
)
from cascade.agents.schemas import (
    BlockedStage,
    CampaignIntent,
    CompletedStage,
    CompoundJudgement,
    ControlCheck,
    ProposalDecision,
    ProposalRequest,
    StageDecision,
    StageDecisionRequest,
    StageInputReadiness,
    StageInputStatus,
    StageProposal,
    TriageDisposition,
    TriageVerdict,
    WorkloadPlan,
)
from cascade.config import get_settings
from cascade.schemas import GPU_ACCELERATED_WORKLOADS, Workload

settings = get_settings()


ALLOWED_JOB_PARAMS_BY_WORKLOAD = {
    "dock": frozenset(
        {
            "exhaustiveness",
            "num_modes",
            "seed",
            "cpu",
            "receptor_ph",
            "ligand_ph",
            "conformers_per_ligand",
            "binding_site_padding",
            "scoring_function_error_kcal_per_mol",
        }
    ),
    "admet": frozenset(
        {
            "max_compounds",
            "herg_logp_threshold",
            "herg_minimum_aromatic_rings",
            "brenk_alerts_that_fail",
            "lipinski_violations_that_fail",
        }
    ),
    "cofold": frozenset(
        {
            "model_name",
            "seeds",
            "cycles",
            "diffusion_steps",
            "samples_per_seed",
            "dtype",
            "use_msa",
            "max_complexes",
            "prediction_timeout_seconds",
            "protein_sequence",
        }
    ),
    "md_stability": frozenset(
        {
            "max_complexes",
            "equilibration_steps",
            "production_steps",
            "timestep_femtoseconds",
            "temperature_kelvin",
            "frames_recorded",
            "minimization_max_iterations",
            "pose_drift_threshold_angstrom",
            "contact_retention_threshold",
            "contact_cutoff_angstrom",
            "contact_break_cutoff_angstrom",
        }
    ),
}

WORKLOADS_NEEDING_AN_UNBUILT_GPU_EXECUTOR = frozenset({"cofold"})
CLOUD_RUN_EXECUTORS = frozenset({"cloud_run_job", "cloud_run_gpu_job"})

IMPLEMENTED_WORKLOADS = frozenset({"dock", "admet", "cofold", "md_stability"})


def allowed_job_params_for_workload(workload: str) -> frozenset[str]:
    return ALLOWED_JOB_PARAMS_BY_WORKLOAD.get(workload, frozenset())


COMPOUND_RECORD_KEY_BY_WORKLOAD = {
    "dock": "scores",
    "admet": "assessments",
    "cofold": "predictions",
    "md_stability": "trajectories",
}


def compound_records_from_manifest(workload: str, manifest: dict) -> list[dict]:
    key = COMPOUND_RECORD_KEY_BY_WORKLOAD.get(workload)
    if key is None:
        return []
    return manifest.get(key) or []


def executor_for_workload(workload: str, compound_count: int) -> str:
    if (
        workload in WORKLOADS_NEEDING_AN_UNBUILT_GPU_EXECUTOR
        or compound_count > settings.max_ligands_per_cloud_run_job
    ):
        return "cloud_batch"
    if workload in GPU_ACCELERATED_WORKLOADS:
        return "cloud_run_gpu_job"
    return "cloud_run_job"


def unsupported_stage_rationale(workload: str, compound_count: int) -> str | None:
    if workload not in IMPLEMENTED_WORKLOADS:
        return (
            f"{workload} has no workload container yet, so CASCADE cannot run this stage. "
            f"Only {', '.join(sorted(IMPLEMENTED_WORKLOADS))} have containers."
        )
    if workload in WORKLOADS_NEEDING_AN_UNBUILT_GPU_EXECUTOR:
        return (
            f"{workload} needs a GPU executor. Its container is built, but the project's single "
            f"L4 GPU allocation is committed to the md_stability Cloud Run Job and Cloud Batch "
            f"submission is not wired up yet."
        )
    if executor_for_workload(workload, compound_count) not in CLOUD_RUN_EXECUTORS:
        return (
            f"{compound_count} compounds exceeds the "
            f"{settings.max_ligands_per_cloud_run_job}-ligand Cloud Run Job ceiling, and "
            f"Cloud Batch submission is not implemented yet."
        )
    return None


def deterministic_run_id(card_id: str, stage: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cascade:run:{card_id}:{stage}"))


CONTROL_GATED_WORKLOADS = frozenset({"dock"})


def control_check_for_job(workload: str, control: dict) -> ControlCheck:
    status = control.get("status")
    name = control.get("requested_name")
    if workload not in CONTROL_GATED_WORKLOADS:
        return ControlCheck(
            verdict="not_measured",
            compound_name=name,
            status=status,
            detail=f"{workload} has no co-crystallized pose to check a control against",
        )
    if not control or status in (None, "not_requested"):
        return ControlCheck(
            verdict="not_measured",
            compound_name=name,
            status=status,
            detail="the campaign ran without a control compound",
        )
    if status != "measured":
        return ControlCheck(
            verdict="not_measured",
            compound_name=name,
            status=status,
            detail=control.get("detail") or f"the control could not be measured: {status}",
        )

    threshold = settings.control_rmsd_threshold_angstrom
    top_pose_rmsd = control.get("rmsd_to_cocrystal_angstrom")
    lowest_mode_rmsd = control.get("lowest_mode_rmsd_angstrom")
    rank = control.get("lowest_mode_rank")
    if top_pose_rmsd is None:
        return ControlCheck(
            verdict="not_measured",
            compound_name=name,
            status=status,
            threshold_angstrom=threshold,
            detail="the control compound docked but no top-pose RMSD was reported",
        )

    measured = ControlCheck(
        verdict="failed",
        compound_name=name,
        status=status,
        top_pose_rmsd_angstrom=top_pose_rmsd,
        lowest_mode_rmsd_angstrom=lowest_mode_rmsd,
        lowest_mode_rank=rank,
        threshold_angstrom=threshold,
        detail="",
    )
    if top_pose_rmsd <= threshold:
        return measured.model_copy(
            update={
                "verdict": "passed",
                "detail": (
                    f"top-ranked pose RMSD {top_pose_rmsd} A is within the {threshold} A threshold"
                ),
            }
        )
    if lowest_mode_rmsd is not None and lowest_mode_rmsd <= threshold:
        return measured.model_copy(
            update={
                "verdict": "pose_sampled_not_top_ranked",
                "detail": (
                    f"top-ranked pose RMSD {top_pose_rmsd} A exceeds the {threshold} A threshold, "
                    f"but the crystal pose was recovered at rank {rank} with RMSD "
                    f"{lowest_mode_rmsd} A - the search found the right pose and the scoring "
                    f"function did not rank it first"
                ),
            }
        )
    return measured.model_copy(
        update={
            "detail": (
                f"top-ranked pose RMSD {top_pose_rmsd} A exceeds the {threshold} A threshold and "
                f"no reported pose came within it"
            )
        }
    )


def attempts_remaining_after(attempt: int) -> int:
    return max(settings.max_job_attempts - attempt, 0)


def plan_escalated_after_control_failure(plan: WorkloadPlan) -> WorkloadPlan:
    conformers = settings.control_rerun_conformers_per_ligand
    return plan.model_copy(
        update={
            "params": plan.params.model_copy(update={"conformers_per_ligand": conformers}),
            "rationale": (
                "The control compound did not reproduce its crystallographic pose, so the "
                "starting geometry is the suspect rather than the search. CASCADE raised "
                f"conformers_per_ligand to {conformers} and is re-running "
                "the same compounds against the same pocket. This escalation is enforced in "
                "code, not chosen by the model."
            ),
        }
    )


def enforce_control_gate(
    verdict: TriageVerdict, control: ControlCheck, attempts_remaining: int
) -> TriageVerdict:
    if control.verdict == "pose_sampled_not_top_ranked":
        verdict = verdict.model_copy(update={"results_discriminate": False})
    if control.verdict == "failed":
        return verdict.model_copy(
            update={
                "run_is_trustworthy": False,
                "next_action": (
                    "rerun_with_more_effort" if attempts_remaining > 0 else "escalate_to_scientist"
                ),
            }
        )
    if verdict.next_action == "rerun_with_more_effort":
        return verdict.model_copy(
            update={
                "next_action": (
                    "complete" if verdict.run_is_trustworthy else "escalate_to_scientist"
                )
            }
        )
    return verdict


def unjudged_compound_hold_reason(score_row: dict) -> str:
    affinity = score_row.get("best_affinity_kcal_per_mol")
    if affinity is None:
        return "Triage returned no judgement for this compound, so it is held rather than dropped."
    return (
        f"Triage returned no judgement for this compound, so it is held on its best affinity of "
        f"{affinity} kcal/mol rather than dropped."
    )


def hold_every_scored_compound_when_triage_judged_none(
    verdict: TriageVerdict, scores: list[dict]
) -> TriageVerdict:
    if verdict.compounds:
        return verdict
    held = [
        CompoundJudgement(
            compound_id=str(row["compound_id"]),
            disposition="hold",
            reason=unjudged_compound_hold_reason(row),
        )
        for row in scores
        if row.get("compound_id")
    ]
    if not held:
        return verdict
    return verdict.model_copy(update={"compounds": held})


def hold_the_control_compound_rather_than_promoting_it(
    verdict: TriageVerdict, control: ControlCheck
) -> TriageVerdict:
    if control.compound_name is None or not verdict.compounds:
        return verdict
    control_name = control.compound_name.casefold()
    return verdict.model_copy(
        update={
            "compounds": [
                judgement
                if judgement.disposition != "promote"
                or judgement.compound_id.casefold() != control_name
                else judgement.model_copy(
                    update={
                        "disposition": "hold",
                        "reason": (
                            f"{judgement.reason.rstrip()} Carried as the control reference rather "
                            f"than promoted: a control establishes whether the method reproduces a "
                            f"pose it is known to get right, so its own result is not a finding "
                            f"this run produced."
                        ),
                    }
                )
                for judgement in verdict.compounds
            ]
        }
    )


def hold_promotions_when_results_do_not_discriminate(verdict: TriageVerdict) -> TriageVerdict:
    if verdict.results_discriminate or not verdict.compounds:
        return verdict
    return verdict.model_copy(
        update={
            "compounds": [
                judgement
                if judgement.disposition != "promote"
                else judgement.model_copy(
                    update={
                        "disposition": "hold",
                        "reason": (
                            f"{judgement.reason.rstrip()} Held rather than promoted because this "
                            f"run did not separate the compounds from one another, so no compound "
                            f"in it is distinguishable as a hit."
                        ),
                    }
                )
                for judgement in verdict.compounds
            ]
        }
    )


def compounds_carried_to_next_stage(
    verdict: TriageVerdict,
) -> tuple[list[CompoundJudgement], TriageDisposition]:
    promoted = [judgement for judgement in verdict.compounds if judgement.disposition == "promote"]
    if promoted:
        return promoted, "promote"
    return [judgement for judgement in verdict.compounds if judgement.disposition == "hold"], "hold"


def stage_input_readiness(
    stage: str,
    available_inputs: frozenset[InputKind],
    compound_count: int,
    already_run: dict[str, str],
) -> StageInputReadiness:
    unmet = sorted(unmet_inputs_for_stage(stage, available_inputs))
    statuses = [
        StageInputStatus(kind=kind, resolves=kind not in unmet)
        for kind in sorted(required_inputs_for_stage(stage))
    ]
    blocking_reason = (
        unmet_requirement_question(unmet[0], stage)
        if unmet
        else unsupported_stage_rationale(stage, compound_count)
    )
    return StageInputReadiness(
        stage=stage,
        inputs_resolve=blocking_reason is None,
        inputs=statuses,
        blocking_reason=blocking_reason,
        already_run_as=already_run.get(stage),
    )


def readiness_for_every_stage(
    available_inputs: frozenset[InputKind],
    compound_count: int,
    already_run: dict[str, str] | None = None,
) -> list[StageInputReadiness]:
    already_run = already_run or {}
    return [
        stage_input_readiness(stage, available_inputs, compound_count, already_run)
        for stage in get_args(Workload)
    ]


def stage_choice_rejection_reason(
    decision: StageDecision, request: StageDecisionRequest
) -> str | None:
    if decision.chosen_stage is None:
        return None
    readiness = {item.stage: item for item in request.stage_readiness}
    chosen = readiness.get(decision.chosen_stage)
    if chosen is None:
        return (
            f"{decision.chosen_stage} is not a stage CASCADE has a workload container for. "
            f"The stages it can run are {', '.join(sorted(readiness))}."
        )
    if not chosen.inputs_resolve:
        return chosen.blocking_reason or (
            f"{decision.chosen_stage} cannot run because an input it needs does not resolve."
        )
    if request.run_is_trustworthy is False:
        return (
            f"The {request.completed_stage} run was not judged trustworthy, so nothing may be "
            f"built on top of it. Choose nothing."
        )
    if request.decision_point == "next_stage" and not request.carried_compounds:
        return (
            f"No compound survived the {request.completed_stage} stage, so "
            f"{decision.chosen_stage} would have nothing to run on. Choose nothing."
        )
    return None


def blocked_stages_from_readiness(
    readiness: list[StageInputReadiness], chosen_stage: str | None
) -> list[BlockedStage]:
    return [
        BlockedStage(
            workload=item.stage,
            reason=item.blocking_reason or "its inputs do not resolve",
        )
        for item in readiness
        if not item.inputs_resolve and item.stage != chosen_stage
    ]


def runnable_stages_from_readiness(
    readiness: list[StageInputReadiness], completed_stage: str | None
) -> list[str]:
    return [
        item.stage for item in readiness if item.inputs_resolve and item.stage != completed_stage
    ]


def proposal_decision_from_stage_decision(
    completed: CompletedStage, request: StageDecisionRequest, decision: StageDecision
) -> ProposalDecision:
    verdict = completed.triaged.verdict
    completed_stage = completed.triaged.result.outcome.workload
    proposal_request = ProposalRequest(
        completed_stage=completed_stage,
        target_name=completed.target_name,
        run_is_trustworthy=verdict.run_is_trustworthy,
        results_discriminate=verdict.results_discriminate,
        triage_headline=verdict.headline,
        carried_compounds=request.carried_compounds,
        carried_disposition=request.carried_disposition or "hold",
        runnable_next_stages=runnable_stages_from_readiness(
            request.stage_readiness, completed_stage
        ),
        blocked_next_stages=blocked_stages_from_readiness(
            request.stage_readiness, decision.chosen_stage
        ),
    )
    return ProposalDecision(
        completed=completed,
        request=proposal_request,
        proposal=StageProposal(
            next_stage=decision.chosen_stage,
            card_title=decision.card_title,
            reason=decision.reason,
            rationale=decision.rationale,
        ),
    )


def stage_blocking_reasons_for_requested_stages(
    intent: CampaignIntent, request: StageDecisionRequest
) -> list[str]:
    readiness = {item.stage: item for item in request.stage_readiness}
    reasons = [
        readiness[stage].blocking_reason
        for stage in intent.requested_stages
        if stage in readiness and not readiness[stage].inputs_resolve
    ]
    return [reason for reason in reasons if reason]
