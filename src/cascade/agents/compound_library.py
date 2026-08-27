import re
from pathlib import PurePosixPath

from cascade.agents.schemas import CampaignIntent, LigandLibrary

LIGAND_SUFFIXES = frozenset({".sdf", ".sd", ".mol", ".smi", ".smiles", ".txt", ".csv"})

DEFAULT_LIGAND_FILENAME = "ligands.smi"

SDF_RECORD_SEPARATOR = "$$$$"

BRACKET_ATOM_PATTERN = re.compile(r"\[[^\]]*\]")

SMILES_BODY_PATTERN = re.compile(r"^[BCNOPSFIHlrbcnops0-9()=#@+\-/\\%.*]{2,}$")

ELEMENT_CHARACTERS = frozenset("BCNOPSFIHbcnops")


def unusable_library_questions(intent: CampaignIntent, library: LigandLibrary) -> list[str]:
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
    if filename.endswith((".sdf", ".sd", ".mol")):
        return len([block for block in text.split(SDF_RECORD_SEPARATOR) if block.strip()])
    if filename.endswith(".csv"):
        return max(len([line for line in text.splitlines() if line.strip()]) - 1, 0)
    return len(
        [line.strip() for line in text.splitlines() if line.strip() and line.strip()[0] != "#"]
    )
