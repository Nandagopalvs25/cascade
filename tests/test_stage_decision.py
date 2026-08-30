import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from cascade.agents.campaign import validated_stage_decision
from cascade.agents.capabilities import InputKind
from cascade.agents.decision_tools import (
    MAXIMUM_COMPOUND_ROWS_RETURNED,
    compound_measurement_without_bulk_series,
    read_previous_stage_compound_measurements,
    read_previous_stage_conclusion,
    read_previous_stage_request_card,
)
from cascade.agents.policy import readiness_for_every_stage, stage_choice_rejection_reason
from cascade.agents.schemas import (
    CampaignIntent,
    CompoundJudgement,
    StageDecision,
    StageDecisionRequest,
)

KNOWN_RUN_ID = "4a311b89-c33f-53e6-b5ac-54583b90288e"

AFTER_A_DOCKING_RUN = frozenset(
    {InputKind.PROTEIN_STRUCTURE, InputKind.LIGAND_STRUCTURES, InputKind.POSED_COMPLEXES}
)
COMPOUNDS_ONLY = frozenset({InputKind.LIGAND_STRUCTURES})


def intent() -> CampaignIntent:
    return CampaignIntent(requested_stages=["dock"], rationale="test")


def decision_request(**overrides) -> StageDecisionRequest:
    fields = {
        "decision_point": "next_stage",
        "card_title": "Screen these compounds",
        "card_description": "seven compounds against HIV protease",
        "intent": intent(),
        "compound_count": 2,
        "stage_readiness": readiness_for_every_stage(AFTER_A_DOCKING_RUN, 2, {}),
        "completed_stage": "dock",
        "run_is_trustworthy": True,
        "results_discriminate": True,
        "carried_compounds": [
            CompoundJudgement(compound_id="indinavir", disposition="promote", reason="-11.2")
        ],
    }
    fields.update(overrides)
    return StageDecisionRequest(**fields)


def decision(**overrides) -> StageDecision:
    fields = {
        "chosen_stage": "admet",
        "question_it_answers": "Whether these carry a known liability.",
        "card_title": "Safety screen",
        "reason": "Cheap and orthogonal.",
        "rationale": "Docking separated them, so the liability screen is next.",
    }
    fields.update(overrides)
    return StageDecision(**fields)


def test_a_runnable_stage_is_accepted():
    assert stage_choice_rejection_reason(decision(), decision_request()) is None


def test_choosing_nothing_is_never_rejected():
    assert stage_choice_rejection_reason(decision(chosen_stage=None), decision_request()) is None


def test_a_stage_whose_inputs_do_not_resolve_is_rejected_with_the_reason():
    request = decision_request(
        stage_readiness=readiness_for_every_stage(COMPOUNDS_ONLY, 2, {}),
    )

    rejection = stage_choice_rejection_reason(decision(chosen_stage="md_stability"), request)

    assert rejection is not None
    assert "3D coordinates" in rejection


def test_nothing_may_be_built_on_a_run_triage_did_not_trust():
    rejection = stage_choice_rejection_reason(
        decision(), decision_request(run_is_trustworthy=False)
    )

    assert rejection is not None
    assert "not judged trustworthy" in rejection


def test_a_stage_with_no_surviving_compounds_is_rejected():
    rejection = stage_choice_rejection_reason(decision(), decision_request(carried_compounds=[]))

    assert rejection is not None
    assert "nothing to run on" in rejection


class RecordingContext:
    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.run_ids: list[str] = []
        self.requests: list[StageDecisionRequest] = []

    async def run_node(self, _agent, node_input, run_id):
        self.run_ids.append(run_id)
        self.requests.append(node_input)
        return self._decisions.pop(0).model_dump()


def test_each_retry_uses_its_own_run_id_so_the_answer_is_not_replayed():
    ctx = RecordingContext([decision(chosen_stage="md_stability"), decision()])
    request = decision_request(stage_readiness=readiness_for_every_stage(COMPOUNDS_ONLY, 2, {}))

    asyncio.run(validated_stage_decision(ctx, request, run_id_prefix="next-stage-dock-1"))

    assert ctx.run_ids == ["next-stage-dock-1-choice1", "next-stage-dock-1-choice2"]


def test_a_rejected_choice_is_carried_back_to_the_agent_with_its_reason():
    ctx = RecordingContext([decision(chosen_stage="md_stability"), decision()])
    request = decision_request(stage_readiness=readiness_for_every_stage(COMPOUNDS_ONLY, 2, {}))

    asyncio.run(validated_stage_decision(ctx, request, run_id_prefix="next-stage-dock-1"))

    second_attempt = ctx.requests[1]
    assert [rejected.stage for rejected in second_attempt.rejected_choices] == ["md_stability"]
    assert "3D coordinates" in second_attempt.rejected_choices[0].reason
    assert ctx.requests[0].choices_remaining == 1
    assert second_attempt.choices_remaining == 0


def test_running_out_of_choices_ends_with_no_stage_rather_than_an_unrunnable_one():
    ctx = RecordingContext(
        [decision(chosen_stage="md_stability"), decision(chosen_stage="md_stability")]
    )
    request = decision_request(stage_readiness=readiness_for_every_stage(COMPOUNDS_ONLY, 2, {}))

    chosen, _ = asyncio.run(
        validated_stage_decision(ctx, request, run_id_prefix="next-stage-dock-1")
    )

    assert chosen.chosen_stage is None


def test_bulk_series_are_stripped_so_a_tool_result_stays_small():
    kept = compound_measurement_without_bulk_series(
        {
            "compound_id": "indinavir",
            "best_affinity_kcal_per_mol": -11.2,
            "mode_affinities": [-11.2, -10.9, -10.4, -9.8, -9.1, -8.7, -8.2],
            "flags": ["clean"],
            "nested": {"frames": [1, 2, 3]},
        }
    )

    assert kept == {
        "compound_id": "indinavir",
        "best_affinity_kcal_per_mol": -11.2,
        "flags": ["clean"],
    }


@pytest.mark.parametrize(
    "tool",
    (read_previous_stage_conclusion, read_previous_stage_compound_measurements),
)
def test_a_run_id_that_is_not_in_the_campaign_returns_a_readable_refusal(tool, db_sessionmaker):
    with patch("cascade.agents.persistence.async_session", db_sessionmaker):
        result = asyncio.run(tool("not-a-uuid"))

    assert result["found"] is False
    assert "campaign_history" in result["detail"]


def test_the_conclusion_tool_reads_the_verdict_recorded_for_a_run(db_sessionmaker):
    recorded = {
        "rationale": "Two compounds separated.",
        "inputs": {"workload": "dock"},
        "output": {
            "run_is_trustworthy": True,
            "results_discriminate": True,
            "headline": "Two separated.",
            "compounds": [
                {"compound_id": "indinavir", "disposition": "promote", "reason": "-11.2"}
            ],
        },
    }
    with patch(
        "cascade.agents.decision_tools.load_triage_decision_for_run",
        AsyncMock(return_value=recorded),
    ):
        result = asyncio.run(read_previous_stage_conclusion(KNOWN_RUN_ID))

    assert result["found"] is True
    assert result["headline"] == "Two separated."
    assert result["compounds"][0]["disposition"] == "promote"


def test_the_measurements_tool_reports_how_many_compounds_it_left_out():
    records = [
        {"compound_id": f"compound_{index}", "best_affinity_kcal_per_mol": -index}
        for index in range(MAXIMUM_COMPOUND_ROWS_RETURNED + 6)
    ]
    with patch(
        "cascade.agents.decision_tools.load_triage_decision_for_run",
        AsyncMock(return_value={"rationale": "", "inputs": {"scores": records}, "output": {}}),
    ):
        result = asyncio.run(read_previous_stage_compound_measurements(KNOWN_RUN_ID))

    assert result["compounds_returned"] == MAXIMUM_COMPOUND_ROWS_RETURNED
    assert result["compounds_omitted"] == 6
    assert len(result["compounds"]) == MAXIMUM_COMPOUND_ROWS_RETURNED


def test_a_trello_failure_is_reported_to_the_agent_rather_than_raised():
    with (
        patch(
            "cascade.agents.decision_tools.load_run_workload_and_card_id",
            AsyncMock(return_value={"workload": "dock", "card_id": "card-1"}),
        ),
        patch(
            "cascade.agents.decision_tools.trello.get_card",
            AsyncMock(side_effect=httpx.ConnectError("boom")),
        ),
    ):
        result = asyncio.run(read_previous_stage_request_card(KNOWN_RUN_ID))

    assert result["found"] is False
    assert "card-1" in result["detail"]
