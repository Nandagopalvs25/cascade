from pathlib import PurePosixPath

from google.api_core import exceptions as google_api_exceptions
from google.auth.exceptions import GoogleAuthError
from pydantic import ValidationError

from cascade.agents.capabilities import (
    POSED_COMPLEX_FILE_NAME,
    InputKind,
    inputs_produced_by_stage,
    ligand_input_kind_for_stage,
    produced_file_name,
    stage_requires_a_protein_structure,
    unmet_requirement_question,
)
from cascade.agents.compound_library import (
    DEFAULT_LIGAND_FILENAME,
    SDF_FILENAME_SUFFIXES,
    compound_count_in_library,
    compound_names_from_library_lines,
    library_subset_for_compounds,
    ligand_filename_for_reference,
    sdf_subset_for_compounds,
    sdf_text_carries_three_dimensional_coordinates,
    smiles_library_lines_from_text,
)
from cascade.agents.persistence import (
    load_results_prefix_for_run,
    load_triage_decision_for_run,
    runs_in_lineage,
)
from cascade.agents.policy import compounds_carried_to_next_stage
from cascade.agents.schemas import (
    CampaignIntent,
    LigandLibrary,
    LineageRun,
    ResolvedStageInputs,
    StageInputRequest,
    TriageVerdict,
    UnmetRequirement,
)
from cascade.clients import campaign_inputs, gcs
from cascade.clients.gcs import run_inputs_prefix
from cascade.schemas import TargetRequest, TargetStructure

SDF_CONTENT_TYPE = "chemical/x-mdl-sdfile"
PLAIN_TEXT_CONTENT_TYPE = "text/plain"

INPUT_READ_FAILURES = (
    google_api_exceptions.GoogleAPICallError,
    GoogleAuthError,
    ValueError,
    OSError,
)


def compound_names_named_on_the_card(card_description: str) -> list[str]:
    lines, _ = smiles_library_lines_from_text(card_description)
    return compound_names_from_library_lines(lines)


def pose_attachment_name(attachment_names: list[str]) -> str | None:
    return next(
        (name for name in attachment_names if name.lower().endswith(SDF_FILENAME_SUFFIXES)),
        None,
    )


async def compound_names_carried_by_run(run_id: str) -> list[str]:
    decision = await load_triage_decision_for_run(run_id)
    if not decision:
        return []
    try:
        verdict = TriageVerdict.model_validate(decision.get("output") or {})
    except ValidationError:
        return []
    carried, _ = compounds_carried_to_next_stage(verdict)
    return [judgement.compound_id for judgement in carried]


async def compound_names_carried_onto_the_card(
    request: StageInputRequest, lineage: list[LineageRun]
) -> list[str]:
    named = compound_names_named_on_the_card(request.card_description)
    if named or not lineage:
        return named
    return await compound_names_carried_by_run(lineage[0].run_id)


async def posed_complexes_from_an_earlier_run(
    request: StageInputRequest, lineage: list[LineageRun]
) -> tuple[LigandLibrary | None, str | None]:
    carried = await compound_names_carried_onto_the_card(request, lineage)
    for ancestor in lineage:
        file_name = produced_file_name(ancestor.workload, InputKind.POSED_COMPLEXES)
        if file_name is None:
            continue
        results_prefix = await load_results_prefix_for_run(ancestor.run_id)
        if not results_prefix:
            continue
        poses_uri = f"{results_prefix.rstrip('/')}/{file_name}"
        try:
            sdf_text = await gcs.download_text_from_uri(poses_uri)
        except INPUT_READ_FAILURES:
            continue
        filtered, kept_names = sdf_subset_for_compounds(sdf_text, carried)
        if not kept_names:
            return None, (
                f"Run `{ancestor.run_id}` produced posed complexes, but none of the compounds "
                f"named on this card ({', '.join(carried) or 'none named'}) are among them. Name "
                f"the compounds exactly as the {ancestor.workload} stage reported them."
            )
        ligands_uri = await gcs.upload_bytes(
            f"{run_inputs_prefix(request.run_id)}/{POSED_COMPLEX_FILE_NAME}",
            filtered.encode(),
            SDF_CONTENT_TYPE,
        )
        return (
            LigandLibrary(
                ligands_uri=ligands_uri,
                compound_count=len(kept_names),
                source="parent_run_poses",
                compound_names=carried,
            ),
            None,
        )
    return None, None


async def posed_complexes_from_a_card_attachment(
    request: StageInputRequest,
) -> tuple[LigandLibrary | None, str | None]:
    attachment_name = pose_attachment_name(request.attachment_names)
    if attachment_name is None:
        return None, None
    content = await campaign_inputs.download_named_card_attachment(request.card_id, attachment_name)
    sdf_text = content.decode("utf-8", "replace")
    if not sdf_text.strip():
        return None, f"The attachment {attachment_name!r} is empty."
    if not sdf_text_carries_three_dimensional_coordinates(sdf_text):
        return None, (
            f"The attachment {attachment_name!r} holds flat 2D structures. {request.stage} "
            f"re-simulates complexes that already sit in the pocket, so it needs an SDF carrying "
            f"3D coordinates. Attach one, or run a stage that produces poses first and use the "
            f"follow-up card CASCADE proposes from it."
        )
    ligands_uri = await gcs.upload_bytes(
        f"{run_inputs_prefix(request.run_id)}/{POSED_COMPLEX_FILE_NAME}",
        content,
        SDF_CONTENT_TYPE,
    )
    return (
        LigandLibrary(
            ligands_uri=ligands_uri,
            compound_count=compound_count_in_library(POSED_COMPLEX_FILE_NAME, sdf_text),
            source="attachment",
            compound_names=compound_names_named_on_the_card(request.card_description),
        ),
        None,
    )


async def resolve_posed_complexes(
    request: StageInputRequest, lineage: list[LineageRun]
) -> tuple[LigandLibrary | None, str | None]:
    library, reason = await posed_complexes_from_an_earlier_run(request, lineage)
    if library is not None or reason is not None:
        return library, reason
    return await posed_complexes_from_a_card_attachment(request)


async def resolve_ligand_structures(
    request: StageInputRequest,
) -> tuple[LigandLibrary | None, str | None]:
    intent = request.intent
    if not intent.ligand_source or not intent.ligand_reference:
        return None, None

    skipped = 0
    compound_names: list[str] = []
    if intent.ligand_source == "smiles_in_text":
        lines, skipped = smiles_library_lines_from_text(request.card_description)
        if not lines:
            return None, (
                "CASCADE could not read a single SMILES from this card. Trello rewrites [X](Y) "
                "sequences as Markdown links, so wrap each SMILES in backticks or attach a .smi "
                "file."
            )
        filename = DEFAULT_LIGAND_FILENAME
        content = ("\n".join(lines) + "\n").encode()
        compound_names = compound_names_from_library_lines(lines)
    else:
        try:
            if intent.ligand_source == "attachment":
                content = await campaign_inputs.download_named_card_attachment(
                    request.card_id, intent.ligand_reference
                )
            else:
                content = await campaign_inputs.download_from_url(intent.ligand_reference)
        except INPUT_READ_FAILURES as error:
            return None, (
                f"CASCADE could not read the compound library this card names "
                f"({intent.ligand_reference!r}): {error}"
            )
        if not content.strip():
            return None, f"The compound library {intent.ligand_reference!r} is empty."
        filename = ligand_filename_for_reference(intent.ligand_reference)

    ligands_uri = await gcs.upload_bytes(
        f"{run_inputs_prefix(request.run_id)}/{filename}", content, PLAIN_TEXT_CONTENT_TYPE
    )
    return (
        LigandLibrary(
            ligands_uri=ligands_uri,
            compound_count=compound_count_in_library(filename, content.decode("utf-8", "replace")),
            source=intent.ligand_source,
            compound_names=compound_names,
            skipped_lines=skipped,
        ),
        None,
    )


async def ligand_structures_from_an_earlier_run(
    request: StageInputRequest, lineage: list[LineageRun]
) -> tuple[LigandLibrary | None, str | None]:
    if not lineage:
        return None, None
    carried = await compound_names_carried_onto_the_card(request, lineage)
    if not carried:
        return None, None

    for ancestor in lineage:
        if not ancestor.ligands_uri:
            continue
        filename = PurePosixPath(ancestor.ligands_uri).name or DEFAULT_LIGAND_FILENAME
        try:
            library_text = await gcs.download_text_from_uri(ancestor.ligands_uri)
        except INPUT_READ_FAILURES:
            continue
        subset, kept_names = library_subset_for_compounds(filename, library_text, carried)
        if not kept_names:
            continue
        is_sdf = filename.lower().endswith(SDF_FILENAME_SUFFIXES)
        ligands_uri = await gcs.upload_bytes(
            f"{run_inputs_prefix(request.run_id)}/{filename}",
            subset.encode(),
            SDF_CONTENT_TYPE if is_sdf else PLAIN_TEXT_CONTENT_TYPE,
        )
        return (
            LigandLibrary(
                ligands_uri=ligands_uri,
                compound_count=len(kept_names),
                source="parent_run_library",
                compound_names=kept_names,
            ),
            None,
        )

    return None, (
        f"This card carries {len(carried)} compound(s) forward from run "
        f"`{lineage[0].run_id}`, but CASCADE could not recover their structures from any compound "
        f"library recorded against that run or its ancestors. Attach a .smi or .sdf library to "
        f"this card."
    )


async def resolve_protein_structure(
    request: StageInputRequest, lineage: list[LineageRun]
) -> tuple[TargetStructure | None, str | None]:
    intent = request.intent
    if intent.target_source and intent.target_reference:
        try:
            return (
                await campaign_inputs.resolve_target_structure(
                    TargetRequest(
                        run_id=request.run_id,
                        card_id=request.card_id,
                        source=intent.target_source,
                        reference=intent.target_reference,
                    )
                ),
                None,
            )
        except INPUT_READ_FAILURES as error:
            return None, (
                f"CASCADE could not read the structure this card names "
                f"({intent.target_source} {intent.target_reference!r}): {error}"
            )
    return next(
        (ancestor.target for ancestor in lineage if ancestor.target is not None), None
    ), None


async def resolve_compound_library(
    request: StageInputRequest, lineage: list[LineageRun]
) -> tuple[LigandLibrary | None, str | None]:
    ligand_kind = ligand_input_kind_for_stage(request.stage)
    if ligand_kind is InputKind.POSED_COMPLEXES:
        return await resolve_posed_complexes(request, lineage)
    if ligand_kind is not InputKind.LIGAND_STRUCTURES:
        return None, None

    library, reason = await resolve_ligand_structures(request)
    if library is not None:
        return library, None
    inherited, inherited_reason = await ligand_structures_from_an_earlier_run(request, lineage)
    if inherited is not None:
        return inherited, None
    return None, inherited_reason or reason


async def resolve_stage_inputs(request: StageInputRequest) -> ResolvedStageInputs:
    lineage = await runs_in_lineage(request.parent_run_id) if request.parent_run_id else []
    unmet: list[UnmetRequirement] = []

    ligand_kind = ligand_input_kind_for_stage(request.stage)
    library, reason = await resolve_compound_library(request, lineage)
    if ligand_kind is not None and library is None:
        unmet.append(
            UnmetRequirement(
                kind=ligand_kind,
                question=reason or unmet_requirement_question(ligand_kind, request.stage),
            )
        )

    target: TargetStructure | None = None
    if stage_requires_a_protein_structure(request.stage):
        target, reason = await resolve_protein_structure(request, lineage)
        if target is None:
            unmet.append(
                UnmetRequirement(
                    kind=InputKind.PROTEIN_STRUCTURE,
                    question=reason
                    or unmet_requirement_question(InputKind.PROTEIN_STRUCTURE, request.stage),
                )
            )

    return ResolvedStageInputs(library=library, target=target, unmet=unmet)


async def input_kinds_available_to_a_campaign(
    intent: CampaignIntent,
    attachment_names: list[str],
    parent_run_id: str | None,
    carried_compound_count: int = 0,
) -> frozenset[InputKind]:
    available: set[InputKind] = set()
    if carried_compound_count or (intent.ligand_source and intent.ligand_reference):
        available.add(InputKind.LIGAND_STRUCTURES)
    if intent.target_source and intent.target_reference:
        available.add(InputKind.PROTEIN_STRUCTURE)
    if pose_attachment_name(attachment_names) is not None:
        available.add(InputKind.POSED_COMPLEXES)
    for ancestor in await runs_in_lineage(parent_run_id) if parent_run_id else []:
        available |= inputs_produced_by_stage(ancestor.workload)
        if ancestor.target is not None:
            available.add(InputKind.PROTEIN_STRUCTURE)
        if ancestor.ligands_uri:
            available.add(InputKind.LIGAND_STRUCTURES)
    return frozenset(available)
