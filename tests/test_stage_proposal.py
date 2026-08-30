from cascade.agents.capabilities import InputKind
from cascade.agents.card_text import (
    blocked_stages_note,
    estimated_stage_cost_line,
    followup_card_comment,
    library_lines_for_compounds,
    parent_run_id_in_card_description,
    proposed_card_description,
    proposed_card_title,
    unproposed_stage_note,
)
from cascade.agents.compound_library import (
    sdf_subset_for_compounds,
    smiles_library_lines_from_text,
)
from cascade.agents.policy import (
    blocked_stages_from_readiness,
    compounds_carried_to_next_stage,
    readiness_for_every_stage,
    runnable_stages_from_readiness,
)
from cascade.agents.schemas import (
    BlockedStage,
    CompletedStage,
    CompoundJudgement,
    ControlCheck,
    JobOutcome,
    JobResult,
    JobSubmission,
    LigandLibrary,
    ProposalDecision,
    ProposalRequest,
    ProposedFollowup,
    StageProposal,
    TriagedJobResult,
    TriageVerdict,
    WorkloadParams,
    WorkloadPlan,
)

AFTER_A_DOCKING_RUN = frozenset(
    {InputKind.PROTEIN_STRUCTURE, InputKind.LIGAND_STRUCTURES, InputKind.POSED_COMPLEXES}
)
COMPOUNDS_ONLY = frozenset({InputKind.LIGAND_STRUCTURES})

PARENT_RUN_ID = "11111111-1111-5111-8111-111111111111"
INDINAVIR_SMILES = (
    "CC(C)(C)NC(=O)[C@@H]1CN(Cc2cccnc2)CCN1C[C@@H](O)C[C@@H](Cc1ccccc1)C(=O)N[C@H]1c2ccccc2C[C@H]1O"
)
LIBRARY_TEXT = (
    f"{INDINAVIR_SMILES}\tindinavir\n"
    "CC(C)Cc1ccc(cc1)C(C)C(=O)O\tibuprofen\n"
    "CC(=O)Oc1ccccc1C(=O)O\taspirin\n"
    "Cn1cnc2c1c(=O)n(C)c(=O)n2C\tcaffeine\n"
)


def judgement(compound_id: str, disposition: str) -> CompoundJudgement:
    return CompoundJudgement(
        compound_id=compound_id, disposition=disposition, reason="a specific number"
    )


def verdict(**overrides) -> TriageVerdict:
    fields = {
        "run_is_trustworthy": True,
        "results_discriminate": True,
        "next_action": "complete",
        "headline": "The run is valid.",
        "compounds": [],
        "rationale": "Nothing to flag.",
    }
    fields.update(overrides)
    return TriageVerdict(**fields)


def completed_stage(judged: TriageVerdict) -> CompletedStage:
    library = LigandLibrary(ligands_uri="gs://b/l.smi", compound_count=4, source="smiles_in_text")
    submission = JobSubmission(
        campaign_id="camp-1",
        card_id="card-1",
        run_id=PARENT_RUN_ID,
        workload="dock",
        attempt=1,
        execution_name="cascade-dock-abc",
        spec_uri="gs://b/spec.json",
        output_uri="gs://b/out",
        library=library,
        plan=WorkloadPlan(
            workload="dock",
            binding_site_method="co_crystal",
            binding_site_confidence="high",
            params=WorkloadParams(conformers_per_ligand=4),
            control_compound="indinavir",
            rationale="Standard effort against a co-crystal pocket.",
        ),
    )
    outcome = JobOutcome(run_id=PARENT_RUN_ID, workload="dock", status="succeeded", exit_code=0)
    return CompletedStage(
        triaged=TriagedJobResult(
            result=JobResult(submission=submission, outcome=outcome),
            verdict=judged,
            control=ControlCheck(verdict="passed", detail="within threshold"),
        ),
        target_name="HIV protease",
        target_source="rcsb",
        target_reference="1HSG",
    )


def proposal_request(**overrides) -> ProposalRequest:
    fields = {
        "completed_stage": "dock",
        "target_name": "HIV protease",
        "results_discriminate": True,
        "triage_headline": "Four compounds separated cleanly.",
        "carried_compounds": [judgement("indinavir", "promote"), judgement("aspirin", "promote")],
        "carried_disposition": "promote",
        "runnable_next_stages": ["admet"],
        "blocked_next_stages": [],
    }
    fields.update(overrides)
    return ProposalRequest(**fields)


def decision(request: ProposalRequest, reason: str) -> ProposalDecision:
    return ProposalDecision(
        completed=completed_stage(verdict()),
        request=request,
        proposal=StageProposal(
            next_stage="admet",
            card_title="Safety screen on 2 compounds",
            reason=reason,
            rationale="Docking separated these two, so the cheap liability screen is next.",
        ),
    )


def test_a_run_that_promoted_compounds_carries_only_those_forward():
    carried, disposition = compounds_carried_to_next_stage(
        verdict(
            compounds=[
                judgement("indinavir", "promote"),
                judgement("aspirin", "hold"),
                judgement("caffeine", "reject"),
            ]
        )
    )

    assert [judged.compound_id for judged in carried] == ["indinavir"]
    assert disposition == "promote"


def test_a_run_that_promoted_nothing_carries_the_held_compounds_instead():
    carried, disposition = compounds_carried_to_next_stage(
        verdict(
            results_discriminate=False,
            compounds=[
                judgement("indinavir", "hold"),
                judgement("aspirin", "hold"),
                judgement("caffeine", "reject"),
            ],
        )
    )

    assert [judged.compound_id for judged in carried] == ["indinavir", "aspirin"]
    assert disposition == "hold"


def test_rejected_compounds_are_never_carried_forward():
    carried, _ = compounds_carried_to_next_stage(
        verdict(compounds=[judgement("caffeine", "reject"), judgement("aspirin", "reject")])
    )

    assert carried == []


def test_the_stages_runnable_after_docking_are_the_safety_screen_and_pose_stability():
    readiness = readiness_for_every_stage(AFTER_A_DOCKING_RUN, 4, {"dock": PARENT_RUN_ID})

    assert runnable_stages_from_readiness(readiness, "dock") == ["admet", "md_stability"]


def test_pose_stability_is_not_offered_when_no_earlier_run_produced_poses():
    readiness = readiness_for_every_stage(COMPOUNDS_ONLY, 4, {"admet": PARENT_RUN_ID})

    assert runnable_stages_from_readiness(readiness, "admet") == []
    blockers = {
        stage.workload: stage.reason for stage in blocked_stages_from_readiness(readiness, None)
    }
    assert "3D coordinates" in blockers["md_stability"]


def test_a_stage_without_a_container_is_reported_as_blocked_not_offered():
    from cascade.agents.policy import unsupported_stage_rationale

    reason = unsupported_stage_rationale("synthesis", 4)

    assert reason is not None
    assert "no workload container" in reason


def test_a_gpu_stage_is_reported_as_blocked_with_the_gpu_reason():
    readiness = readiness_for_every_stage(AFTER_A_DOCKING_RUN, 4, {})
    blockers = {
        stage.workload: stage.reason for stage in blocked_stages_from_readiness(readiness, None)
    }

    assert "needs a GPU executor" in blockers["cofold"]


def test_md_stability_runs_on_a_gpu_cloud_run_job_rather_than_cloud_batch():
    from cascade.agents.policy import executor_for_workload, unsupported_stage_rationale

    assert executor_for_workload("md_stability", 4) == "cloud_run_gpu_job"
    assert unsupported_stage_rationale("md_stability", 4) is None


def test_a_gpu_stage_over_the_cloud_run_ceiling_still_falls_back_to_cloud_batch():
    from cascade.agents.policy import executor_for_workload, unsupported_stage_rationale

    assert executor_for_workload("md_stability", 500) == "cloud_batch"
    assert unsupported_stage_rationale("md_stability", 500) is not None


def test_the_completed_stage_is_never_offered_as_its_own_follow_up():
    readiness = readiness_for_every_stage(AFTER_A_DOCKING_RUN, 4, {})

    assert "admet" not in runnable_stages_from_readiness(readiness, "admet")


def test_only_the_carried_compounds_are_pulled_out_of_the_library():
    lines = library_lines_for_compounds(LIBRARY_TEXT, ["indinavir", "aspirin"])

    assert lines == [
        f"`{INDINAVIR_SMILES}` indinavir",
        "`CC(=O)Oc1ccccc1C(=O)O` aspirin",
    ]


def test_a_carried_compound_missing_from_the_library_is_dropped_rather_than_guessed():
    lines = library_lines_for_compounds(LIBRARY_TEXT, ["indinavir", "not_in_the_library"])

    assert lines == [f"`{INDINAVIR_SMILES}` indinavir"]


def test_a_library_with_no_readable_smiles_yields_no_lines():
    assert library_lines_for_compounds("", ["indinavir"]) == []


def test_the_proposed_card_description_round_trips_through_the_ligand_parser():
    library_lines = library_lines_for_compounds(LIBRARY_TEXT, ["indinavir", "aspirin"])
    description = proposed_card_description(
        decision(proposal_request(), "No further separation is possible on the rest."),
        library_lines,
        PARENT_RUN_ID,
    )

    parsed, _ = smiles_library_lines_from_text(description)

    assert parsed == [
        f"{INDINAVIR_SMILES}\tindinavir",
        "CC(=O)Oc1ccccc1C(=O)O\taspirin",
    ]


def test_a_multi_line_reason_cannot_leak_a_word_into_the_compound_list():
    library_lines = library_lines_for_compounds(LIBRARY_TEXT, ["indinavir"])
    description = proposed_card_description(
        decision(proposal_request(), "Docking ranked them.\nNo compound was separated."),
        library_lines,
        PARENT_RUN_ID,
    )

    parsed, _ = smiles_library_lines_from_text(description)

    assert parsed == [f"{INDINAVIR_SMILES}\tindinavir"]


def test_every_smiles_in_the_description_is_backticked_against_trello_markdown():
    library_lines = library_lines_for_compounds(LIBRARY_TEXT, ["indinavir"])
    description = proposed_card_description(
        decision(proposal_request(), "Worth a liability screen."), library_lines, PARENT_RUN_ID
    )

    assert f"`{INDINAVIR_SMILES}`" in description


def test_the_description_names_the_target_so_a_fresh_campaign_can_resolve_it():
    description = proposed_card_description(
        decision(proposal_request(), "Worth a liability screen."),
        library_lines_for_compounds(LIBRARY_TEXT, ["indinavir"]),
        PARENT_RUN_ID,
    )

    assert "**Target.** HIV protease (rcsb 1HSG)" in description


def test_a_held_carry_says_the_previous_stage_did_not_separate_them():
    description = proposed_card_description(
        decision(
            proposal_request(carried_disposition="hold", results_discriminate=False),
            "The scores did not rank these.",
        ),
        library_lines_for_compounds(LIBRARY_TEXT, ["indinavir", "aspirin"]),
        PARENT_RUN_ID,
    )

    assert "did not separate" in description


def test_the_card_description_names_the_parent_run():
    description = proposed_card_description(
        decision(proposal_request(), "Worth a liability screen."),
        library_lines_for_compounds(LIBRARY_TEXT, ["indinavir"]),
        PARENT_RUN_ID,
    )

    assert parent_run_id_in_card_description(description) == PARENT_RUN_ID


def test_a_description_written_by_a_scientist_names_no_parent_run():
    assert parent_run_id_in_card_description("Screen these against 1HSG.") is None


def test_the_cost_line_names_the_executor_that_would_actually_run_the_stage():
    assert "Cloud Run Job" in estimated_stage_cost_line("admet", 4)
    assert "Cloud Batch" in estimated_stage_cost_line("cofold", 4)
    assert "NVIDIA L4 GPU" in estimated_stage_cost_line("md_stability", 4)


def test_a_library_over_the_cloud_run_ceiling_is_priced_as_cloud_batch():
    assert "Cloud Batch" in estimated_stage_cost_line("admet", 500)


def test_the_comment_for_a_created_card_names_the_stage_and_the_compounds():
    comment = followup_card_comment(
        ProposedFollowup(
            next_stage="admet",
            created_card_id="card-2",
            created_card_url="https://trello.com/c/card-2",
            carried_compounds=["indinavir", "aspirin"],
            note="A liability screen is cheap and orthogonal.",
        )
    )

    assert "Proposed next step: admet" in comment
    assert "`indinavir`, `aspirin`" in comment


def test_the_comment_says_plainly_when_nothing_was_proposed():
    comment = followup_card_comment(ProposedFollowup(note="Nothing survived this stage."))

    assert comment.startswith("**No follow-up proposed.**")
    assert "Nothing survived this stage." in comment


def test_the_comment_names_the_stages_that_could_not_be_proposed():
    blocked = blocked_stages_from_readiness(
        readiness_for_every_stage(AFTER_A_DOCKING_RUN, 2, {}), "admet"
    )
    comment = followup_card_comment(
        ProposedFollowup(
            next_stage="admet",
            created_card_id="card-2",
            carried_compounds=["indinavir"],
            note="A liability screen is cheap.",
            blocked_note=blocked_stages_note(blocked),
        )
    )

    assert "`md_stability`" not in comment
    assert "`cofold` - cofold needs a GPU executor" in comment


def test_no_blocked_note_is_written_when_every_stage_is_runnable():
    assert blocked_stages_note([]) == ""


def test_a_blocked_note_names_each_stage_and_its_reason():
    note = blocked_stages_note(
        [BlockedStage(workload="md_stability", reason="it has no workload container yet.")]
    )

    assert "`md_stability` - it has no workload container yet." in note


def test_an_untrustworthy_run_says_it_will_not_build_on_top_of_itself():
    note = unproposed_stage_note(proposal_request(run_is_trustworthy=False))

    assert "not judged trustworthy" in note


def test_a_stage_that_produced_no_survivors_says_there_is_nothing_to_carry():
    note = unproposed_stage_note(proposal_request(carried_compounds=[]))

    assert "nothing to carry forward" in note


def test_survivors_with_no_runnable_stage_are_named_rather_than_called_nothing():
    note = unproposed_stage_note(proposal_request(runnable_next_stages=[]))

    assert "2 compound(s) survived" in note
    assert "no stage it can run on them today" in note


def test_a_stage_already_run_is_still_offered_with_its_earlier_run_named():
    previous_dock_run = "4a311b89-c33f-53e6-b5ac-54583b90288e"
    readiness = {
        item.stage: item
        for item in readiness_for_every_stage(AFTER_A_DOCKING_RUN, 2, {"dock": previous_dock_run})
    }

    assert readiness["dock"].inputs_resolve
    assert readiness["dock"].already_run_as == previous_dock_run
    assert readiness["admet"].already_run_as is None
    assert "dock" in runnable_stages_from_readiness(list(readiness.values()), "admet")


def test_the_lineage_walk_stops_on_a_cycle():
    import asyncio
    from unittest.mock import patch

    from cascade.agents.persistence import runs_in_lineage

    class FakeRun:
        def __init__(self, run_id, workload, parent):
            self.id, self.workload, self.parent_run_id = run_id, workload, parent
            self.state, self.spec = "succeeded", {}

    import uuid as uuid_module

    a, b = uuid_module.uuid4(), uuid_module.uuid4()
    rows = {a: FakeRun(a, "dock", b), b: FakeRun(b, "admet", a)}

    class FakeSession:
        async def get(self, _model, key):
            return rows.get(key)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    with patch("cascade.agents.persistence.async_session", lambda: FakeSession()):
        walked = asyncio.run(runs_in_lineage(str(a)))

    assert [(run.run_id, run.workload) for run in walked] == [
        (str(a), "dock"),
        (str(b), "admet"),
    ]


def docked_pose_sdf_record(name: str, rank: int) -> str:
    return (
        f"{name}\n  fake\n  0  0\nM  END\n"
        f">  <compound_id>  ({rank}) \n{name}\n\n"
        f">  <affinity_rank>  ({rank}) \n{rank}\n\n"
        f">  <best_affinity_kcal_per_mol>  ({rank}) \n-{rank}.5\n\n"
        "$$$$\n"
    )


def test_only_the_carried_compounds_survive_the_docked_pose_filter():
    sdf = "".join(
        docked_pose_sdf_record(name, rank)
        for rank, name in enumerate(("indinavir", "caffeine", "aspirin"), start=1)
    )

    filtered, kept_names = sdf_subset_for_compounds(sdf, ["indinavir", "aspirin"])

    assert kept_names == ["indinavir", "aspirin"]
    assert "caffeine" not in filtered
    assert filtered.count("$$$$") == 2


def test_the_docked_pose_filter_keeps_every_property_block_readable():
    sdf = "".join(
        docked_pose_sdf_record(name, rank)
        for rank, name in enumerate(("indinavir", "caffeine", "aspirin"), start=1)
    )

    filtered, _ = sdf_subset_for_compounds(sdf, ["indinavir", "aspirin"])

    assert filtered.endswith("$$$$\n")
    assert "-1.5\n\n$$$$" in filtered
    assert "-3.5\n\n$$$$" in filtered
    for record in filtered.split("$$$$\n")[:-1]:
        assert record.count(">  <best_affinity_kcal_per_mol>") == 1


def test_a_proposed_card_title_is_prefixed_with_the_pdb_id_of_an_rcsb_target():
    title = proposed_card_title(completed_stage(verdict()), "Safety screen on 2 compounds")

    assert title == "1HSG · Safety screen on 2 compounds"


def test_a_proposed_card_title_falls_back_to_the_target_name_when_it_is_not_from_rcsb():
    completed = completed_stage(verdict()).model_copy(
        update={"target_source": "card_attachment", "target_reference": "receptor.pdb"}
    )

    title = proposed_card_title(completed, "Safety screen on 2 compounds")

    assert title == "HIV protease · Safety screen on 2 compounds"


def test_a_proposed_card_title_is_still_labelled_when_the_campaign_names_no_target():
    completed = completed_stage(verdict()).model_copy(
        update={"target_name": None, "target_source": None, "target_reference": None}
    )

    title = proposed_card_title(completed, "Safety screen on 2 compounds")

    assert title == "No target · Safety screen on 2 compounds"


def test_a_proposed_card_title_is_not_prefixed_twice_when_the_agent_already_named_the_target():
    title = proposed_card_title(completed_stage(verdict()), "1HSG safety screen on 2 compounds")

    assert title == "1HSG safety screen on 2 compounds"
