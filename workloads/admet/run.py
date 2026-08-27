import logging
import os
import sys
import tempfile
from pathlib import Path

from artifacts import ArtifactStore, publish_job_completion
from assessment import VERDICT_FAIL, VERDICT_FLAG, VERDICT_PASS, assess_compound
from compounds import CompoundFailure, load_compound_library
from job_spec import AdmetParams, JobSpec
from results import (
    RESULTS_ARCHIVE_NAME,
    RESULTS_FILE_NAME,
    build_run_summary,
    measure_control_compound,
    write_assessments_csv,
    write_results_archive,
    write_run_summary,
)

LOGGER = logging.getLogger("cascade.admet")

SPEC_URI_ENVIRONMENT_VARIABLE = "SPEC_URI"
RUN_ID_ENVIRONMENT_VARIABLE = "RUN_ID"
WORKLOAD = "admet"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def read_job_spec(store: ArtifactStore, spec_uri: str) -> JobSpec:
    spec = JobSpec.model_validate_json(store.read_text(spec_uri))
    if spec.workload != WORKLOAD:
        raise ValueError(f"the admet container cannot run workload {spec.workload!r}")
    return spec


def compound_library_file_name(ligands_uri: str) -> str:
    name = ligands_uri.rstrip("/").rsplit("/", maxsplit=1)[-1]
    return name or "ligands.smi"


def run_admet_job(store: ArtifactStore, spec: JobSpec, workspace: Path) -> dict:
    params = AdmetParams.from_job_spec(spec)
    LOGGER.info("run %s: screening compounds from %s", spec.run_id, spec.ligands_uri)

    library_path = store.download_to_file(
        spec.ligands_uri, workspace / compound_library_file_name(spec.ligands_uri)
    )
    records, load_failures = load_compound_library(library_path)
    if not records:
        raise ValueError(f"no usable compounds in {spec.ligands_uri}")
    if len(records) > params.max_compounds:
        raise ValueError(
            f"{len(records)} compounds exceeds the {params.max_compounds} compound ceiling "
            "for a single admet job"
        )

    assessments = []
    assessment_failures: list[CompoundFailure] = []
    for record in records:
        try:
            assessments.append(assess_compound(record.name, record.mol, params))
        except Exception as error:
            assessment_failures.append(CompoundFailure(name=record.name, reason=str(error)))
    if not assessments:
        raise ValueError("every compound failed ADMET assessment")

    failures = [*load_failures, *assessment_failures]
    control = measure_control_compound(spec, assessments)
    summary = build_run_summary(spec, params, assessments, failures, control)
    counts = summary["verdict_counts"]
    LOGGER.info(
        "run %s: assessed %s compounds, %s pass, %s flag, %s fail, %s unusable",
        spec.run_id,
        len(assessments),
        counts[VERDICT_PASS],
        counts[VERDICT_FLAG],
        counts[VERDICT_FAIL],
        len(failures),
    )
    LOGGER.info(
        "run %s: control compound %s status %s verdict %s",
        spec.run_id,
        control.requested_name,
        control.status,
        control.verdict,
    )

    outputs_directory = workspace / "outputs"
    outputs_directory.mkdir(parents=True, exist_ok=True)
    write_assessments_csv(assessments, outputs_directory)
    write_run_summary(summary, outputs_directory)

    uploaded = store.upload_directory(outputs_directory, spec.output_uri)
    LOGGER.info("run %s: uploaded %s output files", spec.run_id, len(uploaded))

    output_prefix = spec.output_uri.rstrip("/")
    archive_path = write_results_archive(outputs_directory, workspace)
    results_archive_uri = store.upload_file(archive_path, f"{output_prefix}/{RESULTS_ARCHIVE_NAME}")

    promoted = [
        assessment["compound_id"]
        for assessment in summary["assessments"]
        if assessment["verdict"] == VERDICT_PASS
    ]
    return {
        "run_id": spec.run_id,
        "workload": spec.workload,
        "status": "succeeded",
        "exit_code": 0,
        "results_uri": output_prefix,
        "results_manifest_uri": f"{output_prefix}/{RESULTS_FILE_NAME}",
        "results_archive_uri": results_archive_uri,
        "summary": {
            "compounds_assessed": len(assessments),
            "compounds_failed": len(failures),
            "passed": counts[VERDICT_PASS],
            "flagged": counts[VERDICT_FLAG],
            "failed": counts[VERDICT_FAIL],
            "promoted_compound_ids": promoted,
            "liability_counts": summary["liability_counts"],
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
        with tempfile.TemporaryDirectory(prefix="cascade-admet-") as workspace:
            spec = read_job_spec(store, spec_uri)
            run_id = spec.run_id
            completion = run_admet_job(store, spec, Path(workspace))
    except Exception as error:
        LOGGER.exception("admet job failed")
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
