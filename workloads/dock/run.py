import logging
import os
import sys
import tempfile
from pathlib import Path

from artifacts import ArtifactStore, publish_job_completion
from docking import (
    LigandDockingResult,
    convert_receptor_pdb_to_pdbqt,
    dock_prepared_ligands,
    pose_rmsd_by_mode,
)
from job_spec import DockingParams, JobSpec, require_target_structure
from ligands import (
    LigandFailure,
    LigandRecord,
    canonical_smiles,
    load_ligand_library,
    molecule_from_pdb_block_with_template,
    prepare_ligand_library,
    records_protonated_at_ph,
)
from results import (
    RESULTS_ARCHIVE_NAME,
    RESULTS_FILE_NAME,
    BindingSiteSummary,
    ControlCompoundSummary,
    ReceptorSummary,
    build_run_summary,
    write_best_pose_files,
    write_combined_poses_sdf,
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
WORKLOAD = "dock"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def read_job_spec(store: ArtifactStore, spec_uri: str) -> JobSpec:
    spec = JobSpec.model_validate_json(store.read_text(spec_uri))
    if spec.workload != WORKLOAD:
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


def docking_completion_payload(
    spec: JobSpec,
    summary: dict,
    binding_site_origin: str,
    ligands_docked: int,
    ligands_failed: int,
    output_prefix: str,
    results_archive_uri: str,
) -> dict:
    best = summary["scores"][0]
    analysis = summary["score_analysis"] or {}
    return {
        "run_id": spec.run_id,
        "workload": spec.workload,
        "status": "succeeded",
        "exit_code": 0,
        "results_uri": output_prefix,
        "results_manifest_uri": f"{output_prefix}/{RESULTS_FILE_NAME}",
        "results_archive_uri": results_archive_uri,
        "summary": {
            "ligands_docked": ligands_docked,
            "ligands_failed": ligands_failed,
            "best_compound_id": best["compound_id"],
            "best_affinity_kcal_per_mol": best["best_affinity_kcal_per_mol"],
            "binding_site_origin": binding_site_origin,
            "control_compound": summary["control_compound"],
            "score_analysis": {
                "scoring_function_error_kcal_per_mol": analysis.get(
                    "scoring_function_error_kcal_per_mol"
                ),
                "ranking_separates_best_compound": analysis.get("ranking_separates_best_compound"),
                "compounds_indistinguishable_from_best_count": len(
                    analysis.get("compounds_indistinguishable_from_best", [])
                ),
                "ranking_is_size_driven": analysis.get("ranking_is_size_driven"),
                "affinity_heavy_atom_correlation": analysis.get("affinity_heavy_atom_correlation"),
                "metrics_agree_on_best_compound": analysis.get("metrics_agree_on_best_compound"),
                "best_by_ligand_efficiency": analysis.get("best_by_ligand_efficiency"),
                "control_affinity_rank": analysis.get("control_affinity_rank"),
            },
        },
    }


def run_docking_job(store: ArtifactStore, spec: JobSpec, workspace: Path) -> dict:
    require_target_structure(spec)
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
    protonation_failures: list[LigandFailure] = []
    if params.ligand_ph is not None:
        records, protonation_failures = records_protonated_at_ph(records, params.ligand_ph)
        LOGGER.info(
            "run %s: protonated %s compounds at pH %s, %s unusable",
            spec.run_id,
            len(records),
            params.ligand_ph,
            len(protonation_failures),
        )
    prepared, preparation_failures = prepare_ligand_library(
        records, seed=params.seed, conformers_per_ligand=params.conformers_per_ligand
    )
    if not prepared:
        raise ValueError(f"no compounds in {spec.ligands_uri} could be prepared for docking")
    LOGGER.info(
        "run %s: prepared %s conformers of %s compounds at %s per compound, %s unusable",
        spec.run_id,
        len(prepared),
        len(records) + len(load_failures),
        params.conformers_per_ligand,
        len(load_failures) + len(protonation_failures) + len(preparation_failures),
    )

    results, docking_failures = dock_prepared_ligands(
        receptor_pdbqt_path, prepared, site.binding_site, params
    )
    if not results:
        raise ValueError("every compound failed to dock")
    failures = [
        *load_failures,
        *protonation_failures,
        *preparation_failures,
        *docking_failures,
    ]
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
    write_combined_poses_sdf(results, outputs_directory)
    if cocrystal_ligand is not None:
        write_reference_ligand_pdb(cocrystal_ligand.to_pdb_block(), outputs_directory)
    summary = build_run_summary(spec, params, receptor, site, control, results, failures)
    write_run_summary(summary, outputs_directory)

    uploaded = store.upload_directory(outputs_directory, spec.output_uri)
    LOGGER.info("run %s: uploaded %s output files", spec.run_id, len(uploaded))

    output_prefix = spec.output_uri.rstrip("/")
    archive_path = write_results_archive(outputs_directory, workspace)
    results_archive_uri = store.upload_file(archive_path, f"{output_prefix}/{RESULTS_ARCHIVE_NAME}")

    analysis = summary["score_analysis"] or {}
    LOGGER.info(
        "run %s: score analysis separates_best=%s size_driven=%s correlation=%s "
        "indistinguishable=%s",
        spec.run_id,
        analysis.get("ranking_separates_best_compound"),
        analysis.get("ranking_is_size_driven"),
        analysis.get("affinity_heavy_atom_correlation"),
        len(analysis.get("compounds_indistinguishable_from_best", [])),
    )
    return docking_completion_payload(
        spec, summary, site.origin, len(results), len(failures), output_prefix, results_archive_uri
    )


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
