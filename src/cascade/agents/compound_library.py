import re
from pathlib import PurePosixPath

from cascade.agents.schemas import CampaignIntent, LigandLibrary

SDF_FILENAME_SUFFIXES = (".sdf", ".sd", ".mol")

LIGAND_SUFFIXES = frozenset({*SDF_FILENAME_SUFFIXES, ".smi", ".smiles", ".txt", ".csv"})

DEFAULT_LIGAND_FILENAME = "ligands.smi"

SDF_RECORD_SEPARATOR = "$$$$"

BRACKET_ATOM_PATTERN = re.compile(r"\[[^\]]*\]")

SMILES_BODY_PATTERN = re.compile(r"^[BCNOPSFIHlrbcnops0-9()=#@+\-/\\%.*]{2,}$")

ELEMENT_CHARACTERS = frozenset("BCNOPSFIHbcnops")


INHERITED_LIBRARY_SOURCES = frozenset({"parent_run_poses", "parent_run_library"})


def unusable_library_questions(intent: CampaignIntent, library: LigandLibrary | None) -> list[str]:
    if library is None or library.source in INHERITED_LIBRARY_SOURCES:
        return []
    questions = []
    named_control = (intent.control_compound or "").strip().lower()
    known_names = {name.lower() for name in library.compound_names}
    if named_control and known_names and named_control not in known_names:
        questions.append(
            f"The control compound {intent.control_compound!r} is not among the "
            f"{library.compound_count} compounds CASCADE could read "
            f"({', '.join(library.compound_names)}). Without it the run cannot be validated. "
            f"Trello rewrites SMILES containing bracketed stereocentres because [X](Y) is "
            f"Markdown link syntax — attach a .smi file or wrap each SMILES in backticks."
        )
    if (
        intent.expected_compound_count is not None
        and intent.expected_compound_count != library.compound_count
    ):
        questions.append(
            f"The card lists {intent.expected_compound_count} compounds but CASCADE could only "
            f"read {library.compound_count}. Check the SMILES that did not survive: Trello "
            f"rewrites [X](Y) sequences as Markdown links."
        )
    return questions


def looks_like_smiles(token: str) -> bool:
    outside_brackets = BRACKET_ATOM_PATTERN.sub("", token)
    if not SMILES_BODY_PATTERN.match(outside_brackets):
        return False
    return bool(set(outside_brackets) & ELEMENT_CHARACTERS)


def smiles_library_lines_from_text(text: str) -> tuple[list[str], int]:
    kept: list[str] = []
    skipped = 0
    for line in text.splitlines():
        stripped = line.replace("`", "").strip().lstrip("-*•").strip()
        if not stripped or stripped.startswith("#"):
            continue
        token, *name_fields = stripped.split()
        if not looks_like_smiles(token):
            skipped += 1
            continue
        name = "_".join(name_fields) if name_fields else f"compound_{len(kept) + 1}"
        kept.append(f"{token}\t{name}")
    return kept, skipped


def compound_names_from_library_lines(lines: list[str]) -> list[str]:
    return [line.split("\t")[1] for line in lines if "\t" in line]


def ligand_filename_for_reference(reference: str) -> str:
    suffix = PurePosixPath(reference).suffix.lower()
    if suffix in LIGAND_SUFFIXES:
        return f"ligands{suffix}"
    return DEFAULT_LIGAND_FILENAME


def compound_count_in_library(filename: str, text: str) -> int:
    if filename.endswith(SDF_FILENAME_SUFFIXES):
        return len([block for block in text.split(SDF_RECORD_SEPARATOR) if block.strip()])
    if filename.endswith(".csv"):
        return max(len([line for line in text.splitlines() if line.strip()]) - 1, 0)
    return len(
        [line.strip() for line in text.splitlines() if line.strip() and line.strip()[0] != "#"]
    )


def sdf_record_compound_name(record: str) -> str:
    for line in record.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def sdf_record_ending_in_newline(record: str) -> str:
    body = record.lstrip("\n")
    return body if body.endswith("\n") else f"{body}\n"


def sdf_subset_for_compounds(sdf_text: str, compound_names: list[str]) -> tuple[str, list[str]]:
    wanted = {name.strip().lower() for name in compound_names}
    kept: list[str] = []
    kept_names: list[str] = []
    for record in sdf_text.split(SDF_RECORD_SEPARATOR):
        if not record.strip():
            continue
        name = sdf_record_compound_name(record)
        if name.lower() in wanted:
            kept.append(sdf_record_ending_in_newline(record))
            kept_names.append(name)
    if not kept:
        return "", []
    return "".join(f"{record}{SDF_RECORD_SEPARATOR}\n" for record in kept), kept_names


def smiles_subset_for_compounds(text: str, compound_names: list[str]) -> tuple[str, list[str]]:
    wanted = {name.strip().lower() for name in compound_names}
    lines, _ = smiles_library_lines_from_text(text)
    kept = [line for line in lines if line.split("\t")[1].strip().lower() in wanted]
    if not kept:
        return "", []
    return "\n".join(kept) + "\n", compound_names_from_library_lines(kept)


def library_subset_for_compounds(
    filename: str, text: str, compound_names: list[str]
) -> tuple[str, list[str]]:
    if filename.lower().endswith(SDF_FILENAME_SUFFIXES):
        return sdf_subset_for_compounds(text, compound_names)
    return smiles_subset_for_compounds(text, compound_names)


SDF_COUNTS_LINE_INDEX = 3
SDF_ATOM_BLOCK_FIRST_LINE_INDEX = 4
SDF_Z_COORDINATE_COLUMNS = slice(20, 30)


def sdf_record_atom_z_coordinates(record: str) -> list[float]:
    lines = record.lstrip("\n").splitlines()
    if len(lines) <= SDF_ATOM_BLOCK_FIRST_LINE_INDEX:
        return []
    try:
        atom_count = int(lines[SDF_COUNTS_LINE_INDEX][:3])
    except ValueError:
        return []
    coordinates: list[float] = []
    for line in lines[
        SDF_ATOM_BLOCK_FIRST_LINE_INDEX : SDF_ATOM_BLOCK_FIRST_LINE_INDEX + atom_count
    ]:
        try:
            coordinates.append(float(line[SDF_Z_COORDINATE_COLUMNS]))
        except ValueError:
            return []
    return coordinates


def sdf_text_carries_three_dimensional_coordinates(sdf_text: str) -> bool:
    return any(
        any(coordinate != 0.0 for coordinate in sdf_record_atom_z_coordinates(record))
        for record in sdf_text.split(SDF_RECORD_SEPARATOR)
        if record.strip()
    )
