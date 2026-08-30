import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from job_spec import FoldParams

LOGGER = logging.getLogger("cascade.cofold.protenix")

PROTENIX_EXECUTABLE = "protenix"
INPUT_FILE_NAME = "protenix_input.json"
STRUCTURE_SUFFIX = ".cif"
SUMMARY_CONFIDENCE_MARKER = "_summary_confidence_sample_"


class ProtenixExecutionError(Exception):
    pass


@dataclass
class ComplexRequest:
    name: str
    compound_name: str
    smiles: str


def build_protenix_input(
    protein_sequence: str, complexes: list[ComplexRequest], chain_count: int = 1
) -> list[dict]:
    return [
        {
            "name": request.name,
            "sequences": [
                {"proteinChain": {"sequence": protein_sequence, "count": chain_count}},
                {"ligand": {"ligand": request.smiles, "count": 1}},
            ],
        }
        for request in complexes
    ]


def write_protenix_input(payload: list[dict], workspace: Path) -> Path:
    destination = workspace / INPUT_FILE_NAME
    destination.write_text(json.dumps(payload, indent=2))
    return destination


def protenix_prediction_command(
    input_path: Path, output_directory: Path, params: FoldParams
) -> list[str]:
    command = [
        PROTENIX_EXECUTABLE,
        "pred",
        "--input",
        str(input_path),
        "--out_dir",
        str(output_directory),
        "--model_name",
        params.model_name,
        "--seeds",
        params.seeds_argument,
        "--cycle",
        str(params.cycles),
        "--step",
        str(params.diffusion_steps),
        "--sample",
        str(params.samples_per_seed),
        "--dtype",
        params.dtype,
    ]
    command += ["--use_msa", "true" if params.use_msa else "false"]
    return command


def run_protenix_prediction(input_path: Path, output_directory: Path, params: FoldParams) -> Path:
    if shutil.which(PROTENIX_EXECUTABLE) is None:
        raise ProtenixExecutionError(f"{PROTENIX_EXECUTABLE} is not available in this image")
    output_directory.mkdir(parents=True, exist_ok=True)
    command = protenix_prediction_command(input_path, output_directory, params)
    LOGGER.info("running %s", " ".join(command))
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=params.prediction_timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise ProtenixExecutionError(
            f"protenix prediction failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()[-1500:]}"
        )
    if not any(output_directory.rglob(f"*{STRUCTURE_SUFFIX}")):
        raise ProtenixExecutionError("protenix produced no predicted structure files")
    return output_directory


def summary_confidence_files(output_directory: Path) -> list[Path]:
    return sorted(
        path for path in output_directory.rglob("*.json") if SUMMARY_CONFIDENCE_MARKER in path.name
    )


def predicted_structure_files(output_directory: Path) -> list[Path]:
    return sorted(output_directory.rglob(f"*{STRUCTURE_SUFFIX}"))
