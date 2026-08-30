from dataclasses import dataclass

from rdkit import Chem

from stability import PoseFailure


class PoseLoadError(Exception):
    pass


@dataclass
class DockedPose:
    name: str
    mol: Chem.Mol
    affinity_rank: int | None = None
    best_affinity_kcal_per_mol: float | None = None

    @property
    def heavy_atom_count(self) -> int:
        return Chem.RemoveHs(self.mol).GetNumAtoms()


def optional_float_property(mol: Chem.Mol, name: str) -> float | None:
    if not mol.HasProp(name):
        return None
    try:
        return float(mol.GetProp(name))
    except ValueError:
        return None


def optional_int_property(mol: Chem.Mol, name: str) -> int | None:
    value = optional_float_property(mol, name)
    return None if value is None else int(value)


def pose_name(mol: Chem.Mol, index: int) -> str:
    for property_name in ("compound_id", "_Name"):
        if mol.HasProp(property_name):
            candidate = mol.GetProp(property_name).strip()
            if candidate:
                return candidate
    return f"compound_{index}"


def read_docked_poses(sdf_text: str) -> tuple[list[DockedPose], list[PoseFailure]]:
    supplier = Chem.SDMolSupplier()
    supplier.SetData(sdf_text, removeHs=False, sanitize=True)
    poses: list[DockedPose] = []
    failures: list[PoseFailure] = []
    for index, mol in enumerate(supplier, start=1):
        if mol is None:
            failures.append(PoseFailure(name=f"record_{index}", reason="pose could not be parsed"))
            continue
        if mol.GetNumConformers() == 0:
            failures.append(
                PoseFailure(name=pose_name(mol, index), reason="pose carried no 3D coordinates")
            )
            continue
        poses.append(
            DockedPose(
                name=pose_name(mol, index),
                mol=mol,
                affinity_rank=optional_int_property(mol, "affinity_rank"),
                best_affinity_kcal_per_mol=optional_float_property(
                    mol, "best_affinity_kcal_per_mol"
                ),
            )
        )
    if not poses and not failures:
        raise PoseLoadError("the pose file held no records")
    return poses, failures


def poses_for_simulation(poses: list[DockedPose], max_complexes: int) -> list[DockedPose]:
    ranked = sorted(
        poses, key=lambda pose: (pose.affinity_rank is None, pose.affinity_rank or 0, pose.name)
    )
    return ranked[:max_complexes]
