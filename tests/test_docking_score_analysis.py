import sys
from pathlib import Path

import pytest

from cascade.agents.card_text import (
    docking_headline,
    docking_score_reliability_line,
)
from cascade.agents.policy import allowed_job_params_for_workload

DOCK_CONTAINER_DIRECTORY = Path(__file__).resolve().parents[1] / "workloads" / "dock"
sys.path.insert(0, str(DOCK_CONTAINER_DIRECTORY))

from job_spec import DockingParams  # noqa: E402
from score_analysis import (  # noqa: E402
    ScoredCompound,
    analyze_docking_scores,
    ligand_efficiency,
    size_independent_ligand_efficiency,
    spearman_rank_correlation,
)


@pytest.fixture
def egfr_panel() -> list[ScoredCompound]:
    return [
        ScoredCompound("lapatinib", -9.096, 40),
        ScoredCompound("vandetanib", -8.454, 30),
        ScoredCompound("gefitinib", -8.036, 31),
        ScoredCompound("quinazoline-fragment", -7.812, 23),
        ScoredCompound("erlotinib", -7.267, 29),
        ScoredCompound("ibuprofen", -6.505, 15),
    ]


def test_ligand_efficiency_is_binding_energy_per_heavy_atom():
    assert ligand_efficiency(-7.267, 29) == pytest.approx(0.2506, abs=1e-4)


def test_size_independent_ligand_efficiency_uses_the_published_exponent():
    assert size_independent_ligand_efficiency(-7.267, 29) == pytest.approx(
        7.267 / 29**0.3, abs=1e-4
    )


def test_efficiency_metrics_are_undefined_without_heavy_atoms():
    assert ligand_efficiency(-7.267, 0) is None
    assert size_independent_ligand_efficiency(-7.267, 0) is None


def test_spearman_correlation_ranks_perfectly_anticorrelated_series():
    assert spearman_rank_correlation([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == -1.0


def test_spearman_correlation_averages_tied_ranks():
    assert spearman_rank_correlation([1.0, 1.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(
        0.866, abs=1e-3
    )


def test_spearman_correlation_needs_three_compounds():
    assert spearman_rank_correlation([1.0, 2.0], [2.0, 1.0]) is None


def test_spearman_correlation_is_undefined_when_every_compound_is_the_same_size():
    assert spearman_rank_correlation([20.0, 20.0, 20.0], [-9.0, -8.0, -7.0]) is None


def test_empty_result_set_has_no_analysis():
    assert analyze_docking_scores([]) is None


def test_the_live_egfr_ranking_is_reported_as_size_driven(egfr_panel):
    analysis = analyze_docking_scores(egfr_panel)

    assert analysis.affinity_heavy_atom_correlation == pytest.approx(-0.886, abs=1e-3)
    assert analysis.ranking_is_size_driven is True


def test_the_live_egfr_ranking_does_not_separate_its_top_compounds(egfr_panel):
    analysis = analyze_docking_scores(egfr_panel)

    assert analysis.ranking_separates_best_compound is False
    assert "erlotinib" in analysis.compounds_indistinguishable_from_best
    assert "quinazoline-fragment" in analysis.compounds_indistinguishable_from_best


def test_ligand_efficiency_does_not_rescue_the_egfr_ranking(egfr_panel):
    analysis = analyze_docking_scores(egfr_panel)

    assert analysis.best_by_affinity == "lapatinib"
    assert analysis.best_by_ligand_efficiency == "ibuprofen"
    assert analysis.best_by_size_independent_ligand_efficiency == "quinazoline-fragment"
    assert analysis.metrics_agree_on_best_compound is False


def test_the_known_potent_control_is_outscored_by_four_compounds(egfr_panel):
    analysis = analyze_docking_scores(egfr_panel, control_compound_name="Erlotinib")

    assert analysis.control_affinity_rank == 5
    assert analysis.compounds_scoring_better_than_control == [
        "lapatinib",
        "vandetanib",
        "gefitinib",
        "quinazoline-fragment",
    ]


def test_a_control_that_never_docked_leaves_the_calibration_fields_empty(egfr_panel):
    analysis = analyze_docking_scores(egfr_panel, control_compound_name="indinavir")

    assert analysis.control_affinity_rank is None
    assert analysis.compounds_scoring_better_than_control == []


def test_a_genuinely_separated_set_is_not_reported_as_ambiguous():
    analysis = analyze_docking_scores(
        [
            ScoredCompound("strong-binder", -11.5, 28),
            ScoredCompound("weak-one", -6.1, 27),
            ScoredCompound("weak-two", -5.4, 29),
        ]
    )

    assert analysis.ranking_separates_best_compound is True
    assert analysis.compounds_indistinguishable_from_best == ["strong-binder"]
    assert analysis.ranking_is_size_driven is False


def test_widening_the_scoring_error_widens_the_indistinguishable_set(egfr_panel):
    tight = analyze_docking_scores(egfr_panel, scoring_function_error_kcal_per_mol=0.5)
    wide = analyze_docking_scores(egfr_panel, scoring_function_error_kcal_per_mol=3.0)

    assert tight.compounds_indistinguishable_from_best == ["lapatinib"]
    assert tight.ranking_separates_best_compound is True
    assert len(wide.compounds_indistinguishable_from_best) == len(egfr_panel)


def test_docking_params_expose_a_configurable_scoring_function_error():
    assert DockingParams().scoring_function_error_kcal_per_mol == 2.0
    assert (
        DockingParams.model_validate(
            {"scoring_function_error_kcal_per_mol": 1.5}
        ).scoring_function_error_kcal_per_mol
        == 1.5
    )


def test_the_planner_may_set_the_scoring_function_error():
    assert "scoring_function_error_kcal_per_mol" in allowed_job_params_for_workload("dock")


def test_the_card_warns_when_the_ranking_cannot_name_a_best_compound():
    line = docking_score_reliability_line(
        {
            "scoring_function_error_kcal_per_mol": 2.0,
            "ranking_separates_best_compound": False,
            "compounds_indistinguishable_from_best_count": 5,
            "ranking_is_size_driven": True,
            "affinity_heavy_atom_correlation": -0.886,
            "metrics_agree_on_best_compound": False,
            "best_by_ligand_efficiency": "ibuprofen",
        }
    )

    assert "top 5 compounds are within the 2.0 kcal/mol scoring error" in line
    assert "affinity tracks molecule size" in line
    assert "ligand efficiency favours ibuprofen" in line


def test_the_card_stays_quiet_when_the_ranking_is_trustworthy():
    assert (
        docking_score_reliability_line(
            {
                "scoring_function_error_kcal_per_mol": 2.0,
                "ranking_separates_best_compound": True,
                "compounds_indistinguishable_from_best_count": 1,
                "ranking_is_size_driven": False,
                "metrics_agree_on_best_compound": True,
            }
        )
        == ""
    )


def test_the_headline_carries_the_reliability_warning_into_the_card_comment():
    headline = docking_headline(
        {
            "ligands_docked": 9,
            "ligands_failed": 0,
            "best_compound_id": "lapatinib",
            "best_affinity_kcal_per_mol": -9.096,
            "score_analysis": {
                "scoring_function_error_kcal_per_mol": 2.0,
                "ranking_separates_best_compound": False,
                "compounds_indistinguishable_from_best_count": 5,
                "ranking_is_size_driven": True,
                "affinity_heavy_atom_correlation": -0.886,
                "metrics_agree_on_best_compound": False,
                "best_by_ligand_efficiency": "ibuprofen",
            },
        }
    )

    assert "9 compounds docked" in headline
    assert "Score reliability:" in headline
    assert "best was lapatinib" not in headline
    assert "highest score lapatinib at -9.096 kcal/mol" in headline


def test_an_older_run_without_score_analysis_still_produces_a_headline():
    headline = docking_headline(
        {
            "ligands_docked": 3,
            "ligands_failed": 0,
            "best_compound_id": "aspirin",
            "best_affinity_kcal_per_mol": -6.1,
        }
    )

    assert headline.endswith("-6.1 kcal/mol")
