THREE_LETTER_TO_ONE_LETTER = {
    "ALA": "A",
    "ARG": "R",
    "ASN": "N",
    "ASP": "D",
    "CYS": "C",
    "GLN": "Q",
    "GLU": "E",
    "GLY": "G",
    "HIS": "H",
    "ILE": "I",
    "LEU": "L",
    "LYS": "K",
    "MET": "M",
    "MSE": "M",
    "PHE": "F",
    "PRO": "P",
    "SEC": "U",
    "SER": "S",
    "THR": "T",
    "TRP": "W",
    "TYR": "Y",
    "VAL": "V",
}

UNKNOWN_RESIDUE = "X"
MINIMUM_USABLE_SEQUENCE_LENGTH = 16


class SequenceExtractionError(Exception):
    pass


def _first_model_lines(pdb_text: str) -> list[str]:
    lines: list[str] = []
    for line in pdb_text.splitlines():
        if line.startswith("ENDMDL"):
            break
        lines.append(line)
    return lines


def _first_seqres_chain(pdb_text: str) -> str | None:
    for line in pdb_text.splitlines():
        if line.startswith("SEQRES") and len(line) > 11:
            return line[11]
    return None


def sequence_from_seqres_records(pdb_text: str, chain: str | None = None) -> str:
    wanted_chain = chain if chain is not None else _first_seqres_chain(pdb_text)
    if wanted_chain is None:
        return ""
    residues: list[str] = []
    for line in pdb_text.splitlines():
        if not line.startswith("SEQRES") or len(line) <= 11:
            continue
        if line[11] != wanted_chain:
            continue
        for token in line[19:].split():
            residues.append(THREE_LETTER_TO_ONE_LETTER.get(token, UNKNOWN_RESIDUE))
    return "".join(residues)


def sequence_from_alpha_carbon_records(pdb_text: str, chain: str | None = None) -> str:
    residues: list[str] = []
    seen_chain: str | None = None
    for line in _first_model_lines(pdb_text):
        if not line.startswith("ATOM"):
            continue
        if line[12:16].strip() != "CA":
            continue
        if line[16] not in (" ", "A"):
            continue
        chain_id = line[21]
        if chain is not None and chain_id != chain:
            continue
        if chain is None:
            if seen_chain is None:
                seen_chain = chain_id
            elif chain_id != seen_chain:
                continue
        residues.append(THREE_LETTER_TO_ONE_LETTER.get(line[17:20].strip(), UNKNOWN_RESIDUE))
    return "".join(residues)


def sequence_from_fasta_text(fasta_text: str) -> str:
    residues: list[str] = []
    for line in fasta_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            if residues:
                break
            continue
        residues.append(stripped.upper())
    return "".join(residues)


def protein_sequence_from_structure_text(structure_text: str, chain: str | None = None) -> str:
    if structure_text.lstrip().startswith(">"):
        sequence = sequence_from_fasta_text(structure_text)
    else:
        sequence = sequence_from_seqres_records(structure_text, chain=chain)
        if len(sequence) < MINIMUM_USABLE_SEQUENCE_LENGTH:
            sequence = sequence_from_alpha_carbon_records(structure_text, chain=chain)

    if len(sequence) < MINIMUM_USABLE_SEQUENCE_LENGTH:
        raise SequenceExtractionError(
            "could not recover a usable protein sequence from the target structure "
            f"(got {len(sequence)} residues, need at least {MINIMUM_USABLE_SEQUENCE_LENGTH})"
        )
    return sequence
