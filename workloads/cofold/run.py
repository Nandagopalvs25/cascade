import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from artifacts import ArtifactStore, publish_job_completion
from compounds import canonical_smiles, load_compound_library
from confidence import best_prediction_per_compound, load_complex_predictions
from job_spec import FoldParams, JobSpec, require_target_structure
from protenix_job import (
    ComplexRequest,
    build_protenix_input,
    predicted_structure_files,
    run_protenix_prediction,
    summary_confidence_files,
    write_protenix_input,
)
from results import (
    PROTENIX_OUTPUT_DIRECTORY_NAME,
    RESULTS_ARCHIVE_NAME,
    RESULTS_FILE_NAME,
    ComplexFailure,
    build_run_summary,
    copy_predicted_structures,
    measure_control_compound,
    write_confidence_csv,
    write_results_archive,
    write_run_summary,
)
from sequences import protein_sequence_from_structure_text

LOGGER = logging.getLogger("cascade.cofold")

SPEC_URI_ENVIRONMENT_VARIABLE = "SPEC_URI"
RUN_ID_ENVIRONMENT_VARIABLE = "RUN_ID"
WORKLOAD = "cofold"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def read_job_spec(store: ArtifactStore, spec_uri: str) -> JobSpec:
    spec = JobSpec.model_validate_json(store.read_text(spec_uri))
    if spec.workload != WORKLOAD:
        raise ValueError(f"the cofold container cannot run workload {spec.workload!r}")
    return spec


def compound_library_file_name(ligands_uri: str) -> str:
    name = ligands_uri.rstrip("/").rsplit("/", maxsplit=1)[-1]
    return name or "ligands.smi"


def complex_name_for_compound(index: int, compound_name: str) -> str:
    safe = "".join(character if character.isalnum() else "_" for character in compound_name).strip(
        "_"
    )
    return f"{index:03d}_{safe or 'compound'}"


def resolve_protein_sequence(store: ArtifactStore, spec: JobSpec, params: FoldParams) -> str:
    if params.protein_sequence:
        return params.protein_sequence.strip().upper()
    structure_text = store.read_text(spec.target.structure_uri)
    return protein_sequence_from_structure_text(structure_text, chain=spec.target.chain)


def run_cofold_job(store: ArtifactStore, spec: JobSpec, workspace: Path) -> dict:
    require_target_structure(spec)
    params = FoldParams.from_job_spec(spec)
    LOGGER.info(
        "run %s: co-folding against %s with protenix model %s",
        spec.run_id,
        spec.target.reference,
        params.model_name,
    )

    protein_sequence = resolve_protein_sequence(store, spec, params)
    LOGGER.info("run %s: protein sequence has %s residues", spec.run_id, len(protein_sequence))

    library_path = store.download_to_file(
        spec.ligands_uri, workspace / compound_library_file_name(spec.ligands_uri)
    )
    records, load_failures = load_compound_library(library_path)
    if not records:
        raise ValueError(f"no usable compounds in {spec.ligands_uri}")

    selected = records[: params.max_complexes]
    skipped = [
        ComplexFailure(
            name=record.name,
            reason=f"exceeded the {params.max_complexes} complex ceiling for one fold job",
        )
        for record in records[params.max_complexes :]
    ]

    requests: list[ComplexRequest] = []
    compound_name_by_complex: dict[str, str] = {}
    for index, record in enumerate(selected, start=1):
        complex_name = complex_name_for_compound(index, record.name)
        compound_name_by_complex[complex_name] = record.name
        requests.append(
            ComplexRequest(
                name=complex_name,
                compound_name=record.name,
                smiles=canonical_smiles(record.mol),
            )
        )

    payload = build_protenix_input(protein_sequence, requests)
    input_path = write_protenix_input(payload, workspace)
    protenix_output = run_protenix_prediction(input_path, workspace / "protenix", params)
    LOGGER.info(
        "run %s: protenix wrote %s structures",
        spec.run_id,
        len(predicted_structure_files(protenix_output)),
    )

    summaries = summary_confidence_files(protenix_output)
    if not summaries:
        raise ValueError("protenix produced no confidence summaries to rank")
    predictions = load_complex_predictions(summaries, compound_name_by_complex)
    ranked = best_prediction_per_compound(predictions)
    if not ranked:
        raise ValueError("no co-folded complex could be ranked")

    folded_names = {prediction.compound_name for prediction in ranked}
    missing = [
        ComplexFailure(name=record.name, reason="protenix returned no confidence summary")
        for record in selected
        if record.name not in folded_names
    ]
    failures = [
        *[ComplexFailure(name=f.name, reason=f.reason) for f in load_failures],
        *skipped,
        *missing,
    ]

    control = measure_control_compound(spec, ranked)
    LOGGER.info(
        "run %s: control compound %s status %s rank %s ranking_score %s",
        spec.run_id,
        control.requested_name,
        control.status,
        control.rank,
        control.ranking_score,
    )

    outputs_directory = workspace / "outputs"
    outputs_directory.mkdir(parents=True, exist_ok=True)
    copy_predicted_structures(ranked, outputs_directory)
    write_confidence_csv(ranked, outputs_directory)
    shutil.copytree(
        protenix_output, outputs_directory / PROTENIX_OUTPUT_DIRECTORY_NAME, dirs_exist_ok=True
    )
    summary = build_run_summary(spec, params, protein_sequence, ranked, failures, control)
    write_run_summary(summary, outputs_directory)

    uploaded = store.upload_directory(outputs_directory, spec.output_uri)
    LOGGER.info("run %s: uploaded %s output files", spec.run_id, len(uploaded))

    output_prefix = spec.output_uri.rstrip("/")
    archive_path = write_results_archive(outputs_directory, workspace)
    results_archive_uri = store.upload_file(archive_path, f"{output_prefix}/{RESULTS_ARCHIVE_NAME}")

    best = summary["predictions"][0]
    return {
        "run_id": spec.run_id,
        "workload": spec.workload,
        "status": "succeeded",
        "exit_code": 0,
        "results_uri": output_prefix,
        "results_manifest_uri": f"{output_prefix}/{RESULTS_FILE_NAME}",
        "results_archive_uri": results_archive_uri,
        "summary": {
            "complexes_folded": len(ranked),
            "complexes_failed": len(failures),
            "protenix_model": params.model_name,
            "sequence_length": len(protein_sequence),
            "best_compound_id": best["compound_id"],
            "best_ranking_score": best["ranking_score"],
            "control_compound": summary["control_compound"],
        },
    }


def main() -> int:
    configure_logging()
    spec_uri = os.environ.get(SPEC_URI_ENVIRONMENT_VARIABLE)
    if not spec_uri:
        LOGGER.error("%s is not set", SPEC_URI_ENVIRONMENT_VARIABLE)
        return 1

    store = ArtifactStore()
    run_id = os.environ.get(RUN_ID_ENVIRONMENT_VARIABLE, "")
    try:
        with tempfile.TemporaryDirectory(prefix="cascade-fold-") as workspace:
            spec = read_job_spec(store, spec_uri)
            run_id = spec.run_id
            completion = run_cofold_job(store, spec, Path(workspace))
    except Exception as error:
        LOGGER.exception("cofold job failed")
        publish_job_completion(
            {
                "run_id": run_id,
                "workload": WORKLOAD,
                "status": "failed",
                "exit_code": 1,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        return 1

    publish_job_completion(completion)
    return 0


if __name__ == "__main__":
    sys.exit(main())
