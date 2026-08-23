import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from google.cloud import pubsub_v1, storage

from docking import (
    LigandDockingResult,
    convert_receptor_pdb_to_pdbqt,
    dock_prepared_ligands,
    pose_rmsd_by_mode,
)
from job_spec import DockingParams, JobSpec
from ligands import (
    LigandRecord,
    canonical_smiles,
    load_ligand_library,
    molecule_from_pdb_block_with_template,
    prepare_ligand_library,
)
from results import (
    RESULTS_ARCHIVE_NAME,
    RESULTS_FILE_NAME,
    BindingSiteSummary,
    ControlCompoundSummary,
    ReceptorSummary,
    build_run_summary,
    write_best_pose_files,
    write_reference_ligand_pdb,
    write_results_archive,
    write_run_summary,
    write_scores_csv,
)
from structure import (
    HeteroResidue,
    binding_site_from_atoms,
    extract_protein_receptor_pdb,
    find_cocrystal_ligands,
    receptor_atom_count,
    receptor_chain_ids,
)

LOGGER = logging.getLogger("cascade.dock")

SPEC_URI_ENVIRONMENT_VARIABLE = "SPEC_URI"
RUN_ID_ENVIRONMENT_VARIABLE = "RUN_ID"
PROJECT_ENVIRONMENT_VARIABLE = "GCP_PROJECT"
TOPIC_ENVIRONMENT_VARIABLE = "PUBSUB_TOPIC"
GCS_URI_SCHEME = "gs://"
PUBLISH_TIMEOUT_SECONDS = 60


class ArtifactStore:
    def __init__(self) -> None:
        self._storage_client: storage.Client | None = None

    def _client(self) -> storage.Client:
        if self._storage_client is None:
            self._storage_client = storage.Client()
        return self._storage_client

    @staticmethod
    def _split_gcs_uri(uri: str) -> tuple[str, str]:
        bucket_name, _, object_path = uri.removeprefix(GCS_URI_SCHEME).partition("/")
        if not bucket_name or not object_path:
            raise ValueError(f"GCS URI missing bucket or object path: {uri}")
        return bucket_name, object_path

    def read_text(self, uri: str) -> str:
        if uri.startswith(GCS_URI_SCHEME):
            bucket_name, object_path = self._split_gcs_uri(uri)
            return self._client().bucket(bucket_name).blob(object_path).download_as_text()
        return Path(uri).read_text()

    def download_to_file(self, uri: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if uri.startswith(GCS_URI_SCHEME):
            bucket_name, object_path = self._split_gcs_uri(uri)
            self._client().bucket(bucket_name).blob(object_path).download_to_filename(
                str(destination)
            )
        else:
            destination.write_bytes(Path(uri).read_bytes())
        return destination

    def upload_file(self, source: Path, uri: str) -> str:
        if uri.startswith(GCS_URI_SCHEME):
            bucket_name, object_path = self._split_gcs_uri(uri)
            self._client().bucket(bucket_name).blob(object_path).upload_from_filename(str(source))
        else:
            destination = Path(uri)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        return uri

    def upload_directory(self, directory: Path, prefix_uri: str) -> list[str]:
        prefix = prefix_uri.rstrip("/")
        uploaded: list[str] = []
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                uploaded.append(self.upload_file(path, f"{prefix}/{relative}"))
        return uploaded


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def read_job_spec(store: ArtifactStore, spec_uri: str) -> JobSpec:
    spec = JobSpec.model_validate_json(store.read_text(spec_uri))
    if spec.workload != "dock":
        raise ValueError(f"the dock container cannot run workload {spec.workload!r}")
    return spec


def ligand_library_file_name(ligands_uri: str) -> str:
    name = ligands_uri.rstrip("/").rsplit("/", maxsplit=1)[-1]
    return name or "ligands.sdf"


def resolve_binding_site(
    spec: JobSpec, params: DockingParams, structure_pdb_text: str
) -> tuple[BindingSiteSummary, HeteroResidue | None]:
    cocrystal_ligands = find_cocrystal_ligands(structure_pdb_text, chain=spec.target.chain)
    largest_cocrystal_ligand = cocrystal_ligands[0] if cocrystal_ligands else None

    if spec.binding_site is not None:
        return (
            BindingSiteSummary(
                binding_site=spec.binding_site,
                origin="job_spec",
                cocrystal_ligand=(
                    largest_cocrystal_ligand.label if largest_cocrystal_ligand else None
                ),
            ),
            largest_cocrystal_ligand,
        )

    if largest_cocrystal_ligand is None:
        raise ValueError(
            "the job spec carries no binding site and the structure has no co-crystallized "
            "ligand to derive one from"
        )

    derived = binding_site_from_atoms(
        largest_cocrystal_ligand.heavy_atoms, padding=params.binding_site_padding
    )
    return (
        BindingSiteSummary(
            binding_site=derived,
            origin="cocrystal_ligand",
            cocrystal_ligand=largest_cocrystal_ligand.label,
        ),
        largest_cocrystal_ligand,
    )


def measure_control_compound(
    spec: JobSpec,
    records: list[LigandRecord],
    results: list[LigandDockingResult],
    cocrystal_ligand: HeteroResidue | None,
) -> ControlCompoundSummary:
    if spec.control_compound is None:
        return ControlCompoundSummary()

    requested = spec.control_compound.strip()
    lowered = requested.lower()
    matching_result = next(
        (result for result in results if result.name.strip().lower() == lowered), None
    )
    if matching_result is None:
        return ControlCompoundSummary(
            requested_name=requested,
            status="not_in_library",
            detail="no docked compound carries the control compound name",
        )

    summary = ControlCompoundSummary(
        requested_name=requested,
        status="measured",
        best_affinity=matching_result.best_affinity,
    )

    if cocrystal_ligand is None:
        summary.status = "no_cocrystal_reference"
        summary.detail = "the structure has no co-crystallized ligand to compare the pose against"
        return summary

    control_record = next(
        (record for record in records if record.name.strip().lower() == lowered), None
    )
    if control_record is None:
        summary.status = "comparison_unavailable"
        summary.detail = "the control compound was docked but its input molecule was not retained"
        return summary

    try:
        reference_mol = molecule_from_pdb_block_with_template(
            cocrystal_ligand.to_pdb_block(), control_record.mol
        )
        rmsd_by_mode = pose_rmsd_by_mode(matching_result, reference_mol)
        summary.rmsd_to_cocrystal_angstrom = rmsd_by_mode[0]
        summary.lowest_mode_rmsd_angstrom = min(rmsd_by_mode)
        summary.lowest_mode_rank = rmsd_by_mode.index(min(rmsd_by_mode)) + 1
        summary.detail = (
            f"{len(rmsd_by_mode)} poses compared against co-crystallized ligand "
            f"{cocrystal_ligand.label}"
        )
    except Exception as error:
        summary.status = "reference_mismatch"
        summary.detail = (
            f"co-crystallized ligand {cocrystal_ligand.label} does not match the control "
            f"compound ({canonical_smiles(control_record.mol)}): {error}"
        )
    return summary


def publish_job_completion(payload: dict) -> None:
    project_id = os.environ.get(PROJECT_ENVIRONMENT_VARIABLE)
    topic_name = os.environ.get(TOPIC_ENVIRONMENT_VARIABLE)
    if not project_id or not topic_name:
        LOGGER.warning(
            "skipping completion publish: %s and %s must both be set",
            PROJECT_ENVIRONMENT_VARIABLE,
            TOPIC_ENVIRONMENT_VARIABLE,
        )
        return
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(project_id, topic_name)
    future = publisher.publish(topic_path, json.dumps(payload).encode())
    future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
    LOGGER.info("published completion for run %s to %s", payload.get("run_id"), topic_path)


def run_docking_job(store: ArtifactStore, spec: JobSpec, workspace: Path) -> dict:
    params = DockingParams.from_job_spec(spec)
    LOGGER.info(
        "run %s: docking against %s with exhaustiveness %s",
        spec.run_id,
        spec.target.reference,
        params.exhaustiveness,
    )

    structure_path = store.download_to_file(spec.target.structure_uri, workspace / "target.pdb")
    structure_pdb_text = structure_path.read_text()
    receptor_pdb = extract_protein_receptor_pdb(structure_pdb_text, chain=spec.target.chain)
    receptor_pdb_path = workspace / "receptor.pdb"
    receptor_pdb_path.write_text(receptor_pdb)
    receptor = ReceptorSummary(
        structure_uri=spec.target.structure_uri,
        requested_chain=spec.target.chain,
        chains_kept=receptor_chain_ids(receptor_pdb),
        atom_count=receptor_atom_count(receptor_pdb),
    )
    LOGGER.info(
        "run %s: receptor kept %s atoms across chains %s",
        spec.run_id,
        receptor.atom_count,
        ",".join(receptor.chains_kept),
    )

    site, cocrystal_ligand = resolve_binding_site(spec, params, structure_pdb_text)
    LOGGER.info(
        "run %s: binding site from %s at %s box %s",
        spec.run_id,
        site.origin,
        site.binding_site.center,
        site.binding_site.box_size,
    )

    receptor_pdbqt_path = convert_receptor_pdb_to_pdbqt(
        receptor_pdb_path, workspace / "receptor.pdbqt", ph=params.receptor_ph
    )

    ligands_path = store.download_to_file(
        spec.ligands_uri, workspace / ligand_library_file_name(spec.ligands_uri)
    )
    records, load_failures = load_ligand_library(ligands_path)
    if not records:
        raise ValueError(f"no usable compounds in {spec.ligands_uri}")
    if len(records) > params.max_ligands:
        raise ValueError(
            f"{len(records)} compounds exceeds the {params.max_ligands} compound ceiling for a "
            "single docking job"
        )
    prepared, preparation_failures = prepare_ligand_library(records, seed=params.seed)
    if not prepared:
        raise ValueError(f"no compounds in {spec.ligands_uri} could be prepared for docking")
    LOGGER.info(
        "run %s: prepared %s of %s compounds, %s unusable",
        spec.run_id,
        len(prepared),
        len(records) + len(load_failures),
        len(load_failures) + len(preparation_failures),
    )

    results, docking_failures = dock_prepared_ligands(
        receptor_pdbqt_path, prepared, site.binding_site, params
    )
    if not results:
        raise ValueError("every compound failed to dock")
    failures = [*load_failures, *preparation_failures, *docking_failures]
    LOGGER.info("run %s: docked %s compounds", spec.run_id, len(results))

    control = measure_control_compound(spec, records, results, cocrystal_ligand)
    LOGGER.info(
        "run %s: control compound %s status %s top-pose rmsd %s lowest-mode rmsd %s at mode %s",
        spec.run_id,
        control.requested_name,
        control.status,
        control.rmsd_to_cocrystal_angstrom,
        control.lowest_mode_rmsd_angstrom,
        control.lowest_mode_rank,
    )

    outputs_directory = workspace / "outputs"
    outputs_directory.mkdir(parents=True, exist_ok=True)
    write_scores_csv(results, outputs_directory)
    write_best_pose_files(results, outputs_directory)
    if cocrystal_ligand is not None:
        write_reference_ligand_pdb(cocrystal_ligand.to_pdb_block(), outputs_directory)
    summary = build_run_summary(spec, params, receptor, site, control, results, failures)
    write_run_summary(summary, outputs_directory)

    uploaded = store.upload_directory(outputs_directory, spec.output_uri)
    LOGGER.info("run %s: uploaded %s output files", spec.run_id, len(uploaded))

    output_prefix = spec.output_uri.rstrip("/")
    archive_path = write_results_archive(outputs_directory, workspace)
    results_archive_uri = store.upload_file(archive_path, f"{output_prefix}/{RESULTS_ARCHIVE_NAME}")

    best = summary["scores"][0]
    return {
        "run_id": spec.run_id,
        "workload": spec.workload,
        "status": "succeeded",
        "exit_code": 0,
        "results_uri": output_prefix,
        "results_manifest_uri": f"{output_prefix}/{RESULTS_FILE_NAME}",
        "results_archive_uri": results_archive_uri,
        "summary": {
            "ligands_docked": len(results),
            "ligands_failed": len(failures),
            "best_compound_id": best["compound_id"],
            "best_affinity_kcal_per_mol": best["best_affinity_kcal_per_mol"],
            "binding_site_origin": site.origin,
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
        with tempfile.TemporaryDirectory(prefix="cascade-dock-") as workspace:
            spec = read_job_spec(store, spec_uri)
            run_id = spec.run_id
            completion = run_docking_job(store, spec, Path(workspace))
    except Exception as error:
        LOGGER.exception("docking job failed")
        publish_job_completion(
            {
                "run_id": run_id,
                "workload": "dock",
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
