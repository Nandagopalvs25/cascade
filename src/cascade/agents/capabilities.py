from enum import StrEnum

from cascade.schemas import Workload

POSED_COMPLEX_FILE_NAME = "poses.sdf"


class InputKind(StrEnum):
    PROTEIN_STRUCTURE = "protein_structure"
    PROTEIN_SEQUENCE = "protein_sequence"
    LIGAND_STRUCTURES = "ligand_structures"
    POSED_COMPLEXES = "posed_complexes"


STAGE_REQUIREMENTS: dict[Workload, frozenset[InputKind]] = {
    "dock": frozenset({InputKind.PROTEIN_STRUCTURE, InputKind.LIGAND_STRUCTURES}),
    "admet": frozenset({InputKind.LIGAND_STRUCTURES}),
    "md_stability": frozenset({InputKind.PROTEIN_STRUCTURE, InputKind.POSED_COMPLEXES}),
    "cofold": frozenset({InputKind.PROTEIN_SEQUENCE, InputKind.LIGAND_STRUCTURES}),
}

PRODUCED_FILE_NAMES_BY_WORKLOAD: dict[Workload, dict[InputKind, str]] = {
    "dock": {InputKind.POSED_COMPLEXES: POSED_COMPLEX_FILE_NAME},
    "admet": {},
    "md_stability": {},
    "cofold": {},
}

INPUT_KINDS_DERIVED_FROM: dict[InputKind, frozenset[InputKind]] = {
    InputKind.PROTEIN_SEQUENCE: frozenset({InputKind.PROTEIN_STRUCTURE}),
}

LIGAND_BEARING_INPUT_KINDS = (InputKind.POSED_COMPLEXES, InputKind.LIGAND_STRUCTURES)


def required_inputs_for_stage(stage: str) -> frozenset[InputKind]:
    return STAGE_REQUIREMENTS.get(stage, frozenset())


def inputs_produced_by_stage(stage: str) -> frozenset[InputKind]:
    return frozenset(PRODUCED_FILE_NAMES_BY_WORKLOAD.get(stage, {}))


def produced_file_name(stage: str, kind: InputKind) -> str | None:
    return PRODUCED_FILE_NAMES_BY_WORKLOAD.get(stage, {}).get(kind)


def inputs_available_after_derivation(available: frozenset[InputKind]) -> frozenset[InputKind]:
    return available | {
        kind for kind, sources in INPUT_KINDS_DERIVED_FROM.items() if sources & available
    }


def unmet_inputs_for_stage(stage: str, available: frozenset[InputKind]) -> frozenset[InputKind]:
    return required_inputs_for_stage(stage) - inputs_available_after_derivation(available)


def ligand_input_kind_for_stage(stage: str) -> InputKind | None:
    required = required_inputs_for_stage(stage)
    return next((kind for kind in LIGAND_BEARING_INPUT_KINDS if kind in required), None)


def stage_requires_a_protein_structure(stage: str) -> bool:
    return any(
        kind is InputKind.PROTEIN_STRUCTURE
        or InputKind.PROTEIN_STRUCTURE in INPUT_KINDS_DERIVED_FROM.get(kind, frozenset())
        for kind in required_inputs_for_stage(stage)
    )


UNMET_REQUIREMENT_QUESTIONS = {
    InputKind.POSED_COMPLEXES: (
        "{stage} works on protein-ligand complexes that are already posed in the pocket, and this "
        "card offers none. Attach an SDF carrying 3D coordinates, or run a stage that produces "
        "poses first and use the follow-up card CASCADE proposes from it."
    ),
    InputKind.LIGAND_STRUCTURES: (
        "{stage} needs compound structures and CASCADE could read none from this card. Paste "
        "SMILES into the description, attach a .smi or .sdf library, or link one."
    ),
    InputKind.PROTEIN_STRUCTURE: (
        "{stage} needs a protein structure and this card names none. Give a 4-character PDB ID, "
        "attach a structure file, or link one."
    ),
    InputKind.PROTEIN_SEQUENCE: (
        "{stage} needs a protein sequence and this card names no structure to take one from. "
        "Give a 4-character PDB ID, attach a structure file, or link one."
    ),
}


def unmet_requirement_question(kind: InputKind, stage: str) -> str:
    return UNMET_REQUIREMENT_QUESTIONS[kind].format(stage=stage)
