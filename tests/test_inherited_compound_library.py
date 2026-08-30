import asyncio
from unittest.mock import AsyncMock, patch

from cascade.agents.capabilities import InputKind
from cascade.agents.compound_library import library_subset_for_compounds
from cascade.agents.input_resolution import (
    input_kinds_available_to_a_campaign,
    ligand_structures_from_an_earlier_run,
    posed_complexes_from_an_earlier_run,
)
from cascade.agents.schemas import CampaignIntent, LineageRun, StageInputRequest

PARENT_RUN_ID = "11111111-1111-5111-8111-111111111111"
THIS_RUN_ID = "22222222-2222-5222-8222-222222222222"

INDINAVIR_SMILES = (
    "CC(C)(C)NC(=O)[C@@H]1CN(Cc2cccnc2)CCN1C[C@@H](O)C[C@@H](Cc1ccccc1)C(=O)N[C@H]1c2ccccc2C[C@H]1O"
)
SMILES_LIBRARY_TEXT = (
    f"{INDINAVIR_SMILES}\tindinavir\n"
    "CC(C)Cc1ccc(cc1)C(C)C(=O)O\tibuprofen\n"
    "Cn1cnc2c1c(=O)n(C)c(=O)n2C\tcaffeine\n"
)

POSES_SDF_TEXT = """indinavir
     RDKit          3D

  1  0  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
M  END
>  <compound_id>  (1)
indinavir

$$$$
caffeine
     RDKit          3D

  1  0  0  0  0  0  0  0  0  0999 V2000
    1.0000    1.0000    1.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
M  END
>  <compound_id>  (2)
caffeine

$$$$
"""

CARD_DESCRIPTION_WITHOUT_STRUCTURES = (
    "**Compounds.** 1 compound(s) promoted by the dock stage:\n\n"
    "`indinavir`\n\n"
    f"**Proposed by CASCADE** from run `{PARENT_RUN_ID}` (dock). Drag to To Do to run it."
)


def stage_input_request() -> StageInputRequest:
    return StageInputRequest(
        run_id=THIS_RUN_ID,
        card_id="card-1",
        stage="admet",
        intent=CampaignIntent(rationale="A follow-up card carrying names but no structures."),
        card_description=CARD_DESCRIPTION_WITHOUT_STRUCTURES,
        parent_run_id=PARENT_RUN_ID,
    )


def recording_gcs(library_text: str) -> AsyncMock:
    client = AsyncMock()
    client.download_text_from_uri.return_value = library_text
    client.upload_bytes.side_effect = lambda path, data, content_type: f"gs://test-bucket/{path}"
    return client


def triage_promoting(*compound_ids: str) -> AsyncMock:
    return AsyncMock(
        return_value={
            "output": {
                "run_is_trustworthy": True,
                "results_discriminate": True,
                "next_action": "complete",
                "headline": "One compound separated.",
                "compounds": [
                    {"compound_id": name, "disposition": "promote", "reason": "-11.2 kcal/mol"}
                    for name in compound_ids
                ],
                "rationale": "Recorded verdict.",
            }
        }
    )


def test_only_the_named_compounds_survive_a_smiles_library_subset():
    subset, names = library_subset_for_compounds(
        "ligands.smi", SMILES_LIBRARY_TEXT, ["indinavir", "caffeine"]
    )

    assert names == ["indinavir", "caffeine"]
    assert "ibuprofen" not in subset


def test_only_the_named_compounds_survive_an_sdf_library_subset():
    subset, names = library_subset_for_compounds("poses.sdf", POSES_SDF_TEXT, ["indinavir"])

    assert names == ["indinavir"]
    assert "caffeine" not in subset
    assert subset.count("$$$$") == 1


def test_a_card_naming_compounds_without_structures_recovers_them_from_the_parent_library():
    gcs = recording_gcs(SMILES_LIBRARY_TEXT)
    lineage = [
        LineageRun(
            run_id=PARENT_RUN_ID,
            workload="dock",
            state="succeeded",
            ligands_uri=f"gs://test-bucket/runs/{PARENT_RUN_ID}/inputs/ligands.smi",
        )
    ]

    with (
        patch("cascade.agents.input_resolution.gcs", gcs),
        patch(
            "cascade.agents.input_resolution.load_triage_decision_for_run",
            triage_promoting("indinavir"),
        ),
    ):
        library, reason = asyncio.run(
            ligand_structures_from_an_earlier_run(stage_input_request(), lineage)
        )

    assert reason is None
    assert library is not None
    assert library.source == "parent_run_library"
    assert library.compound_names == ["indinavir"]
    assert library.compound_count == 1
    assert library.ligands_uri == f"gs://test-bucket/runs/{THIS_RUN_ID}/inputs/ligands.smi"

    uploaded = gcs.upload_bytes.await_args.args[1].decode()
    assert INDINAVIR_SMILES in uploaded
    assert "ibuprofen" not in uploaded


def test_poses_from_a_parent_run_are_carried_forward_as_the_compound_library():
    gcs = recording_gcs(POSES_SDF_TEXT)
    lineage = [
        LineageRun(
            run_id=PARENT_RUN_ID,
            workload="md_stability",
            state="succeeded",
            ligands_uri=f"gs://test-bucket/runs/{PARENT_RUN_ID}/inputs/poses.sdf",
        )
    ]

    with (
        patch("cascade.agents.input_resolution.gcs", gcs),
        patch(
            "cascade.agents.input_resolution.load_triage_decision_for_run",
            triage_promoting("indinavir"),
        ),
    ):
        library, reason = asyncio.run(
            ligand_structures_from_an_earlier_run(stage_input_request(), lineage)
        )

    assert reason is None
    assert library is not None
    assert library.ligands_uri.endswith("/inputs/poses.sdf")
    assert library.compound_count == 1
    assert gcs.upload_bytes.await_args.args[2] == "chemical/x-mdl-sdfile"


def test_a_lineage_that_records_no_library_explains_itself_rather_than_failing_silently():
    lineage = [LineageRun(run_id=PARENT_RUN_ID, workload="dock", state="succeeded")]

    with (
        patch("cascade.agents.input_resolution.gcs", recording_gcs("")),
        patch(
            "cascade.agents.input_resolution.load_triage_decision_for_run",
            triage_promoting("indinavir"),
        ),
    ):
        library, reason = asyncio.run(
            ligand_structures_from_an_earlier_run(stage_input_request(), lineage)
        )

    assert library is None
    assert reason is not None
    assert PARENT_RUN_ID in reason


def test_a_run_without_a_parent_leaves_the_generic_unmet_question_in_place():
    library, reason = asyncio.run(ligand_structures_from_an_earlier_run(stage_input_request(), []))

    assert library is None
    assert reason is None


def test_a_parent_library_makes_compound_structures_available_to_the_stage_decision():
    lineage = [
        LineageRun(
            run_id=PARENT_RUN_ID,
            workload="md_stability",
            state="succeeded",
            ligands_uri=f"gs://test-bucket/runs/{PARENT_RUN_ID}/inputs/poses.sdf",
        )
    ]

    with patch("cascade.agents.input_resolution.runs_in_lineage", AsyncMock(return_value=lineage)):
        available = asyncio.run(
            input_kinds_available_to_a_campaign(
                CampaignIntent(rationale="A follow-up card naming compounds only."),
                [],
                PARENT_RUN_ID,
            )
        )

    assert InputKind.LIGAND_STRUCTURES in available


def test_a_names_only_card_still_finds_its_poses_in_the_parent_run():
    gcs = recording_gcs(POSES_SDF_TEXT)
    lineage = [
        LineageRun(run_id=PARENT_RUN_ID, workload="dock", state="succeeded"),
    ]

    with (
        patch("cascade.agents.input_resolution.gcs", gcs),
        patch(
            "cascade.agents.input_resolution.load_triage_decision_for_run",
            triage_promoting("indinavir"),
        ),
        patch(
            "cascade.agents.input_resolution.load_results_prefix_for_run",
            AsyncMock(return_value=f"gs://test-bucket/runs/{PARENT_RUN_ID}/outputs"),
        ),
    ):
        library, reason = asyncio.run(
            posed_complexes_from_an_earlier_run(stage_input_request(), lineage)
        )

    assert reason is None
    assert library is not None
    assert library.source == "parent_run_poses"
    assert library.compound_count == 1


def test_an_unreadable_ligand_reference_falls_through_to_the_parent_library():
    from cascade.agents.input_resolution import resolve_stage_inputs

    request = stage_input_request()
    request.intent.ligand_source = "url"
    request.intent.ligand_reference = PARENT_RUN_ID

    gcs = recording_gcs(SMILES_LIBRARY_TEXT)
    inputs = AsyncMock()
    inputs.download_from_url.side_effect = ValueError("structure URL must be http(s)")
    lineage = [
        LineageRun(
            run_id=PARENT_RUN_ID,
            workload="md_stability",
            state="succeeded",
            ligands_uri=f"gs://test-bucket/runs/{PARENT_RUN_ID}/inputs/ligands.smi",
        )
    ]

    with (
        patch("cascade.agents.input_resolution.gcs", gcs),
        patch("cascade.agents.input_resolution.campaign_inputs", inputs),
        patch("cascade.agents.input_resolution.runs_in_lineage", AsyncMock(return_value=lineage)),
        patch(
            "cascade.agents.input_resolution.load_triage_decision_for_run",
            triage_promoting("indinavir"),
        ),
    ):
        resolved = asyncio.run(resolve_stage_inputs(request))

    assert resolved.unmet == []
    assert resolved.library is not None
    assert resolved.library.source == "parent_run_library"
    assert resolved.library.compound_names == ["indinavir"]
