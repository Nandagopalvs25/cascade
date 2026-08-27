import uuid
from typing import get_args

from cascade.agents.schemas import (
    BlockedStage,
    CompoundJudgement,
    ControlCheck,
    TriageDisposition,
    TriageVerdict,
    WorkloadParams,
    WorkloadPlan,
)
from cascade.config import get_settings
from cascade.schemas import Workload

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
    "md_stability": frozenset(),
}

GPU_WORKLOADS = frozenset({"md_stability", "cofold"})

IMPLEMENTED_WORKLOADS = frozenset({"dock", "admet", "cofold"})


def allowed_job_params_for_workload(workload: str) -> frozenset[str]:
    return ALLOWED_JOB_PARAMS_BY_WORKLOAD.get(workload, frozenset())


COMPOUND_RECORD_KEY_BY_WORKLOAD = {
    "dock": "scores",
    "admet": "assessments",
    "cofold": "predictions",
}


def compound_records_from_manifest(workload: str, manifest: dict) -> list[dict]:
    key = COMPOUND_RECORD_KEY_BY_WORKLOAD.get(workload)
    if key is None:
        return []
    return manifest.get(key) or []


def executor_for_workload(workload: str, compound_count: int) -> str:
    if workload in GPU_WORKLOADS or compound_count > settings.max_ligands_per_cloud_run_job:
        return "cloud_batch"
    return "cloud_run_job"


def unsupported_stage_rationale(workload: str, compound_count: int) -> str | None:
    if workload not in IMPLEMENTED_WORKLOADS:
        return (
            f"{workload} has no workload container yet, so CASCADE cannot run this stage. "
            f"Only {', '.join(sorted(IMPLEMENTED_WORKLOADS))} have containers."
        )
    if workload in GPU_WORKLOADS:
        return (
            f"{workload} needs a GPU executor. Its container is built, but CASCADE only submits "
            f"to Cloud Run Jobs today and GPU submission is not wired up yet."
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


def escalated_docking_params(params: WorkloadParams) -> WorkloadParams:
    return params.model_copy(
        update={"conformers_per_ligand": settings.control_rerun_conformers_per_ligand}
    )


def plan_escalated_after_control_failure(plan: WorkloadPlan) -> WorkloadPlan:
    escalated = escalated_docking_params(plan.params)
    return plan.model_copy(
        update={
            "params": escalated,
            "rationale": (
                "The control compound did not reproduce its crystallographic pose, so the "
                "starting geometry is the suspect rather than the search. CASCADE raised "
                f"conformers_per_ligand to {escalated.conformers_per_ligand} and is re-running "
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


def compounds_carried_to_next_stage(
    verdict: TriageVerdict,
) -> tuple[list[CompoundJudgement], TriageDisposition]:
    promoted = [judgement for judgement in verdict.compounds if judgement.disposition == "promote"]
    if promoted:
        return promoted, "promote"
    return [judgement for judgement in verdict.compounds if judgement.disposition == "hold"], "hold"


def already_run_stage_rationale(candidate: str, already_run: dict[str, str]) -> str | None:
    previous_run_id = already_run.get(candidate)
    if previous_run_id is None:
        return None
    return (
        f"{candidate} already ran on these compounds in run `{previous_run_id}`, so re-running it "
        "on the same inputs repeats work already done rather than adding information."
    )


def next_stage_options(
    completed_stage: str, compound_count: int, already_run: dict[str, str] | None = None
) -> tuple[list[str], list[BlockedStage]]:
    already_run = already_run or {}
    runnable: list[str] = []
    blocked: list[BlockedStage] = []
    for candidate in get_args(Workload):
        if candidate == completed_stage:
            continue
        reason = already_run_stage_rationale(candidate, already_run) or unsupported_stage_rationale(
            candidate, compound_count
        )
        if reason is None:
            runnable.append(candidate)
        else:
            blocked.append(BlockedStage(workload=candidate, reason=reason))
    return runnable, blocked
