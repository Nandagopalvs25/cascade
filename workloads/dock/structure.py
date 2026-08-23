from dataclasses import dataclass, field

from job_spec import BindingSite

WATER_RESIDUE_NAMES = frozenset({"HOH", "DOD", "WAT", "H2O"})

NON_LIGAND_HETERO_RESIDUE_NAMES = frozenset(
    {
        "ACE",
        "ACT",
        "ACY",
        "BME",
        "BR",
        "CA",
        "CD",
        "CL",
        "CO",
        "CO3",
        "CU",
        "DMS",
        "EDO",
        "EPE",
        "F",
        "FE",
        "FE2",
        "FMT",
        "GOL",
        "HED",
        "IOD",
        "IPA",
        "K",
        "MES",
        "MG",
        "MLI",
        "MN",
        "MOH",
        "NA",
        "NH2",
        "NH4",
        "NI",
        "NO3",
        "PEG",
        "PG4",
        "PGE",
        "PO4",
        "SCN",
        "SO4",
        "TRS",
        "ZN",
    }
)

MINIMUM_LIGAND_HEAVY_ATOMS = 6
MINIMUM_BOX_EDGE = 16.0
MAXIMUM_BOX_EDGE = 30.0


@dataclass(frozen=True)
class StructureAtom:
    name: str
    element: str
    x: float
    y: float
    z: float


@dataclass
class HeteroResidue:
    residue_name: str
    chain_id: str
    residue_number: str
    atoms: list[StructureAtom] = field(default_factory=list)
    pdb_lines: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.residue_name}:{self.chain_id or '_'}:{self.residue_number}"

    @property
    def heavy_atoms(self) -> list[StructureAtom]:
        return [atom for atom in self.atoms if atom.element != "H"]

    @property
    def heavy_atom_count(self) -> int:
        return len(self.heavy_atoms)

    def to_pdb_block(self) -> str:
        return "\n".join([*self.pdb_lines, "END", ""])


def _element_symbol(line: str) -> str:
    symbol = line[76:78].strip()
    if symbol:
        return symbol.capitalize()
    fallback = line[12:16].strip()
    return fallback[:1].capitalize() if fallback else ""


def _atom_from_pdb_line(line: str) -> StructureAtom:
    return StructureAtom(
        name=line[12:16].strip(),
        element=_element_symbol(line),
        x=float(line[30:38]),
        y=float(line[38:46]),
        z=float(line[46:54]),
    )


def _first_model_lines(pdb_text: str) -> list[str]:
    lines: list[str] = []
    for line in pdb_text.splitlines():
        if line.startswith("ENDMDL"):
            break
        lines.append(line)
    return lines


def extract_protein_receptor_pdb(pdb_text: str, chain: str | None = None) -> str:
    kept: list[str] = []
    for line in _first_model_lines(pdb_text):
        if line.startswith("TER"):
            if chain is None or (len(line) > 21 and line[21] == chain):
                kept.append(line)
            continue
        if not line.startswith("ATOM"):
            continue
        if chain is not None and line[21] != chain:
            continue
        if line[16] not in (" ", "A"):
            continue
        if line[17:20].strip() in WATER_RESIDUE_NAMES:
            continue
        kept.append(line)
    if not kept:
        raise ValueError(
            f"no protein ATOM records found in structure for chain {chain!r}"
            if chain
            else "no protein ATOM records found in structure"
        )
    return "\n".join([*kept, "END", ""])


def receptor_chain_ids(receptor_pdb: str) -> list[str]:
    seen: list[str] = []
    for line in receptor_pdb.splitlines():
        if line.startswith("ATOM"):
            chain_id = line[21]
            if chain_id not in seen:
                seen.append(chain_id)
    return seen


def receptor_atom_count(receptor_pdb: str) -> int:
    return sum(1 for line in receptor_pdb.splitlines() if line.startswith("ATOM"))


def find_cocrystal_ligands(pdb_text: str, chain: str | None = None) -> list[HeteroResidue]:
    residues: dict[tuple[str, str, str], HeteroResidue] = {}
    for line in _first_model_lines(pdb_text):
        if not line.startswith("HETATM"):
            continue
        residue_name = line[17:20].strip()
        if residue_name in WATER_RESIDUE_NAMES:
            continue
        if residue_name in NON_LIGAND_HETERO_RESIDUE_NAMES:
            continue
        if line[16] not in (" ", "A"):
            continue
        chain_id = line[21]
        if chain is not None and chain_id != chain:
            continue
        key = (residue_name, chain_id, line[22:27].strip())
        residue = residues.setdefault(
            key,
            HeteroResidue(residue_name=residue_name, chain_id=chain_id, residue_number=key[2]),
        )
        residue.atoms.append(_atom_from_pdb_line(line))
        residue.pdb_lines.append(line)

    candidates = [
        residue
        for residue in residues.values()
        if residue.heavy_atom_count >= MINIMUM_LIGAND_HEAVY_ATOMS
    ]
    return sorted(candidates, key=lambda residue: residue.heavy_atom_count, reverse=True)


def binding_site_from_atoms(
    atoms: list[StructureAtom],
    padding: float = 5.0,
    minimum_edge: float = MINIMUM_BOX_EDGE,
    maximum_edge: float = MAXIMUM_BOX_EDGE,
) -> BindingSite:
    if not atoms:
        raise ValueError("cannot derive a binding site from an empty atom list")

    coordinates = [(atom.x, atom.y, atom.z) for atom in atoms]
    centers: list[float] = []
    edges: list[float] = []
    for axis in range(3):
        values = [coordinate[axis] for coordinate in coordinates]
        lowest, highest = min(values), max(values)
        centers.append(round((lowest + highest) / 2, 3))
        edges.append(round(min(max(highest - lowest + 2 * padding, minimum_edge), maximum_edge), 1))

    return BindingSite(
        center_x=centers[0],
        center_y=centers[1],
        center_z=centers[2],
        size_x=edges[0],
        size_y=edges[1],
        size_z=edges[2],
    )
