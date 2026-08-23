import json
import re
from typing import get_args

import pytest
from pydantic import BaseModel

from cascade.agents.prompts import INTAKE_INSTRUCTION
from cascade.agents.schemas import CampaignIntent, Stage
from cascade.schemas import JobSpec, TargetStructure, Workload

EXECUTABLE_WORKLOADS = ("dock", "admet", "md_stability", "fold_affinity")


def test_workload_covers_every_container_in_the_workloads_directory():
    assert get_args(Workload) == EXECUTABLE_WORKLOADS


def test_stage_is_workload_plus_synthesis_and_stays_derived():
    assert get_args(Stage) == (*get_args(Workload), "synthesis")


def test_stage_serializes_as_a_flat_enum_for_gemini_output_schemas():
    class Probe(BaseModel):
        stage: Stage

    schema = Probe.model_json_schema()["properties"]["stage"]

    assert "anyOf" not in schema
    assert schema["enum"] == list(get_args(Stage))


@pytest.mark.parametrize("workload", EXECUTABLE_WORKLOADS)
def test_job_spec_accepts_every_executable_workload(workload):
    spec = JobSpec(
        run_id="run-1",
        workload=workload,
        target=TargetStructure(
            source="rcsb",
            reference="1HSG",
            pdb_id="1HSG",
            structure_uri="gs://cascade-test/runs/run-1/inputs/target.pdb",
        ),
        ligands_uri="gs://cascade-test/runs/run-1/inputs/ligands.sdf",
        output_uri="gs://cascade-test/runs/run-1/outputs",
    )

    assert spec.workload == workload


def test_job_spec_rejects_synthesis_because_it_has_no_container():
    with pytest.raises(ValueError):
        JobSpec(
            run_id="run-1",
            workload="synthesis",
            target=TargetStructure(
                source="card_attachment",
                reference="model.pdb",
                structure_uri="gs://cascade-test/x.pdb",
            ),
            ligands_uri="gs://cascade-test/in.sdf",
            output_uri="gs://cascade-test/out",
        )


@pytest.mark.parametrize("retired_name", ["fold", "fold-affinity", "foldaffinity"])
def test_retired_stage_spellings_are_rejected(retired_name):
    with pytest.raises(ValueError):
        CampaignIntent(requested_stages=[retired_name], rationale="test")


def test_campaign_intent_requests_only_executable_workloads():
    intent = CampaignIntent(
        requested_stages=["dock", "fold_affinity"],
        rationale="dock then co-fold the survivors",
    )

    assert intent.requested_stages == ["dock", "fold_affinity"]

    with pytest.raises(ValueError):
        CampaignIntent(requested_stages=["synthesis"], rationale="test")


def test_campaign_intent_schema_is_json_serializable_for_adk():
    json.dumps(CampaignIntent.model_json_schema())


def test_intake_prompt_offers_exactly_the_workloads_the_schema_accepts():
    for workload in get_args(Workload):
        assert workload in INTAKE_INSTRUCTION, f"prompt never mentions {workload!r}"


def test_intake_prompt_does_not_offer_the_retired_fold_stage():
    assert not re.search(r"\bfold\b", INTAKE_INSTRUCTION), (
        "prompt offers bare 'fold'; CampaignIntent.requested_stages would reject it"
    )
