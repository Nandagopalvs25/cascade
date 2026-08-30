import json
import re
from typing import get_args

import pytest

from cascade.agents.prompts import INTAKE_INSTRUCTION
from cascade.agents.schemas import CampaignIntent
from cascade.schemas import JobSpec, TargetStructure, Workload

EXECUTABLE_WORKLOADS = ("dock", "admet", "md_stability", "cofold")


def test_workload_covers_every_container_in_the_workloads_directory():
    assert get_args(Workload) == EXECUTABLE_WORKLOADS


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
        requested_stages=["dock", "cofold"],
        rationale="dock then co-fold the survivors",
    )

    assert intent.requested_stages == ["dock", "cofold"]

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


def _objects_without_declared_properties(schema, path=""):
    from google.genai import types

    offenders = []
    if schema.type == types.Type.OBJECT and not schema.properties:
        offenders.append(path or "<root>")
    for name, child in (schema.properties or {}).items():
        offenders.extend(
            _objects_without_declared_properties(child, f"{path}.{name}" if path else name)
        )
    if schema.items:
        offenders.extend(_objects_without_declared_properties(schema.items, f"{path}[]"))
    return offenders


@pytest.mark.parametrize("agent_name", ("intake", "planner", "triage", "stage_decision"))
def test_agent_output_schemas_declare_every_object_property(agent_name):
    from google.genai import _transformers

    from cascade.agents import definitions

    agent = getattr(definitions, f"{agent_name}_agent")
    schema = _transformers.t_schema(None, agent.output_schema)

    assert _objects_without_declared_properties(schema) == []


def test_planner_can_emit_every_parameter_the_instruction_describes():
    from cascade.agents.policy import ALLOWED_JOB_PARAMS_BY_WORKLOAD
    from cascade.agents.schemas import WorkloadParams

    allowed = set().union(*ALLOWED_JOB_PARAMS_BY_WORKLOAD.values())

    assert set(WorkloadParams.model_fields) <= allowed


def test_every_workload_declares_the_inputs_it_requires():
    from cascade.agents.capabilities import STAGE_REQUIREMENTS

    assert tuple(STAGE_REQUIREMENTS) == get_args(Workload)


def test_every_workload_declares_what_it_produces():
    from cascade.agents.capabilities import PRODUCED_FILE_NAMES_BY_WORKLOAD

    assert tuple(PRODUCED_FILE_NAMES_BY_WORKLOAD) == get_args(Workload)


def test_only_the_safety_screen_runs_without_a_protein():
    from cascade.agents.capabilities import stage_requires_a_protein_structure

    without_a_protein = [
        workload
        for workload in get_args(Workload)
        if not stage_requires_a_protein_structure(workload)
    ]

    assert without_a_protein == ["admet"]


def test_a_protein_sequence_can_be_derived_from_a_structure():
    from cascade.agents.capabilities import (
        InputKind,
        inputs_available_after_derivation,
        unmet_inputs_for_stage,
    )

    assert inputs_available_after_derivation(frozenset({InputKind.PROTEIN_STRUCTURE})) == frozenset(
        {InputKind.PROTEIN_STRUCTURE, InputKind.PROTEIN_SEQUENCE}
    )
    assert (
        unmet_inputs_for_stage(
            "cofold", frozenset({InputKind.PROTEIN_STRUCTURE, InputKind.LIGAND_STRUCTURES})
        )
        == frozenset()
    )


def test_the_safety_screen_is_satisfied_by_compounds_alone():
    from cascade.agents.capabilities import InputKind, unmet_inputs_for_stage

    assert unmet_inputs_for_stage("admet", frozenset({InputKind.LIGAND_STRUCTURES})) == frozenset()
    assert unmet_inputs_for_stage("dock", frozenset({InputKind.LIGAND_STRUCTURES})) == frozenset(
        {InputKind.PROTEIN_STRUCTURE}
    )


def test_pose_stability_needs_both_a_receptor_and_poses():
    from cascade.agents.capabilities import InputKind, unmet_inputs_for_stage

    assert unmet_inputs_for_stage(
        "md_stability", frozenset({InputKind.POSED_COMPLEXES})
    ) == frozenset({InputKind.PROTEIN_STRUCTURE})
