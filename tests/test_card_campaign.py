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
from sqlalchemy import select

from cascade.agents.app import cascade_app
from cascade.agents.card_text import parent_run_id_in_card_description
from cascade.agents.compound_library import smiles_library_lines_from_text
from cascade.agents.policy import deterministic_run_id
from cascade.agents.schemas import (
    CampaignIntent,
    CardInputs,
    CompoundJudgement,
    PlanRequest,
    ProposalRequest,
    StageProposal,
    TriageRequest,
    TriageVerdict,
    WorkloadPlan,
)
from cascade.config import get_settings
from cascade.db import get_db
from cascade.dependencies import get_runner
from cascade.main import app
from cascade.models import CardEvent, Decision, Run
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


@node(name="triage")
async def stub_triage_agent(ctx: Context, node_input: TriageRequest) -> TriageVerdict:
    return TriageVerdict(
        run_is_trustworthy=True,
        results_discriminate=True,
        next_action="complete",
        headline=f"{node_input.workload} attempt {node_input.attempt} looks usable.",
        compounds=[],
        rationale="Stub triage accepted the run without reading the numbers.",
    )


@node(name="triage")
async def stub_promoting_triage_agent(ctx: Context, node_input: TriageRequest) -> TriageVerdict:
    return TriageVerdict(
        run_is_trustworthy=True,
        results_discriminate=True,
        next_action="complete",
        headline="Two compounds separated from the rest.",
        compounds=[
            CompoundJudgement(
                compound_id="indinavir", disposition="promote", reason="-11.2 kcal/mol"
            ),
            CompoundJudgement(compound_id="aspirin", disposition="promote", reason="-8.9 kcal/mol"),
            CompoundJudgement(compound_id="caffeine", disposition="reject", reason="-4.1 kcal/mol"),
        ],
        rationale="Stub triage promoted two compounds.",
    )


@node(name="triage")
async def stub_untrustworthy_triage_agent(ctx: Context, node_input: TriageRequest) -> TriageVerdict:
    return TriageVerdict(
        run_is_trustworthy=False,
        results_discriminate=True,
        next_action="complete",
        headline="The pipeline itself is suspect.",
        compounds=[
            CompoundJudgement(
                compound_id="indinavir", disposition="promote", reason="-11.2 kcal/mol"
            )
        ],
        rationale="Stub triage does not trust this run.",
    )


@node(name="proposer")
async def stub_proposer_agent(ctx: Context, node_input: ProposalRequest) -> StageProposal:
    return StageProposal(
        next_stage=node_input.runnable_next_stages[0],
        card_title=f"Safety screen on {len(node_input.carried_compounds)} compounds",
        reason="No liability data exists for these yet, so screen before spending on simulation.",
        rationale="Stub proposer picked the only runnable stage.",
    )


@node(name="proposer")
async def stub_declining_proposer_agent(ctx: Context, node_input: ProposalRequest) -> StageProposal:
    return StageProposal(
        next_stage=None,
        card_title="Nothing to propose",
        reason="Nothing this stage produced would be answered by another stage.",
        rationale="Stub proposer declined.",
    )


class RecordingGCS:
    bucket_name = "test-bucket"

    def __init__(self):
        self.uploaded_json: dict[str, dict] = {}
        self.uploaded_bytes: dict[str, tuple[bytes, str]] = {}
        self.manifests: dict[str, dict] = {}

    async def download_json_from_uri(self, uri: str) -> dict:
        return self.manifests[uri]

    async def download_text_from_uri(self, uri: str) -> str:
        path = uri.removeprefix(f"gs://{self.bucket_name}/")
        return self.uploaded_bytes[path][0].decode()

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
    client.create_card.return_value = {
        "id": "card-proposed",
        "url": "https://trello.com/c/card-proposed",
    }
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
        patch("cascade.agents.policy.settings", settings_override),
        patch("cascade.agents.nodes.gcs", fake_gcs),
        patch("cascade.agents.nodes.cloud_run_jobs", fake_job_client),
        patch("cascade.agents.nodes.campaign_inputs", fake_campaign_inputs),
        patch("cascade.agents.persistence.async_session", db_sessionmaker),
        patch("cascade.agents.campaign.intake_agent", stub_intake_agent),
        patch("cascade.agents.campaign.planner_agent", stub_planner_agent),
        patch("cascade.agents.campaign.triage_agent", stub_triage_agent),
        patch("cascade.agents.campaign.proposer_agent", stub_proposer_agent),
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
                        "status": "measured",
                        "rmsd_to_cocrystal_angstrom": 0.83,
                        "lowest_mode_rmsd_angstrom": 0.83,
                        "lowest_mode_rank": 1,
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
    assert "**Triage.**" in final_comment


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
    with patch("cascade.agents.policy.settings", tiny_ceiling):
        response = campaign_client.post(
            "/pubsub/card-events", json=_envelope("card-big", "act-big")
        )

    assert response.json()["status"] == "unsupported_executor"
    fake_job_client.submit_workload_execution.assert_not_awaited()
    assert fake_trello.move_card.await_args.args == ("card-big", "list-needs-attention")
    assert "exceeds" in _comment_texts(fake_trello)[-1]


@node(name="intake")
async def stub_md_stability_intake_agent(ctx: Context, node_input: CardInputs) -> CampaignIntent:
    return CampaignIntent(
        target_name="HIV protease",
        target_source="rcsb",
        target_reference="1HSG",
        ligand_source="smiles_in_text",
        ligand_reference="card_description",
        requested_stages=["md_stability"],
        ambiguities=[],
        rationale="The card asks for an MD stability check.",
    )


async def stub_cofold_intake_agent(ctx: Context, node_input: CardInputs) -> CampaignIntent:
    return CampaignIntent(
        target_name="HIV protease",
        target_source="rcsb",
        target_reference="1HSG",
        ligand_source="smiles_in_text",
        ligand_reference="card_description",
        requested_stages=["cofold"],
        ambiguities=[],
        rationale="The card asks for a co-folded complex.",
    )


def test_stage_without_a_container_is_refused_before_submission(
    campaign_client, fake_trello, fake_job_client
):
    with patch("cascade.agents.campaign.intake_agent", stub_md_stability_intake_agent):
        response = campaign_client.post("/pubsub/card-events", json=_envelope("card-md", "act-md"))

    assert response.json()["status"] == "unsupported_executor"
    fake_job_client.submit_workload_execution.assert_not_awaited()
    assert "no workload container yet" in _comment_texts(fake_trello)[-1]
    assert fake_trello.move_card.await_args.args == ("card-md", "list-needs-attention")


def test_stage_with_a_container_but_no_gpu_executor_is_refused_before_submission(
    campaign_client, fake_trello, fake_job_client
):
    with patch("cascade.agents.campaign.intake_agent", stub_cofold_intake_agent):
        response = campaign_client.post(
            "/pubsub/card-events", json=_envelope("card-fold", "act-fold")
        )

    assert response.json()["status"] == "unsupported_executor"
    fake_job_client.submit_workload_execution.assert_not_awaited()
    assert "needs a GPU executor" in _comment_texts(fake_trello)[-1]
    assert fake_trello.move_card.await_args.args == ("card-fold", "list-needs-attention")


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
    from cascade.agents.compound_library import smiles_library_lines_from_text

    mangled = "CC(C)(C)NC(=O)[C@@H ]1CN(Cc2cccnc2)CCN1CC@@HCC@@H indinavir"
    kept, skipped = smiles_library_lines_from_text(mangled)
    assert kept == []
    assert skipped == 1


def test_backtick_wrapped_smiles_survives_the_parser():
    from cascade.agents.compound_library import smiles_library_lines_from_text

    kept, skipped = smiles_library_lines_from_text("`CC(=O)Oc1ccccc1C(=O)O` aspirin")
    assert kept == ["CC(=O)Oc1ccccc1C(=O)O\taspirin"]
    assert skipped == 0


def _dock_completion(run_id: str, top_pose_rmsd: float, best: str = "ibuprofen") -> dict:
    return {
        "run_id": run_id,
        "workload": "dock",
        "status": "succeeded",
        "exit_code": 0,
        "results_uri": f"gs://test-bucket/runs/{run_id}/outputs",
        "results_archive_uri": f"gs://test-bucket/runs/{run_id}/outputs/results.zip",
        "summary": {
            "ligands_docked": 4,
            "ligands_failed": 0,
            "best_compound_id": best,
            "best_affinity_kcal_per_mol": -7.4,
            "control_compound": {
                "requested_name": "indinavir",
                "status": "measured",
                "rmsd_to_cocrystal_angstrom": top_pose_rmsd,
                "lowest_mode_rmsd_angstrom": top_pose_rmsd,
                "lowest_mode_rank": 1,
            },
        },
    }


def test_a_failed_control_reruns_the_job_at_higher_effort_and_then_completes(
    campaign_client, fake_trello, fake_job_client
):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-act4", "act-4"))
    run_id = deterministic_run_id("card-act4", "dock")

    first = campaign_client.post(
        "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 2.447))
    )

    assert first.status_code == 200
    comments_after_first = "\n".join(_comment_texts(fake_trello))
    assert "Control compound check failed" in comments_after_first
    assert "more starting conformers per compound" in comments_after_first
    assert "(attempt 2)" in comments_after_first
    assert fake_trello.move_card.await_args.args != ("card-act4", "list-done")

    submitted_specs = [
        call.args[0] for call in fake_job_client.submit_workload_execution.await_args_list
    ]
    assert len(submitted_specs) == 2
    assert submitted_specs[0].params["exhaustiveness"] == 8
    assert submitted_specs[1].params["exhaustiveness"] == 8
    assert submitted_specs[1].params["conformers_per_ligand"] == 8
    assert submitted_specs[1].params["num_modes"] == 9
    assert submitted_specs[1].run_id == run_id

    second = campaign_client.post(
        "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
    )

    assert second.status_code == 200
    assert fake_trello.move_card.await_args.args == ("card-act4", "list-done")
    final_comment = _comment_texts(fake_trello)[-1]
    assert "**Triage.**" in final_comment
    assert "Control `indinavir`: passed" in final_comment


def _dock_completion_with_pose_off_rank(run_id: str) -> dict:
    completion = _dock_completion(run_id, 0.826)
    completion["summary"]["control_compound"] = {
        "requested_name": "indinavir",
        "status": "measured",
        "rmsd_to_cocrystal_angstrom": 10.532,
        "lowest_mode_rmsd_angstrom": 0.826,
        "lowest_mode_rank": 4,
    }
    return completion


def test_a_control_pose_found_off_rank_completes_without_spending_a_retry(
    campaign_client, fake_trello, fake_job_client
):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-offrank", "act-offrank"))
    run_id = deterministic_run_id("card-offrank", "dock")

    response = campaign_client.post(
        "/pubsub/job-completions",
        json=_completion_envelope(_dock_completion_with_pose_off_rank(run_id)),
    )

    assert response.status_code == 200
    assert fake_job_client.submit_workload_execution.await_count == 1
    final_comment = _comment_texts(fake_trello)[-1]
    assert "Control `indinavir`: partial" in final_comment
    assert "recovered at rank 4" in final_comment
    assert "do NOT separate" in final_comment


def test_a_control_that_fails_twice_escalates_instead_of_running_forever(
    campaign_client, fake_trello, fake_job_client
):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-stuck", "act-stuck"))
    run_id = deterministic_run_id("card-stuck", "dock")

    campaign_client.post(
        "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 2.447))
    )
    campaign_client.post(
        "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 3.1))
    )

    assert fake_trello.move_card.await_args.args == ("card-stuck", "list-needs-attention")
    assert fake_job_client.submit_workload_execution.await_count == 2
    final_comment = _comment_texts(fake_trello)[-1]
    assert "Control `indinavir`: FAILED" in final_comment
    assert "needs a scientist" in final_comment


def test_triage_writes_a_row_to_the_decisions_table(campaign_client, db_sessionmaker):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-logged", "act-logged"))
    run_id = deterministic_run_id("card-logged", "dock")
    campaign_client.post(
        "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
    )

    async def load_decisions():
        async with db_sessionmaker() as session:
            result = await session.execute(select(Decision))
            return result.scalars().all()

    decisions = asyncio.run(load_decisions())

    by_agent = {decision.agent: decision for decision in decisions}

    assert set(by_agent) == {"planner", "triage"}
    assert by_agent["planner"].decision_kind == "dock_plan"
    assert by_agent["planner"].rationale
    assert by_agent["planner"].output["params"] is not None

    assert by_agent["triage"].decision_kind == "dock_triage"
    assert by_agent["triage"].rationale
    assert by_agent["triage"].inputs["control"]["verdict"] == "passed"
    assert by_agent["triage"].output["next_action"] == "complete"


def test_a_trustworthy_run_with_promoted_hits_creates_a_card_in_recommended(
    campaign_client, fake_trello
):
    with patch("cascade.agents.campaign.triage_agent", stub_promoting_triage_agent):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-prop", "act-prop"))
        run_id = deterministic_run_id("card-prop", "dock")
        campaign_client.post(
            "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
        )

    fake_trello.create_card.assert_awaited_once()
    list_id, title, description = fake_trello.create_card.await_args.args

    assert list_id == "list-recommended"
    assert "2 compounds" in title
    assert INDINAVIR_SMILES in description
    assert "caffeine" not in description
    assert fake_trello.move_card.await_args.args == ("card-prop", "list-done")


def test_the_recommended_card_is_readable_by_a_fresh_campaign(campaign_client, fake_trello):
    with patch("cascade.agents.campaign.triage_agent", stub_promoting_triage_agent):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-read", "act-read"))
        run_id = deterministic_run_id("card-read", "dock")
        campaign_client.post(
            "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
        )

    description = fake_trello.create_card.await_args.args[2]
    parsed, _ = smiles_library_lines_from_text(description)

    assert [line.split("\t")[1] for line in parsed] == ["indinavir", "aspirin"]
    assert parent_run_id_in_card_description(description) == run_id


def test_the_original_card_comment_links_the_card_the_agent_created(campaign_client, fake_trello):
    with patch("cascade.agents.campaign.triage_agent", stub_promoting_triage_agent):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-link", "act-link"))
        run_id = deterministic_run_id("card-link", "dock")
        campaign_client.post(
            "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
        )

    final_comment = _comment_texts(fake_trello)[-1]

    assert "Proposed next step: admet" in final_comment
    assert "`indinavir`, `aspirin`" in final_comment


def test_a_run_that_promoted_nothing_completes_without_proposing_a_card(
    campaign_client, fake_trello
):
    campaign_client.post("/pubsub/card-events", json=_envelope("card-none", "act-none"))
    run_id = deterministic_run_id("card-none", "dock")
    campaign_client.post(
        "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
    )

    fake_trello.create_card.assert_not_awaited()
    assert fake_trello.move_card.await_args.args == ("card-none", "list-done")
    assert "**No follow-up proposed.**" in _comment_texts(fake_trello)[-1]


def test_an_untrustworthy_run_proposes_nothing_even_with_promoted_compounds(
    campaign_client, fake_trello
):
    with patch("cascade.agents.campaign.triage_agent", stub_untrustworthy_triage_agent):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-untrust", "act-untrust"))
        run_id = deterministic_run_id("card-untrust", "dock")
        campaign_client.post(
            "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
        )

    fake_trello.create_card.assert_not_awaited()


def test_a_proposer_that_declines_creates_no_card_and_says_why(campaign_client, fake_trello):
    with (
        patch("cascade.agents.campaign.triage_agent", stub_promoting_triage_agent),
        patch("cascade.agents.campaign.proposer_agent", stub_declining_proposer_agent),
    ):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-decl", "act-decl"))
        run_id = deterministic_run_id("card-decl", "dock")
        campaign_client.post(
            "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
        )

    fake_trello.create_card.assert_not_awaited()
    assert "**No follow-up proposed.**" in _comment_texts(fake_trello)[-1]


def test_a_duplicate_completion_delivery_does_not_create_a_second_recommended_card(
    campaign_client, fake_trello
):
    with patch("cascade.agents.campaign.triage_agent", stub_promoting_triage_agent):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-twice", "act-twice"))
        run_id = deterministic_run_id("card-twice", "dock")
        payload = _completion_envelope(_dock_completion(run_id, 0.83))
        campaign_client.post("/pubsub/job-completions", json=payload)
        campaign_client.post("/pubsub/job-completions", json=payload)

    assert fake_trello.create_card.await_count == 1


def test_the_proposer_writes_a_decision_row_naming_the_card_it_created(
    campaign_client, db_sessionmaker
):
    with patch("cascade.agents.campaign.triage_agent", stub_promoting_triage_agent):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-dec", "act-dec"))
        run_id = deterministic_run_id("card-dec", "dock")
        campaign_client.post(
            "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
        )

    async def load_proposer_decisions():
        async with db_sessionmaker() as session:
            result = await session.execute(select(Decision).where(Decision.agent == "proposer"))
            return result.scalars().all()

    decisions = asyncio.run(load_proposer_decisions())

    assert len(decisions) == 1
    assert decisions[0].decision_kind == "dock_followup"
    assert decisions[0].rationale
    assert decisions[0].output["created_card_id"] == "card-proposed"
    assert decisions[0].output["next_stage"] == "admet"
    assert decisions[0].inputs["carried_disposition"] == "promote"


def test_a_dragged_proposal_card_records_the_docking_run_as_its_parent(
    campaign_client, fake_trello, db_sessionmaker
):
    with patch("cascade.agents.campaign.triage_agent", stub_promoting_triage_agent):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-parent", "act-parent"))
        dock_run_id = deterministic_run_id("card-parent", "dock")
        campaign_client.post(
            "/pubsub/job-completions",
            json=_completion_envelope(_dock_completion(dock_run_id, 0.83)),
        )

    proposed_description = fake_trello.create_card.await_args.args[2]
    fake_trello.get_card.return_value = {
        "name": "Safety screen on 2 compounds",
        "desc": proposed_description,
    }
    campaign_client.post("/pubsub/card-events", json=_envelope("card-child", "act-child"))

    async def load_parent_of_child():
        async with db_sessionmaker() as session:
            result = await session.execute(
                select(Run.parent_run_id).where(Run.trello_card_id == "card-child")
            )
            return result.scalar_one()

    assert str(asyncio.run(load_parent_of_child())) == dock_run_id


def test_the_original_card_says_which_stages_could_not_be_proposed(campaign_client, fake_trello):
    with patch("cascade.agents.campaign.triage_agent", stub_promoting_triage_agent):
        campaign_client.post("/pubsub/card-events", json=_envelope("card-block", "act-block"))
        run_id = deterministic_run_id("card-block", "dock")
        campaign_client.post(
            "/pubsub/job-completions", json=_completion_envelope(_dock_completion(run_id, 0.83))
        )

    final_comment = _comment_texts(fake_trello)[-1]

    assert "`md_stability` - md_stability has no workload container yet" in final_comment
    assert "`cofold` - cofold needs a GPU executor" in final_comment
