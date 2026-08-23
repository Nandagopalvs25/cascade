import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from meeko import PDBQTMolecule, RDKitMolCreate
from rdkit import Chem
from rdkit.Chem import rdMolAlign
from vina import Vina

from job_spec import BindingSite, DockingParams
from ligands import LigandFailure, PreparedLigand

OPENBABEL_EXECUTABLE = "obabel"
OPENBABEL_TIMEOUT_SECONDS = 600


class ReceptorPreparationError(Exception):
    pass


class PoseConversionError(Exception):
    pass


@dataclass
class LigandDockingResult:
    name: str
    mode_affinities: list[float] = field(default_factory=list)
    poses_pdbqt: str = ""

    @property
    def best_affinity(self) -> float:
        return self.mode_affinities[0]

    @property
    def mode_count(self) -> int:
        return len(self.mode_affinities)

    @property
    def mode_affinity_spread(self) -> float:
        if not self.mode_affinities:
            return 0.0
        return round(max(self.mode_affinities) - min(self.mode_affinities), 3)

    @property
    def best_pose_pdbqt(self) -> str:
        models = split_pose_models(self.poses_pdbqt)
        return models[0] if models else self.poses_pdbqt


def convert_receptor_pdb_to_pdbqt(
    receptor_pdb_path: str | Path, output_path: str | Path, ph: float = 7.4
) -> Path:
    executable = shutil.which(OPENBABEL_EXECUTABLE)
    if executable is None:
        raise ReceptorPreparationError(f"{OPENBABEL_EXECUTABLE} is not available in this image")

    destination = Path(output_path)
    completed = subprocess.run(
        [
            executable,
            str(receptor_pdb_path),
            "-O",
            str(destination),
            "-xr",
            "-p",
            f"{ph}",
        ],
        capture_output=True,
        text=True,
        timeout=OPENBABEL_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise ReceptorPreparationError(
            f"openbabel receptor conversion failed: {completed.stderr.strip()[-500:]}"
        )
    if not destination.exists() or "ATOM" not in destination.read_text():
        raise ReceptorPreparationError("openbabel produced an empty receptor PDBQT")
    return destination


def split_pose_models(poses_pdbqt: str) -> list[str]:
    models: list[str] = []
    current: list[str] = []
    inside_model = False
    for line in poses_pdbqt.splitlines():
        if line.startswith("MODEL"):
            inside_model = True
            current = []
            continue
        if line.startswith("ENDMDL"):
            inside_model = False
            if current:
                models.append("\n".join([*current, ""]))
            current = []
            continue
        if inside_model:
            current.append(line)
    if not models and poses_pdbqt.strip():
        models.append(poses_pdbqt)
    return models


def dock_prepared_ligands(
    receptor_pdbqt_path: str | Path,
    prepared_ligands: list[PreparedLigand],
    binding_site: BindingSite,
    params: DockingParams,
) -> tuple[list[LigandDockingResult], list[LigandFailure]]:
    engine = Vina(sf_name="vina", cpu=params.cpu, seed=params.seed, verbosity=0)
    engine.set_receptor(str(receptor_pdbqt_path))
    engine.compute_vina_maps(center=binding_site.center, box_size=binding_site.box_size)

    results: list[LigandDockingResult] = []
    failures: list[LigandFailure] = []
    for ligand in prepared_ligands:
        try:
            engine.set_ligand_from_string(ligand.pdbqt)
            engine.dock(exhaustiveness=params.exhaustiveness, n_poses=params.num_modes)
            energies = engine.energies(n_poses=params.num_modes)
            mode_affinities = [round(float(row[0]), 3) for row in energies]
            if not mode_affinities:
                failures.append(
                    LigandFailure(name=ligand.name, reason="docking returned no scored pose")
                )
                continue
            results.append(
                LigandDockingResult(
                    name=ligand.name,
                    mode_affinities=mode_affinities,
                    poses_pdbqt=engine.poses(n_poses=params.num_modes),
                )
            )
        except Exception as error:
            failures.append(LigandFailure(name=ligand.name, reason=f"docking failed: {error}"))
    return results, failures


def molecule_from_pose_pdbqt(pose_pdbqt: str) -> Chem.Mol:
    pdbqt_molecule = PDBQTMolecule(pose_pdbqt, skip_typing=True)
    molecules = RDKitMolCreate.from_pdbqt_mol(pdbqt_molecule)
    if not molecules or molecules[0] is None:
        raise PoseConversionError("docked pose could not be converted back to a molecule")
    return Chem.RemoveHs(molecules[0])


def heavy_atom_rmsd_without_alignment(pose_mol: Chem.Mol, reference_mol: Chem.Mol) -> float:
    return round(
        rdMolAlign.CalcRMS(Chem.RemoveHs(pose_mol), Chem.RemoveHs(reference_mol)),
        3,
    )


def best_pose_rmsd_to_reference(result: LigandDockingResult, reference_mol: Chem.Mol) -> float:
    return heavy_atom_rmsd_without_alignment(
        molecule_from_pose_pdbqt(result.best_pose_pdbqt), reference_mol
    )


def pose_rmsd_by_mode(result: LigandDockingResult, reference_mol: Chem.Mol) -> list[float]:
    return [
        heavy_atom_rmsd_without_alignment(molecule_from_pose_pdbqt(model), reference_mol)
        for model in split_pose_models(result.poses_pdbqt)
    ]


def lowest_rmsd_across_modes(result: LigandDockingResult, reference_mol: Chem.Mol) -> float:
    return min(pose_rmsd_by_mode(result, reference_mol))
