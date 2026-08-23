import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from rdkit import Chem

from docking import (
    best_pose_rmsd_to_reference,
    convert_receptor_pdb_to_pdbqt,
    dock_prepared_ligands,
    lowest_rmsd_across_modes,
)
from job_spec import DockingParams
from ligands import (
    LigandRecord,
    canonical_smiles,
    molecule_from_pdb_block_with_template,
    prepare_ligand_for_docking,
)
from structure import (
    binding_site_from_atoms,
    extract_protein_receptor_pdb,
    find_cocrystal_ligands,
    receptor_atom_count,
    receptor_chain_ids,
)

LOGGER = logging.getLogger("cascade.dock.validate")

RCSB_STRUCTURE_URL_TEMPLATE = "https://files.rcsb.org/download/{pdb_id}.pdb"
RCSB_IDEAL_LIGAND_URL_TEMPLATE = "https://files.rcsb.org/ligands/download/{ligand_code}_ideal.sdf"
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_TIMEOUT_SECONDS = 60
CRYSTAL_CONFORMER_VARIANT = "crystal_conformer"
GENERATED_CONFORMER_VARIANT = "generated_conformer"


@dataclass
class DockingTrial:
    variant: str
    exhaustiveness: int
    best_affinity_kcal_per_mol: float
    best_pose_rmsd_angstrom: float
    lowest_mode_rmsd_angstrom: float
    mode_affinity_spread: float
    elapsed_seconds: float


def download_text_with_retries(url: str, attempts: int = DOWNLOAD_ATTEMPTS) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                return response.read().decode()
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            LOGGER.warning("attempt %s/%s failed for %s: %s", attempt, attempts, url, error)
            time.sleep(2 * attempt)
    raise RuntimeError(f"could not download {url}: {last_error}")


def crystal_reference_molecule(cocrystal_ligand_pdb_block: str, ideal_sdf_text: str) -> Chem.Mol:
    template = Chem.MolFromMolBlock(ideal_sdf_text, removeHs=True)
    if template is None:
        raise RuntimeError("the RCSB ideal ligand definition could not be parsed")
    return molecule_from_pdb_block_with_template(cocrystal_ligand_pdb_block, template)


def build_docking_variants(reference_mol: Chem.Mol) -> dict[str, LigandRecord]:
    generated = Chem.MolFromSmiles(canonical_smiles(reference_mol))
    if generated is None:
        raise RuntimeError("the reference ligand SMILES could not be re-parsed")
    return {
        CRYSTAL_CONFORMER_VARIANT: LigandRecord(
            name=CRYSTAL_CONFORMER_VARIANT, mol=Chem.Mol(reference_mol)
        ),
        GENERATED_CONFORMER_VARIANT: LigandRecord(name=GENERATED_CONFORMER_VARIANT, mol=generated),
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the docking toolchain by redocking a co-crystallized ligand."
    )
    parser.add_argument("--pdb-id", default="1HSG")
    parser.add_argument("--ligand-code", default="MK1")
    parser.add_argument("--chain", default=None)
    parser.add_argument("--exhaustiveness", type=int, nargs="+", default=[8, 32])
    parser.add_argument("--num-modes", type=int, default=9)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--rmsd-threshold", type=float, default=2.0)
    parser.add_argument("--out-dir", default="validation-1hsg")
    return parser.parse_args()


def run_validation(arguments: argparse.Namespace) -> dict:
    output_directory = Path(arguments.out_dir)
    output_directory.mkdir(parents=True, exist_ok=True)
    poses_directory = output_directory / "poses"
    poses_directory.mkdir(parents=True, exist_ok=True)

    pdb_id = arguments.pdb_id.upper()
    ligand_code = arguments.ligand_code.upper()

    structure_pdb_text = download_text_with_retries(
        RCSB_STRUCTURE_URL_TEMPLATE.format(pdb_id=pdb_id)
    )
    (output_directory / f"{pdb_id}.pdb").write_text(structure_pdb_text)
    ideal_sdf_text = download_text_with_retries(
        RCSB_IDEAL_LIGAND_URL_TEMPLATE.format(ligand_code=ligand_code)
    )
    (output_directory / f"{ligand_code}_ideal.sdf").write_text(ideal_sdf_text)

    receptor_pdb = extract_protein_receptor_pdb(structure_pdb_text, chain=arguments.chain)
    receptor_pdb_path = output_directory / "receptor.pdb"
    receptor_pdb_path.write_text(receptor_pdb)
    chains_kept = receptor_chain_ids(receptor_pdb)
    LOGGER.info(
        "receptor: %s atoms across chains %s",
        receptor_atom_count(receptor_pdb),
        ",".join(chains_kept),
    )

    cocrystal_ligands = find_cocrystal_ligands(structure_pdb_text, chain=arguments.chain)
    if not cocrystal_ligands:
        raise RuntimeError(f"{pdb_id} has no co-crystallized ligand candidates")
    LOGGER.info(
        "co-crystallized ligand candidates: %s",
        ", ".join(
            f"{ligand.label} ({ligand.heavy_atom_count} heavy atoms)"
            for ligand in cocrystal_ligands
        ),
    )
    cocrystal_ligand = cocrystal_ligands[0]
    if cocrystal_ligand.residue_name != ligand_code:
        raise RuntimeError(
            f"expected {ligand_code} to be the largest co-crystallized ligand in {pdb_id}, "
            f"found {cocrystal_ligand.label}"
        )
    (output_directory / "reference_ligand.pdb").write_text(cocrystal_ligand.to_pdb_block())

    reference_mol = crystal_reference_molecule(cocrystal_ligand.to_pdb_block(), ideal_sdf_text)
    Chem.MolToMolFile(reference_mol, str(output_directory / "reference_ligand.sdf"))
    LOGGER.info("reference ligand SMILES: %s", canonical_smiles(reference_mol))

    binding_site = binding_site_from_atoms(cocrystal_ligand.heavy_atoms)
    LOGGER.info("binding site center %s box %s", binding_site.center, binding_site.box_size)

    receptor_pdbqt_path = convert_receptor_pdb_to_pdbqt(
        receptor_pdb_path, output_directory / "receptor.pdbqt"
    )

    variants = build_docking_variants(reference_mol)
    trials: list[DockingTrial] = []
    for variant_name, record in variants.items():
        prepared = prepare_ligand_for_docking(record, seed=arguments.seed)
        (poses_directory / f"{variant_name}_input.pdbqt").write_text(prepared.pdbqt)
        for exhaustiveness in sorted(arguments.exhaustiveness):
            params = DockingParams(
                exhaustiveness=exhaustiveness,
                num_modes=arguments.num_modes,
                seed=arguments.seed,
                cpu=arguments.cpu,
            )
            started = time.perf_counter()
            results, failures = dock_prepared_ligands(
                receptor_pdbqt_path, [prepared], binding_site, params
            )
            elapsed = time.perf_counter() - started
            if not results:
                raise RuntimeError(
                    f"{variant_name} failed to dock at exhaustiveness {exhaustiveness}: "
                    f"{failures[0].reason if failures else 'unknown reason'}"
                )
            result = results[0]
            pose_path = poses_directory / f"{variant_name}_exhaustiveness{exhaustiveness}.pdbqt"
            pose_path.write_text(result.poses_pdbqt)
            trial = DockingTrial(
                variant=variant_name,
                exhaustiveness=exhaustiveness,
                best_affinity_kcal_per_mol=result.best_affinity,
                best_pose_rmsd_angstrom=best_pose_rmsd_to_reference(result, reference_mol),
                lowest_mode_rmsd_angstrom=lowest_rmsd_across_modes(result, reference_mol),
                mode_affinity_spread=result.mode_affinity_spread,
                elapsed_seconds=round(elapsed, 1),
            )
            trials.append(trial)
            LOGGER.info(
                "%s at exhaustiveness %s: %.2f kcal/mol, best-pose RMSD %.2f A, "
                "lowest-mode RMSD %.2f A, %.1fs",
                variant_name,
                exhaustiveness,
                trial.best_affinity_kcal_per_mol,
                trial.best_pose_rmsd_angstrom,
                trial.lowest_mode_rmsd_angstrom,
                trial.elapsed_seconds,
            )

    highest_exhaustiveness = max(arguments.exhaustiveness)
    decisive_trial = next(
        trial
        for trial in trials
        if trial.variant == CRYSTAL_CONFORMER_VARIANT
        and trial.exhaustiveness == highest_exhaustiveness
    )
    passed = decisive_trial.best_pose_rmsd_angstrom < arguments.rmsd_threshold

    report = {
        "pdb_id": pdb_id,
        "ligand_code": ligand_code,
        "passed": passed,
        "rmsd_threshold_angstrom": arguments.rmsd_threshold,
        "decisive_trial": asdict(decisive_trial),
        "receptor": {
            "chains_kept": chains_kept,
            "atom_count": receptor_atom_count(receptor_pdb),
        },
        "cocrystal_ligand": {
            "label": cocrystal_ligand.label,
            "heavy_atom_count": cocrystal_ligand.heavy_atom_count,
            "smiles": canonical_smiles(reference_mol),
        },
        "binding_site": binding_site.model_dump(),
        "trials": [asdict(trial) for trial in trials],
    }
    (output_directory / "validation_report.json").write_text(json.dumps(report, indent=2))
    return report


def print_report(report: dict) -> None:
    header = (
        f"{'variant':<21}{'exh':>5}{'affinity':>11}{'pose RMSD':>12}"
        f"{'best RMSD':>12}{'spread':>9}{'seconds':>9}"
    )
    print()
    print(f"1HSG redocking validation — {report['pdb_id']} / {report['ligand_code']}")
    print(
        f"binding site center "
        f"({report['binding_site']['center_x']}, {report['binding_site']['center_y']}, "
        f"{report['binding_site']['center_z']}) "
        f"box ({report['binding_site']['size_x']}, {report['binding_site']['size_y']}, "
        f"{report['binding_site']['size_z']})"
    )
    print(header)
    print("-" * len(header))
    for trial in report["trials"]:
        print(
            f"{trial['variant']:<21}{trial['exhaustiveness']:>5}"
            f"{trial['best_affinity_kcal_per_mol']:>11.2f}"
            f"{trial['best_pose_rmsd_angstrom']:>12.2f}"
            f"{trial['lowest_mode_rmsd_angstrom']:>12.2f}"
            f"{trial['mode_affinity_spread']:>9.2f}"
            f"{trial['elapsed_seconds']:>9.1f}"
        )
    print()
    verdict = "PASS" if report["passed"] else "FAIL"
    print(
        f"{verdict}: crystal conformer at exhaustiveness "
        f"{report['decisive_trial']['exhaustiveness']} reproduced the crystal pose to "
        f"{report['decisive_trial']['best_pose_rmsd_angstrom']:.2f} A "
        f"(threshold {report['rmsd_threshold_angstrom']} A)"
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    report = run_validation(parse_arguments())
    print_report(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
