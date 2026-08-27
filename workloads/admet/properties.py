import os
import sys
from dataclasses import asdict, dataclass

from rdkit import Chem, RDConfig
from rdkit.Chem import QED, Crippen, Descriptors, Lipinski, rdMolDescriptors


def _load_synthetic_accessibility_scorer():
    contrib_path = os.path.join(RDConfig.RDContribDir, "SA_Score")
    if contrib_path not in sys.path:
        sys.path.append(contrib_path)
    import sascorer

    return sascorer


_SYNTHETIC_ACCESSIBILITY = _load_synthetic_accessibility_scorer()


@dataclass(frozen=True)
class CompoundProperties:
    molecular_weight: float
    crippen_logp: float
    topological_polar_surface_area: float
    hydrogen_bond_donors: int
    hydrogen_bond_acceptors: int
    rotatable_bonds: int
    aromatic_rings: int
    heavy_atoms: int
    formal_charge: int
    fraction_carbon_sp3: float
    molar_refractivity: float
    drug_likeness_qed: float
    synthetic_accessibility: float

    def as_dict(self) -> dict:
        return asdict(self)


def compute_compound_properties(mol: Chem.Mol) -> CompoundProperties:
    return CompoundProperties(
        molecular_weight=round(Descriptors.MolWt(mol), 2),
        crippen_logp=round(Crippen.MolLogP(mol), 3),
        topological_polar_surface_area=round(Descriptors.TPSA(mol), 2),
        hydrogen_bond_donors=Lipinski.NumHDonors(mol),
        hydrogen_bond_acceptors=Lipinski.NumHAcceptors(mol),
        rotatable_bonds=Lipinski.NumRotatableBonds(mol),
        aromatic_rings=rdMolDescriptors.CalcNumAromaticRings(mol),
        heavy_atoms=mol.GetNumHeavyAtoms(),
        formal_charge=Chem.GetFormalCharge(mol),
        fraction_carbon_sp3=round(Descriptors.FractionCSP3(mol), 3),
        molar_refractivity=round(Crippen.MolMR(mol), 2),
        drug_likeness_qed=round(QED.qed(mol), 3),
        synthetic_accessibility=round(_SYNTHETIC_ACCESSIBILITY.calculateScore(mol), 2),
    )
