import csv
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from confidence import ComplexPrediction
from job_spec import FoldParams, JobSpec

CONFIDENCE_FILE_NAME = "confidence.csv"
RESULTS_FILE_NAME = "results.json"
RESULTS_ARCHIVE_NAME = "results.zip"
STRUCTURES_DIRECTORY_NAME = "structures"
PROTENIX_OUTPUT_DIRECTORY_NAME = "protenix_raw"

AFFINITY_CAVEAT = (
    "Protenix predicts complex structure and per-prediction confidence, not binding free "
    "energy. ranking_score is a structural confidence score and must not be read as a "
    "predicted affinity in kcal/mol."
)

REPORTED_METRIC_COLUMNS = ("ranking_score", "plddt", "ptm", "iptm", "has_clash")


@dataclass
class ComplexFailure:
    name: str
    reason: str


@dataclass
class ControlCompoundSummary:
    requested_name: str | None = None
    status: str = "not_requested"
    ranking_score: float | None = None
    rank: int | None = None
    detail: str = ""


def measure_control_compound(
    spec: JobSpec, ranked: list[ComplexPrediction]
) -> ControlCompoundSummary:
    if spec.control_compound is None:
        return ControlCompoundSummary()

    requested = spec.control_compound.strip()
    lowered = requested.lower()
    for rank, prediction in enumerate(ranked, start=1):
        if prediction.compound_name.strip().lower() == lowered:
            return ControlCompoundSummary(
                requested_name=requested,
                status="measured",
                ranking_score=prediction.ranking_score,
                rank=rank,
                detail=(
                    f"the control co-folded complex ranks {rank} of {len(ranked)} by "
                    "structural confidence"
                ),
            )
    return ControlCompoundSummary(
        requested_name=requested,
        status="not_in_library",
        detail="no co-folded compound carries the control compound name",
    )


def copy_predicted_structures(ranked: list[ComplexPrediction], directory: Path) -> Path:
    structures_directory = directory / STRUCTURES_DIRECTORY_NAME
    structures_directory.mkdir(parents=True, exist_ok=True)
    for rank, prediction in enumerate(ranked, start=1):
        if prediction.structure_path is None:
            continue
        destination = structures_directory / structure_file_relative_name(
            rank, prediction.compound_name
        )
        shutil.copyfile(prediction.structure_path, destination)
    return structures_directory


def structure_file_relative_name(rank: int, compound_name: str) -> str:
    safe = "".join(character if character.isalnum() else "_" for character in compound_name)
    return f"{rank:03d}_{safe.strip('_') or 'complex'}.cif"


def write_confidence_csv(ranked: list[ComplexPrediction], directory: Path) -> Path:
    destination = directory / CONFIDENCE_FILE_NAME
    with destination.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "compound_id", *REPORTED_METRIC_COLUMNS, "seed", "structure_file"])
        for rank, prediction in enumerate(ranked, start=1):
            writer.writerow(
                [
                    rank,
                    prediction.compound_name,
                    *[prediction.metrics.get(column, "") for column in REPORTED_METRIC_COLUMNS],
                    prediction.seed if prediction.seed is not None else "",
                    f"{STRUCTURES_DIRECTORY_NAME}/"
                    f"{structure_file_relative_name(rank, prediction.compound_name)}",
                ]
            )
    return destination


def build_run_summary(
    spec: JobSpec,
    params: FoldParams,
    protein_sequence: str,
    ranked: list[ComplexPrediction],
    failures: list[ComplexFailure],
    control: ControlCompoundSummary,
) -> dict:
    return {
        "run_id": spec.run_id,
        "workload": spec.workload,
        "status": "succeeded",
        "method": {
            "engine": "protenix",
            "model_name": params.model_name,
            "use_msa": params.use_msa,
            "seeds": params.seeds,
            "cycles": params.cycles,
            "diffusion_steps": params.diffusion_steps,
            "samples_per_seed": params.samples_per_seed,
            "caveat": AFFINITY_CAVEAT,
        },
        "target": {
            "source": spec.target.source,
            "reference": spec.target.reference,
            "pdb_id": spec.target.pdb_id,
            "requested_chain": spec.target.chain,
            "sequence_length": len(protein_sequence),
        },
        "params": params.model_dump(),
        "compounds": {
            "folded": len(ranked),
            "failed": len(failures),
            "failures": [
                {"compound_id": failure.name, "reason": failure.reason} for failure in failures
            ],
        },
        "control_compound": {
            "requested_name": control.requested_name,
            "status": control.status,
            "ranking_score": control.ranking_score,
            "rank": control.rank,
            "detail": control.detail,
        },
        "predictions": [
            {
                "rank": rank,
                "compound_id": prediction.compound_name,
                "ranking_score": prediction.ranking_score,
                "metrics": prediction.metrics,
                "seed": prediction.seed,
                "protenix_sample_rank": prediction.sample_rank,
                "structure_file": (
                    f"{STRUCTURES_DIRECTORY_NAME}/"
                    f"{structure_file_relative_name(rank, prediction.compound_name)}"
                ),
            }
            for rank, prediction in enumerate(ranked, start=1)
        ],
    }


def write_run_summary(summary: dict, directory: Path) -> Path:
    destination = directory / RESULTS_FILE_NAME
    destination.write_text(json.dumps(summary, indent=2))
    return destination


def write_results_archive(outputs_directory: Path, workspace: Path) -> Path:
    archive_base = workspace / RESULTS_ARCHIVE_NAME.removesuffix(".zip")
    return Path(shutil.make_archive(str(archive_base), "zip", root_dir=outputs_directory))
