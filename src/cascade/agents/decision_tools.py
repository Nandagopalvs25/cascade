import httpx

from cascade.agents.persistence import (
    load_run_workload_and_card_id,
    load_triage_decision_for_run,
)
from cascade.clients import trello

MAXIMUM_COMPOUND_ROWS_RETURNED = 25
MAXIMUM_LIST_VALUES_KEPT = 6
MAXIMUM_CARD_TEXT_CHARACTERS = 4000

RUN_NOT_IN_CAMPAIGN_DETAIL = (
    "No run with that id has a recorded result. Use a run_id exactly as it appears in "
    "campaign_history; ids from anywhere else resolve to nothing."
)


def compound_measurement_without_bulk_series(record: dict) -> dict:
    kept: dict = {}
    for key, value in record.items():
        if isinstance(value, dict):
            continue
        if isinstance(value, list) and (
            len(value) > MAXIMUM_LIST_VALUES_KEPT
            or not all(isinstance(item, str) for item in value)
        ):
            continue
        kept[key] = value
    return kept


async def read_previous_stage_conclusion(run_id: str) -> dict:
    """Open what an earlier stage concluded, compound by compound, with triage's reasons.

    Args:
        run_id: A run id copied verbatim from campaign_history. Ids from anywhere
            else resolve to nothing.

    Returns:
        The triage verdict for that run: whether it was trustworthy, whether its
        results separated the compounds, its headline, its rationale, and one
        judgement per compound.
    """
    recorded = await load_triage_decision_for_run(run_id)
    if recorded is None:
        return {"found": False, "detail": RUN_NOT_IN_CAMPAIGN_DETAIL}
    verdict = recorded["output"] or {}
    return {
        "found": True,
        "run_id": run_id,
        "workload": (recorded["inputs"] or {}).get("workload"),
        "run_is_trustworthy": verdict.get("run_is_trustworthy"),
        "results_discriminate": verdict.get("results_discriminate"),
        "headline": verdict.get("headline"),
        "rationale": recorded["rationale"],
        "compounds": verdict.get("compounds") or [],
    }


async def read_previous_stage_compound_measurements(run_id: str) -> dict:
    """Open the numbers an earlier stage measured, with how far its ranking can be trusted.

    Args:
        run_id: A run id copied verbatim from campaign_history. Ids from anywhere
            else resolve to nothing.

    Returns:
        The per-compound measurements for that run, its control check, and its score_analysis block.
        Bulk per-frame series are omitted, and at most 25 compounds are returned.
    """
    recorded = await load_triage_decision_for_run(run_id)
    if recorded is None:
        return {"found": False, "detail": RUN_NOT_IN_CAMPAIGN_DETAIL}
    inputs = recorded["inputs"] or {}
    records = inputs.get("scores") or []
    return {
        "found": True,
        "run_id": run_id,
        "workload": inputs.get("workload"),
        "control": inputs.get("control") or {},
        "score_analysis": inputs.get("score_analysis") or {},
        "compounds_returned": min(len(records), MAXIMUM_COMPOUND_ROWS_RETURNED),
        "compounds_omitted": max(len(records) - MAXIMUM_COMPOUND_ROWS_RETURNED, 0),
        "compounds": [
            compound_measurement_without_bulk_series(record)
            for record in records[:MAXIMUM_COMPOUND_ROWS_RETURNED]
        ],
    }


async def read_previous_stage_request_card(run_id: str) -> dict:
    """Open the Trello card an earlier stage was requested on, in the scientist's own words.

    Args:
        run_id: A run id copied verbatim from campaign_history. Ids from anywhere
            else resolve to nothing.

    Returns:
        The title and description of the card that stage ran from, truncated to 4000 characters.
    """
    run = await load_run_workload_and_card_id(run_id)
    if run is None:
        return {"found": False, "detail": RUN_NOT_IN_CAMPAIGN_DETAIL}
    try:
        card = await trello.get_card(run["card_id"])
    except httpx.HTTPError as error:
        return {"found": False, "detail": f"Trello did not return card {run['card_id']}: {error}"}
    return {
        "found": True,
        "run_id": run_id,
        "workload": run["workload"],
        "card_id": run["card_id"],
        "title": card.get("name", ""),
        "description": (card.get("desc") or "")[:MAXIMUM_CARD_TEXT_CHARACTERS],
    }
