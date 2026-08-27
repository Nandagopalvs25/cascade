import csv
import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from docking import LigandDockingResult
from job_spec import BindingSite, DockingParams, JobSpec
from ligands import LigandFailure
from score_analysis import (
    ScoredCompound,
    analyze_docking_scores,
    ligand_efficiency,
    size_independent_ligand_efficiency,
)

SCORES_FILE_NAME = "scores.csv"
RESULTS_FILE_NAME = "results.json"
RESULTS_ARCHIVE_NAME = "results.zip"
POSES_DIRECTORY_NAME = "poses"
REFERENCE_LIGAND_FILE_NAME = "reference_ligand.pdb"

UNSAFE_FILE_STEM_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ReceptorSummary:
    structure_uri: str
    requested_chain: str | None
    chains_kept: list[str]
    atom_count: int


@dataclass
class BindingSiteSummary:
    binding_site: BindingSite
    origin: str
    cocrystal_ligand: str | None = None


@dataclass
class ControlCompoundSummary:
    requested_name: str | None = None
    status: str = "not_requested"
    detail: str = ""
    best_affinity: float | None = None
    rmsd_to_cocrystal_angstrom: float | None = None
    lowest_mode_rmsd_angstrom: float | None = None
    lowest_mode_rank: int | None = None


def safe_file_stem(name: str) -> str:
    cleaned = UNSAFE_FILE_STEM_CHARACTERS.sub("_", name).strip("_")
    return cleaned or "ligand"


def rank_results_by_affinity(results: list[LigandDockingResult]) -> list[LigandDockingResult]:
    return sorted(results, key=lambda result: result.best_affinity)


def pose_file_relative_path(rank: int, compound_name: str) -> str:
    return f"{POSES_DIRECTORY_NAME}/{rank:03d}_{safe_file_stem(compound_name)}_best.pdbqt"


def write_scores_csv(results: list[LigandDockingResult], directory: Path) -> Path:
    destination = directory / SCORES_FILE_NAME
    with destination.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "rank",
                "compound_id",
                "best_affinity_kcal_per_mol",
                "heavy_atom_count",
                "ligand_efficiency",
                "size_independent_ligand_efficiency",
                "mode_count",
                "mode_affinity_spread",
                "pose_file",
            ]
        )
        for rank, result in enumerate(rank_results_by_affinity(results), start=1):
            writer.writerow(
                [
                    rank,
                    result.name,
                    f"{result.best_affinity:.3f}",
                    result.heavy_atom_count,
                    ligand_efficiency(result.best_affinity, result.heavy_atom_count),
                    size_independent_ligand_efficiency(
                        result.best_affinity, result.heavy_atom_count
                    ),
                    result.mode_count,
                    f"{result.mode_affinity_spread:.3f}",
                    pose_file_relative_path(rank, result.name),
                ]
            )
    return destination


def write_best_pose_files(results: list[LigandDockingResult], directory: Path) -> Path:
    poses_directory = directory / POSES_DIRECTORY_NAME
    poses_directory.mkdir(parents=True, exist_ok=True)
    for rank, result in enumerate(rank_results_by_affinity(results), start=1):
        pose_path = directory / pose_file_relative_path(rank, result.name)
        pose_path.write_text(result.best_pose_pdbqt)
    return poses_directory


def write_reference_ligand_pdb(pdb_block: str, directory: Path) -> Path:
    destination = directory / REFERENCE_LIGAND_FILE_NAME
    destination.write_text(pdb_block)
    return destination


def score_analysis_for_results(
    results: list[LigandDockingResult],
    params: DockingParams,
    control: ControlCompoundSummary,
) -> dict | None:
    analysis = analyze_docking_scores(
        [
            ScoredCompound(
                name=result.name,
                best_affinity=result.best_affinity,
                heavy_atom_count=result.heavy_atom_count,
            )
            for result in results
        ],
        control_compound_name=control.requested_name,
        scoring_function_error_kcal_per_mol=params.scoring_function_error_kcal_per_mol,
    )
    return asdict(analysis) if analysis is not None else None


def build_run_summary(
    spec: JobSpec,
    params: DockingParams,
    receptor: ReceptorSummary,
    site: BindingSiteSummary,
    control: ControlCompoundSummary,
    results: list[LigandDockingResult],
    failures: list[LigandFailure],
) -> dict:
    ranked = rank_results_by_affinity(results)
    return {
        "run_id": spec.run_id,
        "workload": spec.workload,
        "status": "succeeded",
        "target": {
            "source": spec.target.source,
            "reference": spec.target.reference,
            "pdb_id": spec.target.pdb_id,
            "structure_uri": receptor.structure_uri,
            "requested_chain": receptor.requested_chain,
            "chains_docked": receptor.chains_kept,
            "receptor_atom_count": receptor.atom_count,
        },
        "binding_site": {
            **site.binding_site.model_dump(),
            "origin": site.origin,
            "cocrystal_ligand": site.cocrystal_ligand,
        },
        "params": params.model_dump(),
        "ligands": {
            "docked": len(results),
            "failed": len(failures),
            "failures": [
                {"compound_id": failure.name, "reason": failure.reason} for failure in failures
            ],
        },
        "control_compound": {
            "requested_name": control.requested_name,
            "status": control.status,
            "detail": control.detail,
            "best_affinity_kcal_per_mol": control.best_affinity,
            "rmsd_to_cocrystal_angstrom": control.rmsd_to_cocrystal_angstrom,
            "lowest_mode_rmsd_angstrom": control.lowest_mode_rmsd_angstrom,
            "lowest_mode_rank": control.lowest_mode_rank,
        },
        "scores": [
            {
                "rank": rank,
                "compound_id": result.name,
                "best_affinity_kcal_per_mol": result.best_affinity,
                "heavy_atom_count": result.heavy_atom_count,
                "ligand_efficiency": ligand_efficiency(
                    result.best_affinity, result.heavy_atom_count
                ),
                "size_independent_ligand_efficiency": size_independent_ligand_efficiency(
                    result.best_affinity, result.heavy_atom_count
                ),
                "mode_affinities": result.mode_affinities,
                "mode_affinity_spread": result.mode_affinity_spread,
            }
            for rank, result in enumerate(ranked, start=1)
        ],
        "score_analysis": score_analysis_for_results(results, params, control),
    }


def write_run_summary(summary: dict, directory: Path) -> Path:
    destination = directory / RESULTS_FILE_NAME
    destination.write_text(json.dumps(summary, indent=2))
    return destination


def write_results_archive(outputs_directory: Path, workspace: Path) -> Path:
    archive_base = workspace / RESULTS_ARCHIVE_NAME.removesuffix(".zip")
    return Path(shutil.make_archive(str(archive_base), "zip", root_dir=outputs_directory))
