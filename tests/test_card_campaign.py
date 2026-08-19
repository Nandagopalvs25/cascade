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

from cascade.agents.app import cascade_app
from cascade.agents.schemas import CampaignIntent, CardInputs
from cascade.config import get_settings
from cascade.db import get_db
from cascade.dependencies import get_runner
from cascade.main import app
from cascade.models import CardEvent
from cascade.security import verify_pubsub_oidc


def _envelope(card_id: str, action_id: str) -> dict:
    data = json.dumps({"card_id": card_id, "action_id": action_id}).encode()
    return {"message": {"data": base64.b64encode(data).decode()}}


@node(name="intake")
async def stub_intake_agent(ctx: Context, node_input: CardInputs) -> CampaignIntent:
    return CampaignIntent(
        target_name="HIV protease",
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


@pytest.fixture
def fake_trello():
    client = AsyncMock()
    client.get_card.return_value = {"name": "Screen 40 compounds", "desc": "against HIV protease"}
    client.get_attachments.return_value = [{"name": "compounds.smi"}]
    return client


@pytest.fixture
def campaign_client(settings_override, fake_trello, db_session_override):
    runner = Runner(app=cascade_app, session_service=InMemorySessionService())
    app.dependency_overrides[get_settings] = lambda: settings_override
    app.dependency_overrides[get_db] = db_session_override
    app.dependency_overrides[get_runner] = lambda: runner
    app.dependency_overrides[verify_pubsub_oidc] = lambda: None
    with (
        patch("cascade.agents.nodes.trello", fake_trello),
        patch("cascade.agents.nodes.settings", settings_override),
    ):
        yield TestClient(app)
    app.dependency_overrides.clear()


def test_card_event_runs_campaign_and_comments_on_card(campaign_client, fake_trello):
    with patch("cascade.agents.campaign.intake_agent", stub_intake_agent):
        response = campaign_client.post("/pubsub/card-events", json=_envelope("card-abc", "act-1"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "acknowledged"
    assert body["card_id"] == "card-abc"

    fake_trello.get_card.assert_awaited_once_with("card-abc")
    comment_card_id, comment_text = fake_trello.add_comment.await_args.args
    assert comment_card_id == "card-abc"
    assert "HIV protease" in comment_text
    fake_trello.move_card.assert_awaited_once_with("card-abc", "list-in-progress")


def test_ambiguous_card_asks_and_does_not_advance(campaign_client, fake_trello):
    with patch("cascade.agents.campaign.intake_agent", stub_ambiguous_intake_agent):
        response = campaign_client.post("/pubsub/card-events", json=_envelope("card-xyz", "act-2"))

    assert response.status_code == 200
    assert response.json()["status"] == "needs_clarification"

    _, comment_text = fake_trello.add_comment.await_args.args
    assert "No target protein named on the card." in comment_text
    fake_trello.move_card.assert_awaited_once_with("card-xyz", "list-needs-attention")


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
    campaign_client, fake_trello, db_sessionmaker
):
    _seed_card_event(db_sessionmaker, "act-dup")
    with patch("cascade.agents.campaign.intake_agent", stub_intake_agent):
        first = campaign_client.post("/pubsub/card-events", json=_envelope("card-dup", "act-dup"))
        second = campaign_client.post("/pubsub/card-events", json=_envelope("card-dup", "act-dup"))

    assert first.json()["status"] == "acknowledged"
    assert second.json()["status"] == "duplicate"
    assert fake_trello.add_comment.await_count == 1


def test_clarified_card_resubmitted_runs_again(campaign_client, fake_trello, db_sessionmaker):
    _seed_card_event(db_sessionmaker, "act-first")
    _seed_card_event(db_sessionmaker, "act-second")

    with patch("cascade.agents.campaign.intake_agent", stub_ambiguous_intake_agent):
        first = campaign_client.post("/pubsub/card-events", json=_envelope("card-fix", "act-first"))
    with patch("cascade.agents.campaign.intake_agent", stub_intake_agent):
        second = campaign_client.post(
            "/pubsub/card-events", json=_envelope("card-fix", "act-second")
        )

    assert first.json()["status"] == "needs_clarification"
    assert second.json()["status"] == "acknowledged"
    assert first.json()["campaign_id"] == second.json()["campaign_id"]
    assert fake_trello.add_comment.await_count == 2
    assert fake_trello.move_card.await_args_list[0].args[1] == "list-needs-attention"
    assert fake_trello.move_card.await_args_list[1].args[1] == "list-in-progress"
