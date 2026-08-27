import csv
import io
from dataclasses import dataclass
from pathlib import Path

from rdkit import Chem, RDLogger

RDLogger.DisableLog("rdApp.*")

SDF_SUFFIXES = frozenset({".sdf", ".sd", ".mol"})
SMILES_SUFFIXES = frozenset({".smi", ".smiles", ".txt"})
SDF_RECORD_SEPARATOR = "$$$$"
CSV_SMILES_COLUMN_NAMES = ("smiles", "canonical_smiles", "structure")
CSV_NAME_COLUMN_NAMES = ("name", "id", "compound_id", "compound", "title")


@dataclass
class CompoundRecord:
    name: str
    mol: Chem.Mol


@dataclass
class CompoundFailure:
    name: str
    reason: str


def _fallback_compound_name(index: int) -> str:
    return f"CMP-{index + 1:03d}"


def _molecule_from_mol_block(block: str) -> tuple[Chem.Mol | None, str]:
    mol = Chem.MolFromMolBlock(block)
    if mol is not None:
        return mol, ""
    if Chem.MolFromMolBlock(block, sanitize=False) is None:
        return None, "molblock could not be parsed"
    return None, "molblock parsed but failed chemistry sanitization"


def _molecule_from_smiles(smiles: str) -> tuple[Chem.Mol | None, str]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is not None:
        return mol, ""
    if Chem.MolFromSmiles(smiles, sanitize=False) is None:
        return None, f"SMILES could not be parsed: {smiles}"
    return None, f"SMILES parsed but failed chemistry sanitization: {smiles}"


def _compounds_from_sdf_text(text: str) -> tuple[list[CompoundRecord], list[CompoundFailure]]:
    records: list[CompoundRecord] = []
    failures: list[CompoundFailure] = []
    blocks = [block for block in text.split(SDF_RECORD_SEPARATOR) if block.strip()]
    for index, block in enumerate(blocks):
        molblock = block.lstrip("\n")
        lines = molblock.splitlines()
        name = (lines[0].strip() if lines else "") or _fallback_compound_name(index)
        mol, reason = _molecule_from_mol_block(molblock)
        if mol is None:
            failures.append(CompoundFailure(name=name, reason=reason))
            continue
        mol.SetProp("_Name", name)
        records.append(CompoundRecord(name=name, mol=mol))
    return records, failures


def _compounds_from_smiles_lines(text: str) -> tuple[list[CompoundRecord], list[CompoundFailure]]:
    records: list[CompoundRecord] = []
    failures: list[CompoundFailure] = []
    index = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        smiles = fields[0]
        name = fields[1] if len(fields) > 1 else _fallback_compound_name(index)
        index += 1
        mol, reason = _molecule_from_smiles(smiles)
        if mol is None:
            failures.append(CompoundFailure(name=name, reason=reason))
            continue
        mol.SetProp("_Name", name)
        records.append(CompoundRecord(name=name, mol=mol))
    return records, failures


def _column_value(row: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    lowered = {key.strip().lower(): value for key, value in row.items() if key}
    for candidate in candidates:
        value = lowered.get(candidate)
        if value and value.strip():
            return value.strip()
    return None


def _compounds_from_csv_text(text: str) -> tuple[list[CompoundRecord], list[CompoundFailure]]:
    records: list[CompoundRecord] = []
    failures: list[CompoundFailure] = []
    for index, row in enumerate(csv.DictReader(io.StringIO(text))):
        smiles = _column_value(row, CSV_SMILES_COLUMN_NAMES)
        name = _column_value(row, CSV_NAME_COLUMN_NAMES) or _fallback_compound_name(index)
        if smiles is None:
            failures.append(CompoundFailure(name=name, reason="row has no SMILES column"))
            continue
        mol, reason = _molecule_from_smiles(smiles)
        if mol is None:
            failures.append(CompoundFailure(name=name, reason=reason))
            continue
        mol.SetProp("_Name", name)
        records.append(CompoundRecord(name=name, mol=mol))
    return records, failures


def load_compound_library(path: str | Path) -> tuple[list[CompoundRecord], list[CompoundFailure]]:
    library_path = Path(path)
    suffix = library_path.suffix.lower()
    text = library_path.read_text()
    if suffix in SDF_SUFFIXES:
        return _compounds_from_sdf_text(text)
    if suffix in SMILES_SUFFIXES:
        return _compounds_from_smiles_lines(text)
    if suffix == ".csv":
        return _compounds_from_csv_text(text)
    raise ValueError(f"unsupported compound library format: {library_path.name}")


def canonical_smiles(mol: Chem.Mol) -> str:
    return Chem.MolToSmiles(Chem.RemoveHs(mol))
