import csv
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from job_spec import JobSpec, StabilityParams
from stability import (
    STABILITY_CAVEAT,
    PoseFailure,
    PoseStabilitySummary,
    pose_stability_separates_compounds,
    rank_by_pose_stability,
    stability_verdict_counts,
)

RESULTS_FILE_NAME = "results.json"
RESULTS_ARCHIVE_NAME = "results.zip"
STABILITY_CSV_FILE_NAME = "stability.csv"
TRAJECTORY_DIRECTORY_NAME = "trajectories"
VERDICT_STABLE = "stable"

REPORTED_COLUMNS = (
    "compound_id",
    "verdict",
    "final_rmsd_angstrom",
    "mean_rmsd_angstrom",
    "maximum_rmsd_angstrom",
    "final_contact_retention",
    "mean_contact_retention",
    "sustained_contact_retention",
    "frames",
    "heavy_atoms",
    "affinity_rank",
    "best_affinity_kcal_per_mol",
)


@dataclass(frozen=True)
class ControlPoseSummary:
    requested_name: str | None
    status: str
    verdict: str | None
    detail: str
    final_rmsd_angstrom: float | None = None


def measure_control_compound(
    spec: JobSpec, summaries: list[PoseStabilitySummary]
) -> ControlPoseSummary:
    requested = spec.control_compound
    if not requested:
        return ControlPoseSummary(
            requested_name=None,
            status="not_requested",
            verdict=None,
            detail="",
        )
    by_name = {summary.compound_id.lower(): summary for summary in summaries}
    matched = by_name.get(requested.strip().lower())
    if matched is None:
        return ControlPoseSummary(
            requested_name=requested,
            status="not_found",
            verdict=None,
            detail=f"{requested} was not among the poses that reached simulation",
        )
    return ControlPoseSummary(
        requested_name=requested,
        status="measured",
        verdict=matched.verdict,
        detail=(
            f"the control pose finished {matched.final_rmsd_angstrom} A from its docked pose and "
            f"retained {matched.sustained_contact_retention:.0%} of its receptor contacts"
        ),
        final_rmsd_angstrom=matched.final_rmsd_angstrom,
    )


def stability_record(summary: PoseStabilitySummary) -> dict:
    return {
        "compound_id": summary.compound_id,
        "verdict": summary.verdict,
        "reasons": summary.reasons,
        "frames": summary.frames,
        "final_rmsd_angstrom": summary.final_rmsd_angstrom,
        "mean_rmsd_angstrom": summary.mean_rmsd_angstrom,
        "maximum_rmsd_angstrom": summary.maximum_rmsd_angstrom,
        "final_contact_retention": summary.final_contact_retention,
        "mean_contact_retention": summary.mean_contact_retention,
        "sustained_contact_retention": summary.sustained_contact_retention,
        "heavy_atoms": summary.heavy_atoms,
        "affinity_rank": summary.affinity_rank,
        "best_affinity_kcal_per_mol": summary.best_affinity_kcal_per_mol,
    }


def write_stability_csv(summaries: list[PoseStabilitySummary], directory: Path) -> Path:
    destination = directory / STABILITY_CSV_FILE_NAME
    with destination.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORTED_COLUMNS)
        for summary in rank_by_pose_stability(summaries):
            record = stability_record(summary)
            writer.writerow([record[column] for column in REPORTED_COLUMNS])
    return destination


def write_rmsd_series(summaries_by_name: dict[str, list[float]], directory: Path) -> Path:
    trajectory_directory = directory / TRAJECTORY_DIRECTORY_NAME
    trajectory_directory.mkdir(parents=True, exist_ok=True)
    for compound_id, series in summaries_by_name.items():
        destination = trajectory_directory / f"{compound_id}_rmsd.csv"
        with destination.open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("frame", "rmsd_angstrom"))
            for frame, value in enumerate(series, start=1):
                writer.writerow((frame, value))
    return trajectory_directory


def build_run_summary(
    spec: JobSpec,
    params: StabilityParams,
    summaries: list[PoseStabilitySummary],
    failures: list[PoseFailure],
    control: ControlPoseSummary,
    platform_name: str,
) -> dict:
    ranked = rank_by_pose_stability(summaries)
    return {
        "run_id": spec.run_id,
        "workload": spec.workload,
        "status": "succeeded",
        "method": {
            "engine": "openmm",
            "protein_force_field": "amber14-all",
            "ligand_force_field": "gaff",
            "solvent": "implicit gbn2",
            "platform": platform_name,
            "production_picoseconds": params.production_picoseconds,
            "receptor_restrained": True,
            "caveat": STABILITY_CAVEAT,
        },
        "target": {
            "source": spec.target.source,
            "reference": spec.target.reference,
            "pdb_id": spec.target.pdb_id,
        },
        "params": params.model_dump(),
        "poses": {
            "simulated": len(summaries),
            "failed": len(failures),
            "failures": [
                {"compound_id": failure.name, "reason": failure.reason} for failure in failures
            ],
        },
        "verdict_counts": stability_verdict_counts(summaries),
        "control_compound": {
            "requested_name": control.requested_name,
            "status": control.status,
            "verdict": control.verdict,
            "detail": control.detail,
            "final_rmsd_angstrom": control.final_rmsd_angstrom,
        },
        "stability_analysis": {
            "pose_count": len(summaries),
            "pose_drift_threshold_angstrom": params.pose_drift_threshold_angstrom,
            "contact_retention_threshold": params.contact_retention_threshold,
            "contact_cutoff_angstrom": params.contact_cutoff_angstrom,
            "contact_break_cutoff_angstrom": params.contact_break_cutoff_angstrom,
            "results_separate_compounds": pose_stability_separates_compounds(summaries),
            "most_stable_compound": ranked[0].compound_id if ranked else None,
            "caveat": STABILITY_CAVEAT,
        },
        "trajectories": [stability_record(summary) for summary in ranked],
    }


def write_run_summary(summary: dict, directory: Path) -> Path:
    destination = directory / RESULTS_FILE_NAME
    destination.write_text(json.dumps(summary, indent=2))
    return destination


def write_results_archive(outputs_directory: Path, workspace: Path) -> Path:
    archive_base = workspace / RESULTS_ARCHIVE_NAME.removesuffix(".zip")
    return Path(shutil.make_archive(str(archive_base), "zip", root_dir=outputs_directory))
