import csv
import io
from dataclasses import dataclass
from pathlib import Path

from meeko import MoleculePreparation, PDBQTWriterLegacy
from rdkit import Chem, RDLogger
from rdkit.Chem import AllChem
from rdkit.Chem.AllChem import AssignBondOrdersFromTemplate

RDLogger.DisableLog("rdApp.*")

SDF_SUFFIXES = frozenset({".sdf", ".sd", ".mol"})
SMILES_SUFFIXES = frozenset({".smi", ".smiles", ".txt"})
CSV_SMILES_COLUMN_NAMES = ("smiles", "canonical_smiles", "structure")
CSV_NAME_COLUMN_NAMES = ("name", "id", "compound_id", "compound", "title")


class LigandPreparationError(Exception):
    pass


@dataclass
class LigandRecord:
    name: str
    mol: Chem.Mol


@dataclass
class PreparedLigand:
    name: str
    pdbqt: str
    mol: Chem.Mol


@dataclass
class LigandFailure:
    name: str
    reason: str


def _fallback_ligand_name(index: int) -> str:
    return f"CMP-{index + 1:03d}"


def _molecule_from_mol_block(block: str) -> tuple[Chem.Mol | None, str]:
    mol = Chem.MolFromMolBlock(block, removeHs=False)
    if mol is not None:
        return mol, ""
    if Chem.MolFromMolBlock(block, removeHs=False, sanitize=False) is None:
        return None, "molblock could not be parsed"
    return None, "molblock parsed but failed chemistry sanitization"


def _molecule_from_smiles(smiles: str) -> tuple[Chem.Mol | None, str]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is not None:
        return mol, ""
    if Chem.MolFromSmiles(smiles, sanitize=False) is None:
        return None, f"SMILES could not be parsed: {smiles}"
    return None, f"SMILES parsed but failed chemistry sanitization: {smiles}"


def _load_ligands_from_sdf_text(text: str) -> tuple[list[LigandRecord], list[LigandFailure]]:
    records: list[LigandRecord] = []
    failures: list[LigandFailure] = []
    blocks = [block for block in text.split("$$$$") if block.strip()]
    for index, block in enumerate(blocks):
        molblock = block.lstrip("\n")
        title = molblock.splitlines()[0].strip() if molblock.splitlines() else ""
        name = title or _fallback_ligand_name(index)
        mol, reason = _molecule_from_mol_block(molblock)
        if mol is None:
            failures.append(LigandFailure(name=name, reason=reason))
            continue
        mol.SetProp("_Name", name)
        records.append(LigandRecord(name=name, mol=mol))
    return records, failures


def _load_ligands_from_smiles_lines(text: str) -> tuple[list[LigandRecord], list[LigandFailure]]:
    records: list[LigandRecord] = []
    failures: list[LigandFailure] = []
    index = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        smiles = fields[0]
        name = fields[1] if len(fields) > 1 else _fallback_ligand_name(index)
        index += 1
        mol, reason = _molecule_from_smiles(smiles)
        if mol is None:
            failures.append(LigandFailure(name=name, reason=reason))
            continue
        mol.SetProp("_Name", name)
        records.append(LigandRecord(name=name, mol=mol))
    return records, failures


def _column_value(row: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    lowered = {key.strip().lower(): value for key, value in row.items() if key}
    for candidate in candidates:
        value = lowered.get(candidate)
        if value and value.strip():
            return value.strip()
    return None


def _load_ligands_from_csv_text(text: str) -> tuple[list[LigandRecord], list[LigandFailure]]:
    records: list[LigandRecord] = []
    failures: list[LigandFailure] = []
    reader = csv.DictReader(io.StringIO(text))
    for index, row in enumerate(reader):
        smiles = _column_value(row, CSV_SMILES_COLUMN_NAMES)
        name = _column_value(row, CSV_NAME_COLUMN_NAMES) or _fallback_ligand_name(index)
        if smiles is None:
            failures.append(LigandFailure(name=name, reason="row has no SMILES column"))
            continue
        mol, reason = _molecule_from_smiles(smiles)
        if mol is None:
            failures.append(LigandFailure(name=name, reason=reason))
            continue
        mol.SetProp("_Name", name)
        records.append(LigandRecord(name=name, mol=mol))
    return records, failures


def load_ligand_library(path: str | Path) -> tuple[list[LigandRecord], list[LigandFailure]]:
    library_path = Path(path)
    suffix = library_path.suffix.lower()
    text = library_path.read_text()
    if suffix in SDF_SUFFIXES:
        return _load_ligands_from_sdf_text(text)
    if suffix in SMILES_SUFFIXES:
        return _load_ligands_from_smiles_lines(text)
    if suffix == ".csv":
        return _load_ligands_from_csv_text(text)
    raise ValueError(f"unsupported ligand library format: {library_path.name}")


def has_three_dimensional_conformer(mol: Chem.Mol) -> bool:
    return mol.GetNumConformers() > 0 and mol.GetConformer().Is3D()


def generate_three_dimensional_conformer(mol: Chem.Mol, seed: int = 42) -> Chem.Mol:
    embedded = Chem.AddHs(mol)
    parameters = AllChem.ETKDGv3()
    parameters.randomSeed = seed
    if AllChem.EmbedMolecule(embedded, parameters) != 0:
        parameters.useRandomCoords = True
        if AllChem.EmbedMolecule(embedded, parameters) != 0:
            raise LigandPreparationError("could not generate a 3D conformer")
    if AllChem.MMFFHasAllMoleculeParams(embedded):
        AllChem.MMFFOptimizeMolecule(embedded, maxIters=500)
    else:
        AllChem.UFFOptimizeMolecule(embedded, maxIters=500)
    return embedded


def ligand_pdbqt_string(mol: Chem.Mol) -> str:
    setups = MoleculePreparation().prepare(mol)
    if not setups:
        raise LigandPreparationError("meeko produced no molecule setup")
    pdbqt_string, is_ok, error_message = PDBQTWriterLegacy.write_string(setups[0])
    if not is_ok:
        raise LigandPreparationError(f"meeko could not write PDBQT: {error_message}")
    return pdbqt_string


def prepare_ligand_for_docking(record: LigandRecord, seed: int = 42) -> PreparedLigand:
    if has_three_dimensional_conformer(record.mol):
        mol = Chem.AddHs(record.mol, addCoords=True)
    else:
        mol = generate_three_dimensional_conformer(record.mol, seed=seed)
    return PreparedLigand(name=record.name, pdbqt=ligand_pdbqt_string(mol), mol=mol)


def prepare_ligand_library(
    records: list[LigandRecord], seed: int = 42
) -> tuple[list[PreparedLigand], list[LigandFailure]]:
    prepared: list[PreparedLigand] = []
    failures: list[LigandFailure] = []
    for record in records:
        try:
            prepared.append(prepare_ligand_for_docking(record, seed=seed))
        except Exception as error:
            failures.append(LigandFailure(name=record.name, reason=str(error)))
    return prepared, failures


def canonical_smiles(mol: Chem.Mol) -> str:
    return Chem.MolToSmiles(Chem.RemoveHs(mol))


def molecule_from_pdb_block_with_template(pdb_block: str, template: Chem.Mol) -> Chem.Mol:
    raw = Chem.MolFromPDBBlock(pdb_block, removeHs=True, sanitize=False, proximityBonding=True)
    if raw is None:
        raise LigandPreparationError("co-crystallized ligand block could not be parsed")
    raw.UpdatePropertyCache(strict=False)
    Chem.SanitizeMol(
        raw,
        sanitizeOps=Chem.SanitizeFlags.SANITIZE_ALL
        ^ Chem.SanitizeFlags.SANITIZE_PROPERTIES
        ^ Chem.SanitizeFlags.SANITIZE_KEKULIZE,
        catchErrors=True,
    )
    return AssignBondOrdersFromTemplate(Chem.RemoveHs(template), raw)
