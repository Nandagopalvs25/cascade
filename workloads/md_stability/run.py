import logging
import os
import sys
import tempfile
from pathlib import Path

from artifacts import ArtifactStore, publish_job_completion
from job_spec import JobSpec, StabilityParams, require_target_structure
from poses import poses_for_simulation, read_docked_poses
from results import (
    RESULTS_ARCHIVE_NAME,
    RESULTS_FILE_NAME,
    VERDICT_STABLE,
    build_run_summary,
    measure_control_compound,
    write_results_archive,
    write_rmsd_series,
    write_run_summary,
    write_stability_csv,
)
from simulation import simulate_pose_stability
from stability import PoseFailure, summarize_pose_stability

LOGGER = logging.getLogger("cascade.md_stability")

SPEC_URI_ENVIRONMENT_VARIABLE = "SPEC_URI"
RUN_ID_ENVIRONMENT_VARIABLE = "RUN_ID"
WORKLOAD = "md_stability"
RECEPTOR_FILE_NAME = "receptor.pdb"
POSE_FILE_NAME = "poses.sdf"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def read_job_spec(store: ArtifactStore, spec_uri: str) -> JobSpec:
    spec = JobSpec.model_validate_json(store.read_text(spec_uri))
    if spec.workload != WORKLOAD:
        raise ValueError(f"the md_stability container cannot run workload {spec.workload!r}")
    return spec


def run_md_stability_job(store: ArtifactStore, spec: JobSpec, workspace: Path) -> dict:
    require_target_structure(spec)
    params = StabilityParams.from_job_spec(spec)
    LOGGER.info("run %s: simulating docked poses from %s", spec.run_id, spec.ligands_uri)

    pose_path = store.download_to_file(spec.ligands_uri, workspace / POSE_FILE_NAME)
    poses, load_failures = read_docked_poses(pose_path.read_text())
    if not poses:
        raise ValueError(f"no simulatable docked poses in {spec.ligands_uri}")
    selected = poses_for_simulation(poses, params.max_complexes)
    LOGGER.info("run %s: %s pose(s) read, simulating %s", spec.run_id, len(poses), len(selected))

    receptor_path = store.download_to_file(
        spec.target.structure_uri, workspace / RECEPTOR_FILE_NAME
    )

    summaries = []
    rmsd_series_by_compound: dict[str, list[float]] = {}
    simulation_failures: list[PoseFailure] = []
    platform_name = "unknown"
    for pose in selected:
        try:
            trajectory, platform_name = simulate_pose_stability(str(receptor_path), pose, params)
            summaries.append(
                summarize_pose_stability(
                    trajectory,
                    params.pose_drift_threshold_angstrom,
                    params.contact_retention_threshold,
                )
            )
            rmsd_series_by_compound[pose.name] = trajectory.rmsd_series_angstrom
        except Exception as error:
            LOGGER.exception("run %s: pose %s failed to simulate", spec.run_id, pose.name)
            simulation_failures.append(PoseFailure(name=pose.name, reason=str(error)))
    if not summaries:
        raise ValueError("every docked pose failed to simulate")

    failures = [*load_failures, *simulation_failures]
    control = measure_control_compound(spec, summaries)
    summary = build_run_summary(spec, params, summaries, failures, control, platform_name)
    counts = summary["verdict_counts"]
    LOGGER.info(
        "run %s: %s stable, %s drifted, %s unstable, %s unusable",
        spec.run_id,
        counts["stable"],
        counts["drifted"],
        counts["unstable"],
        len(failures),
    )

    outputs_directory = workspace / "outputs"
    outputs_directory.mkdir(parents=True, exist_ok=True)
    write_stability_csv(summaries, outputs_directory)
    write_rmsd_series(rmsd_series_by_compound, outputs_directory)
    write_run_summary(summary, outputs_directory)

    uploaded = store.upload_directory(outputs_directory, spec.output_uri)
    LOGGER.info("run %s: uploaded %s output files", spec.run_id, len(uploaded))

    output_prefix = spec.output_uri.rstrip("/")
    archive_path = write_results_archive(outputs_directory, workspace)
    results_archive_uri = store.upload_file(archive_path, f"{output_prefix}/{RESULTS_ARCHIVE_NAME}")

    promoted = [
        record["compound_id"]
        for record in summary["trajectories"]
        if record["verdict"] == VERDICT_STABLE
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
            "poses_simulated": len(summaries),
            "poses_failed": len(failures),
            "stable": counts["stable"],
            "drifted": counts["drifted"],
            "unstable": counts["unstable"],
            "promoted_compound_ids": promoted,
            "most_stable_compound": summary["stability_analysis"]["most_stable_compound"],
            "production_picoseconds": params.production_picoseconds,
            "platform": platform_name,
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
        with tempfile.TemporaryDirectory(prefix="cascade-md-stability-") as workspace:
            spec = read_job_spec(store, spec_uri)
            run_id = spec.run_id
            completion = run_md_stability_job(store, spec, Path(workspace))
    except Exception as error:
        LOGGER.exception("md_stability job failed")
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
