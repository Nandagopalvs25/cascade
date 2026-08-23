import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from google.adk.agents.context import Context
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import node
from google.auth.exceptions import GoogleAuthError

from cascade.agents.app import cascade_app
from cascade.agents.nodes import deterministic_run_id
from cascade.agents.schemas import (
    CampaignIntent,
    CardInputs,
    PlanRequest,
    WorkloadPlan,
)
from cascade.config import get_settings
from cascade.db import get_db
from cascade.dependencies import get_runner
from cascade.main import app
from cascade.models import CardEvent
from cascade.schemas import TargetStructure
from cascade.security import verify_pubsub_oidc

INDINAVIR_SMILES = (
    "CC(C)(C)NC(=O)[C@@H]1CN(Cc2cccnc2)CCN1C[C@@H](O)C[C@@H](Cc1ccccc1)C(=O)N[C@H]1c2ccccc2C[C@H]1O"
)
CARD_DESCRIPTION = f"""Screen these against HIV protease (PDB 1HSG).
{INDINAVIR_SMILES} indinavir
CC(C)Cc1ccc(cc1)C(C)C(=O)O ibuprofen
CC(=O)Oc1ccccc1C(=O)O aspirin
Cn1cnc2c1c(=O)n(C)c(=O)n2C caffeine
"""


def _envelope(card_id: str, action_id: str) -> dict:
    data = json.dumps({"card_id": card_id, "action_id": action_id}).encode()
    return {"message": {"data": base64.b64encode(data).decode()}}


def _completion_envelope(payload: dict) -> dict:
    return {"message": {"data": base64.b64encode(json.dumps(payload).encode()).decode()}}


@node(name="intake")
async def stub_intake_agent(ctx: Context, node_input: CardInputs) -> CampaignIntent:
    return CampaignIntent(
        target_name="HIV protease",
        target_source="rcsb",
        target_reference="1HSG",
        ligand_source="smiles_in_text",
        ligand_reference="card_description",
        control_compound="indinavir",
        requested_stages=["dock"],
        ambiguities=[],
        rationale=f"Card {node_input.title!r} names a target and a compound set.",
    )


@node(name="intake")
async def stub_ambiguous_intake_agent(ctx: Context, node_input: CardInputs) -> CampaignIntent:
    return CampaignIntent(
        target_name=None,
        requested_stages=["dock"],
        ambiguities=["No target protein named on the card."],
        rationale="The card does not say what to screen against.",
    )


@node(name="planner")
async def stub_planner_agent(ctx: Context, node_input: PlanRequest) -> WorkloadPlan:
    return WorkloadPlan(
        workload=node_input.stage,
        binding_site=None,
        binding_site_method="co_crystal",
        binding_site_confidence="high",
        params={"exhaustiveness": 8, "num_modes": 9, "not_a_real_param": "drop me"},
        control_compound=node_input.intent.control_compound,
        rationale="Three compounds against a co-crystal pocket runs at default effort.",
    )


class RecordingGCS:
    bucket_name = "test-bucket"

    def __init__(self):
        self.uploaded_json: dict[str, dict] = {}
        self.uploaded_bytes: dict[str, tuple[bytes, str]] = {}

    def uri_for_path(self, path: str) -> str:
        return f"gs://{self.bucket_name}/{path}"

    async def upload_json(self, path: str, data: dict) -> str:
        self.uploaded_json[path] = data
        return self.uri_for_path(path)

    async def upload_bytes(self, path: str, data: bytes, content_type: str) -> str:
        self.uploaded_bytes[path] = (data, content_type)
        return self.uri_for_path(path)

    async def generate_signed_url_for_uri(
        self, uri: str, expiration_minutes: int, download_filename: str | None = None
    ) -> str:
        self.signed = (uri, expiration_minutes, download_filename)
        object_path = uri.removeprefix(f"gs://{self.bucket_name}/")
        return (
            f"https://storage.googleapis.com/{self.bucket_name}/{object_path}"
            f"?X-Goog-Expires={expiration_minutes * 60}"
        )


@pytest.fixture
def fake_trello():
    client = AsyncMock()
    client.get_card.return_value = {
        "name": "Screen 3 compounds",
        "desc": CARD_DESCRIPTION,
    }
    client.get_attachments.return_value = [{"name": "notes.txt"}]
    return client


@pytest.fixture
def fake_gcs():
    return RecordingGCS()


@pytest.fixture
def fake_job_client():
    client = AsyncMock()
    client.submit_workload_execution.return_value = (
        "projects/p/locations/us-central1/jobs/cascade-dock/executions/dock-xyz"
    )
    return client


@pytest.fixture
def fake_campaign_inputs():
    client = AsyncMock()
    client.resolve_target_structure.return_value = TargetStructure(
        source="rcsb",
        reference="1HSG",
        structure_uri="gs://test-bucket/runs/r/inputs/structure.pdb",
        pdb_id="1HSG",
    )
    return client


@pytest.fixture
def campaign_client(
    settings_override,
    fake_trello,
    fake_gcs,
    fake_job_client,
    fake_campaign_inputs,
    db_session_override,
    db_sessionmaker,
):
    runner = Runner(app=cascade_app, session_service=InMemorySessionService())
    app.dependency_overrides[get_settings] = lambda: settings_override
    app.dependency_overrides[get_db] = db_session_override
    app.dependency_overrides[get_runner] = lambda: runner
    app.dependency_overrides[verify_pubsub_oidc] = lambda: None
    with (
        patch("cascade.agents.nodes.trello", fake_trello),
        patch("cascade.agents.nodes.settings", settings_override),
        patch("cascade.agents.nodes.gcs", fake_gcs),
        patch("cascade.agents.nodes.cloud_run_jobs", fake_job_client),
        patch("cascade.agents.nodes.campaign_inputs", fake_campaign_inputs),
        patch("cascade.agents.persistence.async_session", db_sessionmaker),
        patch("cascade.agents.campaign.intake_agent", stub_intake_agent),
        patch("cascade.agents.campaign.planner_agent", stub_planner_agent),
    ):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _comment_texts(fake_trello) -> list[str]:
    return [call.args[1] for call in fake_trello.add_comment.await_args_list]


def test_card_event_submits_a_docking_job_and_suspends(
    campaign_client, fake_trello, fake_gcs, fake_job_client
):
    response = campaign_client.post("/pubsub/card-events", json=_envelope("card-abc", "act-1"))

    assert response.status_code == 200
    assert response.json()["status"] == "started"

    fake_job_client.submit_workload_execution.assert_awaited_once()
    submitted_spec = fake_job_client.submit_workload_execution.await_args.args[0]
    run_id = deterministic_run_id("card-abc", "dock")

    assert submitted_spec.run_id == run_id
    assert submitted_spec.workload == "dock"
    assert submitted_spec.control_compound == "indinavir"
    assert submitted_spec.params == {"exhaustiveness": 8, "num_modes": 9}
    assert submitted_spec.output_uri == f"gs://test-bucket/runs/{run_id}/outputs"

    library, content_type = fake_gcs.uploaded_bytes[f"runs/{run_id}/inputs/ligands.smi"]
    assert content_type == "text/plain"
    assert library.decode().splitlines() == [
        f"{INDINAVIR_SMILES}\tindinavir",
        "CC(C)Cc1ccc(cc1)C(C)C(=O)O\tibuprofen",
        "CC(=O)Oc1ccccc1C(=O)O\taspirin",
        "Cn1cnc2c1c(=O)n(C)c(=O)n2C\tcaffeine",
    ]

    assert any("dock job submitted" in text for text in _comment_texts(fake_trello))
    assert submitted_spec.ligands_uri == f"gs://test-bucket/runs/{run_id}/inputs/ligands.smi"
    fake_trello.move_card.assert_awaited_once_with("card-abc", "list-in-progress")


def test_completion_event_finishes_the_campaign_and_moves_the_card_to_done(
    campaign_client, fake_trello
):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-done", "act-done"))
    run_id = deterministic_run_id("card-done", "dock")

    completion = campaign_client.post(
        "/pubsub/job-completions",
        json=_completion_envelope(
            {
                "run_id": run_id,
                "workload": "dock",
                "status": "succeeded",
                "exit_code": 0,
                "results_uri": f"gs://test-bucket/runs/{run_id}/outputs",
                "results_archive_uri": f"gs://test-bucket/runs/{run_id}/outputs/results.zip",
                "summary": {
                    "ligands_docked": 3,
                    "ligands_failed": 0,
                    "best_compound_id": "ibuprofen",
                    "best_affinity_kcal_per_mol": -7.4,
                    "control_compound": {
                        "requested_name": "indinavir",
                        "status": "passed",
                        "lowest_mode_rmsd_angstrom": 0.83,
                        "lowest_mode_rank": 3,
                    },
                },
            }
        ),
    )

    assert completion.status_code == 200
    assert completion.json() == {"status": "resumed", "run_id": run_id}

    assert fake_trello.move_card.await_args.args == ("card-done", "list-done")
    final_comment = _comment_texts(fake_trello)[-1]
    assert "3 compounds docked" in final_comment
    assert (
        "[Download results](https://storage.googleapis.com/test-bucket/"
        f"runs/{run_id}/outputs/results.zip?X-Goog-Expires=604800)"
    ) in final_comment
    assert "gs://" not in final_comment.split("Archived at")[0]
    assert "ibuprofen" in final_comment
    assert "passed" in final_comment


def test_failed_job_escalates_to_needs_attention(campaign_client, fake_trello):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-fail", "act-fail"))
    run_id = deterministic_run_id("card-fail", "dock")

    completion = campaign_client.post(
        "/pubsub/job-completions",
        json=_completion_envelope(
            {
                "run_id": run_id,
                "workload": "dock",
                "status": "failed",
                "exit_code": 1,
                "error_type": "ValueError",
                "error": "receptor had no atoms",
            }
        ),
    )

    assert completion.json()["status"] == "resumed"
    assert fake_trello.move_card.await_args.args == ("card-fail", "list-needs-attention")
    assert "receptor had no atoms" in _comment_texts(fake_trello)[-1]


def test_duplicate_completion_delivery_is_ignored(campaign_client, fake_trello):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-dup2", "act-dup2"))
    run_id = deterministic_run_id("card-dup2", "dock")
    payload = _completion_envelope(
        {
            "run_id": run_id,
            "workload": "dock",
            "status": "succeeded",
            "exit_code": 0,
            "results_uri": f"gs://test-bucket/runs/{run_id}/outputs",
            "summary": {"ligands_docked": 3, "best_compound_id": "aspirin"},
        }
    )

    first = campaign_client.post("/pubsub/job-completions", json=payload)
    second = campaign_client.post("/pubsub/job-completions", json=payload)

    assert first.json()["status"] == "resumed"
    assert second.json()["status"] == "ignored"
    assert len([c for c in _comment_texts(fake_trello) if "finished" in c]) == 1


def test_completion_for_an_unknown_run_is_ignored(campaign_client):
    response = campaign_client.post(
        "/pubsub/job-completions",
        json=_completion_envelope(
            {
                "run_id": deterministic_run_id("never-submitted", "dock"),
                "workload": "dock",
                "status": "succeeded",
                "exit_code": 0,
            }
        ),
    )
    assert response.json()["status"] == "ignored"


def test_ambiguous_card_asks_and_does_not_advance(campaign_client, fake_trello, fake_job_client):
    with patch("cascade.agents.campaign.intake_agent", stub_ambiguous_intake_agent):
        response = campaign_client.post("/pubsub/card-events", json=_envelope("card-xyz", "act-2"))

    assert response.json()["status"] == "needs_clarification"
    assert "No target protein named on the card." in _comment_texts(fake_trello)[0]
    fake_trello.move_card.assert_awaited_once_with("card-xyz", "list-needs-attention")
    fake_job_client.submit_workload_execution.assert_not_awaited()


def test_card_event_without_card_id_is_rejected(campaign_client):
    envelope = {"message": {"data": base64.b64encode(json.dumps({}).encode()).decode()}}
    response = campaign_client.post("/pubsub/card-events", json=envelope)
    assert response.status_code == 400


def _seed_card_event(db_sessionmaker, action_id: str) -> None:
    async def seed():
        async with db_sessionmaker() as session:
            session.add(
                CardEvent(trello_action_id=action_id, kind="card.created_in_todo", payload={})
            )
            await session.commit()

    asyncio.run(seed())


def test_duplicate_delivery_of_same_action_does_not_rerun(
    campaign_client, fake_trello, fake_job_client, db_sessionmaker
):
    _seed_card_event(db_sessionmaker, "act-dup")
    first = campaign_client.post("/pubsub/card-events", json=_envelope("card-dup", "act-dup"))
    second = campaign_client.post("/pubsub/card-events", json=_envelope("card-dup", "act-dup"))

    assert first.json()["status"] == "started"
    assert second.json()["status"] == "duplicate"
    assert fake_job_client.submit_workload_execution.await_count == 1


def test_library_too_large_for_cloud_run_stops_instead_of_submitting(
    campaign_client, fake_trello, fake_job_client, settings_override
):
    tiny_ceiling = settings_override.model_copy(update={"max_ligands_per_cloud_run_job": 2})
    with patch("cascade.agents.nodes.settings", tiny_ceiling):
        response = campaign_client.post(
            "/pubsub/card-events", json=_envelope("card-big", "act-big")
        )

    assert response.json()["status"] == "unsupported_executor"
    fake_job_client.submit_workload_execution.assert_not_awaited()
    assert fake_trello.move_card.await_args.args == ("card-big", "list-needs-attention")
    assert "exceeds" in _comment_texts(fake_trello)[-1]


@node(name="intake")
async def stub_admet_intake_agent(ctx: Context, node_input: CardInputs) -> CampaignIntent:
    return CampaignIntent(
        target_name="HIV protease",
        target_source="rcsb",
        target_reference="1HSG",
        ligand_source="smiles_in_text",
        ligand_reference="card_description",
        requested_stages=["admet"],
        ambiguities=[],
        rationale="The card asks for ADMET predictions.",
    )


def test_stage_without_a_container_is_refused_before_submission(
    campaign_client, fake_trello, fake_job_client
):
    with patch("cascade.agents.campaign.intake_agent", stub_admet_intake_agent):
        response = campaign_client.post(
            "/pubsub/card-events", json=_envelope("card-admet", "act-admet")
        )

    assert response.json()["status"] == "unsupported_executor"
    fake_job_client.submit_workload_execution.assert_not_awaited()
    assert "no workload container yet" in _comment_texts(fake_trello)[-1]
    assert fake_trello.move_card.await_args.args == ("card-admet", "list-needs-attention")


def test_finished_campaign_is_not_run_again_for_a_new_card_event(
    campaign_client, fake_trello, fake_job_client
):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-again", "act-again-1"))
    run_id = deterministic_run_id("card-again", "dock")
    campaign_client.post(
        "/pubsub/job-completions",
        json=_completion_envelope(
            {
                "run_id": run_id,
                "workload": "dock",
                "status": "succeeded",
                "exit_code": 0,
                "results_uri": f"gs://test-bucket/runs/{run_id}/outputs",
                "summary": {"ligands_docked": 4, "best_compound_id": "indinavir"},
            }
        ),
    )
    submissions_before = fake_job_client.submit_workload_execution.await_count

    second = campaign_client.post(
        "/pubsub/card-events", json=_envelope("card-again", "act-again-2")
    )

    assert second.json()["status"] == "already_complete"
    assert fake_job_client.submit_workload_execution.await_count == submissions_before


def test_second_card_event_while_the_job_runs_does_not_submit_twice(
    campaign_client, fake_job_client
):
    first = campaign_client.post("/pubsub/card-events", json=_envelope("card-twice", "act-t1"))
    second = campaign_client.post("/pubsub/card-events", json=_envelope("card-twice", "act-t2"))

    assert first.json()["status"] == "started"
    assert second.json()["status"] == "started"
    assert fake_job_client.submit_workload_execution.await_count == 1


def test_completion_with_a_malformed_run_id_is_ignored_not_a_server_error(campaign_client):
    response = campaign_client.post(
        "/pubsub/job-completions",
        json=_completion_envelope(
            {"run_id": "not-a-uuid", "workload": "dock", "status": "succeeded", "exit_code": 0}
        ),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_results_link_falls_back_to_an_https_url_when_signing_fails(
    campaign_client, fake_trello, fake_gcs
):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-nosign", "act-nosign"))
    run_id = deterministic_run_id("card-nosign", "dock")
    archive_uri = f"gs://test-bucket/runs/{run_id}/outputs/results.zip"
    fake_gcs.generate_signed_url_for_uri = AsyncMock(
        side_effect=GoogleAuthError("no signing credentials")
    )

    completion = campaign_client.post(
        "/pubsub/job-completions",
        json=_completion_envelope(
            {
                "run_id": run_id,
                "workload": "dock",
                "status": "succeeded",
                "exit_code": 0,
                "results_uri": f"gs://test-bucket/runs/{run_id}/outputs",
                "results_archive_uri": archive_uri,
                "summary": {"ligands_docked": 4, "best_compound_id": "indinavir"},
            }
        ),
    )

    assert completion.json()["status"] == "resumed"
    final_comment = _comment_texts(fake_trello)[-1]
    assert (
        f"https://storage.cloud.google.com/test-bucket/runs/{run_id}/outputs/results.zip"
        in final_comment
    )
    assert "](gs://" not in final_comment
    assert fake_trello.move_card.await_args.args == ("card-nosign", "list-done")


@node(name="intake")
async def stub_intake_agent_expecting_four(ctx: Context, node_input: CardInputs) -> CampaignIntent:
    return CampaignIntent(
        target_name="HIV protease",
        target_source="rcsb",
        target_reference="1HSG",
        ligand_source="smiles_in_text",
        ligand_reference="card_description",
        control_compound="indinavir",
        expected_compound_count=4,
        requested_stages=["dock"],
        ambiguities=[],
        rationale="Four compounds with indinavir as the control.",
    )


def test_missing_control_compound_stops_before_paying_for_a_docking_run(
    campaign_client, fake_trello, fake_job_client
):
    fake_trello.get_card.return_value = {
        "name": "Screen 4 compounds",
        "desc": (
            "Target: PDB 1HSG. Control compound: indinavir\n"
            "CC(C)Cc1ccc(cc1)C(C)C(=O)O ibuprofen\n"
            "CC(=O)Oc1ccccc1C(=O)O aspirin\n"
            "Cn1cnc2c1c(=O)n(C)c(=O)n2C caffeine\n"
        ),
    }
    with patch("cascade.agents.campaign.intake_agent", stub_intake_agent_expecting_four):
        response = campaign_client.post(
            "/pubsub/card-events", json=_envelope("card-nocontrol", "act-nocontrol")
        )

    assert response.json()["status"] == "needs_clarification"
    fake_job_client.submit_workload_execution.assert_not_awaited()
    question = _comment_texts(fake_trello)[-1]
    assert "control compound 'indinavir' is not among" in question
    assert "Markdown link syntax" in question
    assert fake_trello.move_card.await_args.args == ("card-nocontrol", "list-needs-attention")


def test_markdown_mangled_smiles_is_reported_not_silently_dropped():
    from cascade.agents.nodes import smiles_library_lines_from_text

    mangled = "CC(C)(C)NC(=O)[C@@H ]1CN(Cc2cccnc2)CCN1CC@@HCC@@H indinavir"
    kept, skipped = smiles_library_lines_from_text(mangled)
    assert kept == []
    assert skipped == 1


def test_backtick_wrapped_smiles_survives_the_parser():
    from cascade.agents.nodes import smiles_library_lines_from_text

    kept, skipped = smiles_library_lines_from_text("`CC(=O)Oc1ccccc1C(=O)O` aspirin")
    assert kept == ["CC(=O)Oc1ccccc1C(=O)O\taspirin"]
    assert skipped == 0
