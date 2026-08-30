from unittest.mock import patch

from cascade.agents.card_text import (
    agent_prose_without_runaway_text,
    triage_card_comment,
)
from cascade.agents.policy import (
    attempts_remaining_after,
    compounds_carried_to_next_stage,
    control_check_for_job,
    enforce_control_gate,
    hold_every_scored_compound_when_triage_judged_none,
    hold_promotions_when_results_do_not_discriminate,
    hold_the_control_compound_rather_than_promoting_it,
    plan_escalated_after_control_failure,
)
from cascade.agents.schemas import (
    CompoundJudgement,
    ControlCheck,
    JobOutcome,
    JobResult,
    JobSubmission,
    LigandLibrary,
    TriagedJobResult,
    TriageVerdict,
    WorkloadParams,
    WorkloadPlan,
)
from cascade.config import Settings


def control_summary(**overrides) -> dict:
    summary = {
        "requested_name": "indinavir",
        "status": "measured",
        "rmsd_to_cocrystal_angstrom": 0.83,
        "lowest_mode_rmsd_angstrom": 0.83,
        "lowest_mode_rank": 1,
        "detail": "9 poses compared",
    }
    summary.update(overrides)
    return summary


def verdict(**overrides) -> TriageVerdict:
    fields = {
        "run_is_trustworthy": True,
        "results_discriminate": True,
        "next_action": "complete",
        "headline": "Looks fine.",
        "compounds": [],
        "rationale": "Nothing to flag.",
    }
    fields.update(overrides)
    return TriageVerdict(**fields)


def triaged(control: ControlCheck, judged: TriageVerdict) -> TriagedJobResult:
    library = LigandLibrary(ligands_uri="gs://b/l.smi", compound_count=4, source="smiles_in_text")
    submission = JobSubmission(
        campaign_id="camp-1",
        card_id="card-1",
        run_id="11111111-1111-5111-8111-111111111111",
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
    outcome = JobOutcome(run_id=submission.run_id, workload="dock", status="succeeded", exit_code=0)
    return TriagedJobResult(
        result=JobResult(submission=submission, outcome=outcome),
        verdict=judged,
        control=control,
    )


def test_a_top_ranked_pose_inside_the_threshold_passes():
    check = control_check_for_job("dock", control_summary())

    assert check.verdict == "passed"
    assert "top-ranked pose RMSD 0.83 A is within" in check.detail


def test_no_pose_anywhere_near_the_crystal_pose_fails():
    check = control_check_for_job(
        "dock",
        control_summary(rmsd_to_cocrystal_angstrom=10.532, lowest_mode_rmsd_angstrom=2.447),
    )

    assert check.verdict == "failed"
    assert "no reported pose came within it" in check.detail
    assert check.top_pose_rmsd_angstrom == 10.532


def test_a_crystal_pose_recovered_below_the_top_rank_is_not_counted_as_a_pass():
    check = control_check_for_job(
        "dock",
        control_summary(
            rmsd_to_cocrystal_angstrom=10.532, lowest_mode_rmsd_angstrom=0.826, lowest_mode_rank=4
        ),
    )

    assert check.verdict == "pose_sampled_not_top_ranked"
    assert "recovered at rank 4" in check.detail
    assert "did not rank it first" in check.detail


def test_a_pose_recovered_off_rank_forces_the_scores_to_be_called_indiscriminate():
    control = control_check_for_job(
        "dock",
        control_summary(
            rmsd_to_cocrystal_angstrom=10.532, lowest_mode_rmsd_angstrom=0.826, lowest_mode_rank=4
        ),
    )

    enforced = enforce_control_gate(
        verdict(results_discriminate=True, next_action="complete"), control, 1
    )

    assert enforced.results_discriminate is False
    assert enforced.run_is_trustworthy is True
    assert enforced.next_action == "complete"


def test_a_scoring_failure_never_asks_for_more_search_effort():
    control = control_check_for_job(
        "dock",
        control_summary(
            rmsd_to_cocrystal_angstrom=10.532, lowest_mode_rmsd_angstrom=0.826, lowest_mode_rank=4
        ),
    )

    enforced = enforce_control_gate(verdict(next_action="rerun_with_more_effort"), control, 1)

    assert enforced.next_action != "rerun_with_more_effort"


def test_a_control_that_never_docked_is_not_measured():
    check = control_check_for_job("dock", control_summary(status="not_in_library"))

    assert check.verdict == "not_measured"


def test_a_campaign_with_no_control_is_not_measured():
    assert control_check_for_job("dock", {}).verdict == "not_measured"


def test_admet_has_no_geometric_control_to_gate_on():
    check = control_check_for_job("admet", {"requested_name": "erlotinib", "verdict": "fail"})

    assert check.verdict == "not_measured"
    assert "no co-crystallized pose" in check.detail


def test_a_failed_control_forces_a_rerun_even_when_the_agent_says_complete():
    control = control_check_for_job(
        "dock",
        control_summary(rmsd_to_cocrystal_angstrom=10.532, lowest_mode_rmsd_angstrom=2.447),
    )

    enforced = enforce_control_gate(verdict(next_action="complete"), control, 1)

    assert enforced.next_action == "rerun_with_more_effort"
    assert enforced.run_is_trustworthy is False


def test_a_failed_control_with_no_attempts_left_escalates_instead_of_rerunning():
    control = control_check_for_job(
        "dock",
        control_summary(rmsd_to_cocrystal_angstrom=10.532, lowest_mode_rmsd_angstrom=2.447),
    )

    enforced = enforce_control_gate(verdict(next_action="complete"), control, 0)

    assert enforced.next_action == "escalate_to_scientist"
    assert enforced.run_is_trustworthy is False


def test_the_agent_cannot_demand_a_rerun_when_the_control_passed():
    control = control_check_for_job("dock", control_summary())

    enforced = enforce_control_gate(verdict(next_action="rerun_with_more_effort"), control, 1)

    assert enforced.next_action == "complete"


def test_an_untrustworthy_run_with_a_passing_control_escalates_rather_than_rerunning():
    control = control_check_for_job("dock", control_summary())

    enforced = enforce_control_gate(
        verdict(next_action="rerun_with_more_effort", run_is_trustworthy=False), control, 1
    )

    assert enforced.next_action == "escalate_to_scientist"


def test_the_gate_leaves_an_escalation_the_agent_asked_for_alone():
    control = control_check_for_job("dock", control_summary())

    enforced = enforce_control_gate(verdict(next_action="escalate_to_scientist"), control, 1)

    assert enforced.next_action == "escalate_to_scientist"


def test_attempts_remaining_runs_out_at_the_configured_ceiling():
    assert attempts_remaining_after(1) == 1
    assert attempts_remaining_after(2) == 0
    assert attempts_remaining_after(3) == 0


def test_a_rerun_raises_conformer_count_rather_than_search_effort():
    plan = WorkloadPlan(
        workload="dock",
        binding_site_method="co_crystal",
        binding_site_confidence="high",
        params=WorkloadParams(exhaustiveness=8, num_modes=9),
        rationale="Eight-fold search is enough for this pocket.",
    )

    escalated = plan_escalated_after_control_failure(plan).params

    assert escalated.conformers_per_ligand == 8
    assert escalated.exhaustiveness == 8
    assert escalated.num_modes == 9


def test_the_card_comment_says_plainly_when_results_do_not_separate():
    comment = triage_card_comment(
        triaged(
            control_check_for_job("dock", control_summary()),
            verdict(
                results_discriminate=False,
                headline="The run is valid but the scores do not rank these compounds.",
                compounds=[
                    CompoundJudgement(
                        compound_id="erlotinib",
                        disposition="hold",
                        reason="within 0.76 kcal/mol of a known non-binder",
                    )
                ],
            ),
        )
    )

    assert "do NOT separate" in comment
    assert "Control `indinavir`: passed" in comment
    assert "0 promoted, 1 held, 0 rejected." in comment
    assert "`erlotinib` hold: within 0.76 kcal/mol of a known non-binder" in comment


def test_the_card_comment_shouts_a_failed_control():
    comment = triage_card_comment(
        triaged(
            control_check_for_job(
                "dock",
                control_summary(rmsd_to_cocrystal_angstrom=10.532, lowest_mode_rmsd_angstrom=2.447),
            ),
            verdict(run_is_trustworthy=False),
        )
    )

    assert "Control `indinavir`: FAILED" in comment
    assert "exceeds the 2.0 A threshold" in comment


def test_the_rmsd_threshold_is_configurable():
    strict = Settings(
        database_url="postgresql+asyncpg://u:p@h/d",
        gcp_project_id="p",
        gcs_bucket="b",
        pubsub_card_events_topic="t",
        trello_api_key="k",
        trello_api_token="t",
        trello_api_secret="s",
        trello_board_id="b",
        trello_callback_url="https://example.test/hook",
        trello_list_todo="a",
        trello_list_in_progress="b",
        trello_list_recommended="c",
        trello_list_needs_attention="d",
        trello_list_done="e",
        control_rmsd_threshold_angstrom=0.5,
    )

    with patch("cascade.agents.policy.settings", strict):
        check = control_check_for_job("dock", control_summary())

    assert check.verdict == "failed"


def score_rows() -> list[dict]:
    return [
        {"rank": 1, "compound_id": "saquinavir", "best_affinity_kcal_per_mol": -10.877},
        {"rank": 2, "compound_id": "indinavir", "best_affinity_kcal_per_mol": -10.415},
        {"rank": 3, "compound_id": "caffeine", "best_affinity_kcal_per_mol": -5.502},
    ]


def test_a_triage_that_judged_no_compound_holds_every_scored_one_instead():
    held = hold_every_scored_compound_when_triage_judged_none(
        verdict(results_discriminate=False, compounds=[]), score_rows()
    )

    assert [judged.compound_id for judged in held.compounds] == [
        "saquinavir",
        "indinavir",
        "caffeine",
    ]
    assert {judged.disposition for judged in held.compounds} == {"hold"}
    assert "-10.877 kcal/mol" in held.compounds[0].reason


def test_compound_judgements_the_triage_agent_did_make_are_left_alone():
    judged = verdict(
        compounds=[
            CompoundJudgement(compound_id="saquinavir", disposition="promote", reason="Best pose.")
        ]
    )

    assert hold_every_scored_compound_when_triage_judged_none(judged, score_rows()) is judged


def test_a_workload_that_reports_no_scores_invents_no_compound_judgements():
    unjudged = verdict(compounds=[])

    assert hold_every_scored_compound_when_triage_judged_none(unjudged, []) is unjudged


RUNAWAY_RATIONALE = (
    "The docking ranking is heavily driven by heavy atom count (correlation -0.75), placing "
    "saquinavir, indinavir, nelfinavir, and ritonavir within the 2.0 kcal/mol scoring error "
    "window. Metrics disagree on the top compound, with ibuprofen leading in ligand efficiency "
    "(0.46) while large inhibitors dominate raw affinity. Because no control ligand was run to "
    "benchmark pose accuracy and the top scores are indistinguishable, scientist escalation is "
    "required to determine next steps before progression " + "compounds are selected " * 60 + "."
)


def test_a_rationale_that_runs_away_mid_sentence_is_cut_at_the_last_sane_sentence():
    kept = agent_prose_without_runaway_text(RUNAWAY_RATIONALE, "fallback headline")

    assert kept.startswith("The docking ranking is heavily driven by heavy atom count")
    assert kept.endswith("dominate raw affinity.")
    assert "compounds are selected compounds are selected" not in kept


def test_a_rationale_of_scratchpad_notes_keeps_only_its_first_sentences():
    kept = agent_prose_without_runaway_text(
        "Control passed at 0.826 A. Top four sit inside scoring error. Wait, actually we can "
        "complete without escalation. Let's make rationale concise and rigorous (2-4 sentences). "
        "The control indinavir passed geometric validation.",
        "fallback headline",
    )

    assert kept == "Control passed at 0.826 A. Top four sit inside scoring error."
    assert "Wait" not in kept
    assert "Let's" not in kept


def test_a_clean_rationale_survives_untouched():
    clean = (
        "The control indinavir reproduced the crystallographic pose at 0.826 A. The top four "
        "compounds sit within the 2.0 kcal/mol scoring error. Ranking is size-driven at -0.75."
    )

    assert agent_prose_without_runaway_text(clean, "fallback headline") == clean


def test_a_rationale_that_is_one_long_runaway_sentence_falls_back_to_the_headline():
    assert (
        agent_prose_without_runaway_text("cleanly and properly " * 80, "fallback headline")
        == "fallback headline"
    )


def test_the_triage_output_schema_never_lets_the_model_omit_its_compound_judgements():
    assert "compounds" in TriageVerdict.model_json_schema()["required"]


def test_triage_reads_the_compound_records_each_workload_actually_writes():
    from cascade.agents.policy import compound_records_from_manifest

    assert compound_records_from_manifest("dock", {"scores": [{"compound_id": "a"}]}) == [
        {"compound_id": "a"}
    ]
    assert compound_records_from_manifest("admet", {"assessments": [{"compound_id": "b"}]}) == [
        {"compound_id": "b"}
    ]
    assert compound_records_from_manifest("cofold", {"predictions": [{"compound_id": "c"}]}) == [
        {"compound_id": "c"}
    ]
    assert compound_records_from_manifest("admet", {"scores": [{"compound_id": "d"}]}) == []


def test_the_submission_comment_shows_what_the_agent_decided():
    from cascade.agents.card_text import planner_decision_lines

    plan = WorkloadPlan(
        workload="dock",
        binding_site_method="co_crystal",
        binding_site_confidence="high",
        params=WorkloadParams(conformers_per_ligand=4, exhaustiveness=8),
        control_compound="indinavir",
        rationale="Standard effort against a co-crystal pocket.",
    )
    comment = "\n".join(planner_decision_lines(plan, "dock"))

    assert "Standard effort against a co-crystal pocket." in comment
    assert "co-crystallized ligand (high confidence)" in comment
    assert "`indinavir`" in comment
    assert "`conformers_per_ligand`: 4" in comment
    assert "`exhaustiveness`: 8" in comment


def test_a_rerun_comment_never_repeats_the_superseded_rationale():
    from cascade.agents.card_text import planner_decision_lines

    plan = WorkloadPlan(
        workload="dock",
        binding_site_method="co_crystal",
        binding_site_confidence="high",
        params=WorkloadParams(conformers_per_ligand=4),
        control_compound="indinavir",
        rationale="Four starting conformers is enough to recover the control pose.",
    )
    rerun = plan_escalated_after_control_failure(plan)
    comment = "\n".join(planner_decision_lines(rerun, "dock"))

    assert "Four starting conformers is enough" not in comment
    assert "`conformers_per_ligand`: 8" in comment
    assert "enforced in code" in comment


def test_settings_from_another_stage_never_reach_the_card():
    from cascade.agents.card_text import planner_decision_lines

    plan = WorkloadPlan(
        workload="admet",
        binding_site_method="none",
        binding_site_confidence="low",
        params=WorkloadParams(exhaustiveness=32, max_compounds=500),
        rationale="Property profiling only.",
    )
    comment = "\n".join(planner_decision_lines(plan, "admet"))

    assert "exhaustiveness" not in comment
    assert "`max_compounds`: 500" in comment
    assert "Binding site" not in comment


def _verdict_with(compounds, results_discriminate=True):
    return TriageVerdict(
        run_is_trustworthy=True,
        results_discriminate=results_discriminate,
        next_action="complete",
        headline="Docking ran.",
        compounds=compounds,
        rationale="Stub rationale.",
    )


def _measured_control(name="indinavir"):
    return ControlCheck(
        verdict="passed",
        compound_name=name,
        status="measured",
        top_pose_rmsd_angstrom=0.682,
        threshold_angstrom=2.0,
        detail="within threshold",
    )


def test_the_control_compound_is_held_rather_than_promoted_as_a_hit():
    verdict = _verdict_with(
        [
            CompoundJudgement(
                compound_id="indinavir", disposition="promote", reason="control reproduced its pose"
            ),
            CompoundJudgement(compound_id="saquinavir", disposition="hold", reason="within error"),
        ]
    )

    held = hold_the_control_compound_rather_than_promoting_it(verdict, _measured_control())
    by_id = {judgement.compound_id: judgement for judgement in held.compounds}

    assert by_id["indinavir"].disposition == "hold"
    assert "control reference" in by_id["indinavir"].reason
    assert by_id["saquinavir"].disposition == "hold"


def test_holding_the_control_stops_it_being_the_only_compound_carried_forward():
    verdict = _verdict_with(
        [
            CompoundJudgement(compound_id="indinavir", disposition="promote", reason="control"),
            CompoundJudgement(compound_id="saquinavir", disposition="hold", reason="within error"),
            CompoundJudgement(compound_id="nelfinavir", disposition="hold", reason="within error"),
        ]
    )

    held = hold_the_control_compound_rather_than_promoting_it(verdict, _measured_control())
    carried, disposition = compounds_carried_to_next_stage(held)

    assert disposition == "hold"
    assert sorted(judgement.compound_id for judgement in carried) == [
        "indinavir",
        "nelfinavir",
        "saquinavir",
    ]


def test_nothing_is_promoted_when_the_run_did_not_separate_the_compounds():
    verdict = _verdict_with(
        [
            CompoundJudgement(compound_id="saquinavir", disposition="promote", reason="-11.3"),
            CompoundJudgement(compound_id="caffeine", disposition="reject", reason="-5.5"),
        ],
        results_discriminate=False,
    )

    downgraded = hold_promotions_when_results_do_not_discriminate(verdict)
    by_id = {judgement.compound_id: judgement for judgement in downgraded.compounds}

    assert by_id["saquinavir"].disposition == "hold"
    assert "did not separate" in by_id["saquinavir"].reason
    assert by_id["caffeine"].disposition == "reject"


def test_a_discriminating_run_keeps_its_promotions():
    verdict = _verdict_with(
        [CompoundJudgement(compound_id="saquinavir", disposition="promote", reason="-11.3")]
    )

    assert hold_promotions_when_results_do_not_discriminate(verdict) == verdict


def test_a_run_without_a_control_is_left_alone():
    verdict = _verdict_with(
        [CompoundJudgement(compound_id="saquinavir", disposition="promote", reason="-11.3")]
    )
    control = ControlCheck(verdict="not_measured", detail="no control requested")

    assert hold_the_control_compound_rather_than_promoting_it(verdict, control) == verdict
