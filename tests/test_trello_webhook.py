import asyncio
import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from cascade.config import Settings, get_settings
from cascade.db import get_db
from cascade.main import app
from cascade.models import Event


def _sign(raw_body: bytes, callback_url: str, secret: str) -> str:
    content = raw_body + callback_url.encode()
    return base64.b64encode(hmac.new(secret.encode(), content, hashlib.sha1).digest()).decode()


def _payload(action_id: str, action_type: str, list_after_id: str | None) -> dict:
    data = {"card": {"id": "card-abc", "name": "Screen 40 compounds"}}
    if list_after_id is not None:
        data["listBefore"] = {"id": "some-other-list"}
        data["listAfter"] = {"id": list_after_id}
    return {"action": {"id": action_id, "type": action_type, "data": data}}


def _create_card_payload(action_id: str, list_id: str) -> dict:
    return {
        "action": {
            "id": action_id,
            "type": "createCard",
            "data": {
                "card": {"id": "card-abc", "name": "Screen 40 compounds"},
                "list": {"id": list_id},
            },
        }
    }


def _post_signed(client: TestClient, settings: Settings, body: bytes):
    signature = _sign(body, settings.trello_callback_url, settings.trello_api_secret)
    return client.post("/webhooks/trello", content=body, headers={"x-trello-webhook": signature})


def _event_count() -> int:
    async def _count(sessionmaker):
        async with sessionmaker() as session:
            result = await session.execute(select(func.count()).select_from(Event))
            return result.scalar_one()

    from conftest import _test_sessionmaker

    return asyncio.run(_count(_test_sessionmaker))


@pytest.fixture
def client(settings_override, db_session_override):
    app.dependency_overrides[get_settings] = lambda: settings_override
    app.dependency_overrides[get_db] = db_session_override
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_head_returns_200(client):
    response = client.head("/webhooks/trello")
    assert response.status_code == 200


def test_loop_suppression_ignores_own_agent_traffic(client):
    response = client.post(
        "/webhooks/trello",
        headers={"X-Trello-Client-Identifier": "cascade-agent"},
        content=b"not even valid json",
    )
    assert response.status_code == 200
    assert _event_count() == 0


def test_missing_signature_returns_401(client, settings_override):
    body = json.dumps(
        _payload("action-1", "updateCard", settings_override.trello_list_todo)
    ).encode()
    response = client.post("/webhooks/trello", content=body)
    assert response.status_code == 401


def test_invalid_signature_returns_401(client, settings_override):
    body = json.dumps(
        _payload("action-2", "updateCard", settings_override.trello_list_todo)
    ).encode()
    response = client.post(
        "/webhooks/trello",
        content=body,
        headers={"x-trello-webhook": "not-a-real-signature"},
    )
    assert response.status_code == 401


def test_wrong_action_type_is_noop(client, settings_override):
    body = json.dumps(
        _payload("action-3", "commentCard", settings_override.trello_list_todo)
    ).encode()
    response = _post_signed(client, settings_override, body)
    assert response.status_code == 200
    assert _event_count() == 0


def test_wrong_target_list_is_noop(client, settings_override):
    body = json.dumps(
        _payload("action-4", "updateCard", settings_override.trello_list_in_progress)
    ).encode()
    response = _post_signed(client, settings_override, body)
    assert response.status_code == 200
    assert _event_count() == 0


def test_card_moved_to_todo_persists_event(client, settings_override, capsys):
    body = json.dumps(
        _payload("action-5", "updateCard", settings_override.trello_list_todo)
    ).encode()
    response = _post_signed(client, settings_override, body)
    assert response.status_code == 200
    assert _event_count() == 1

    captured = capsys.readouterr()
    assert "card_id=card-abc" in captured.out
    assert "action_id=action-5" in captured.out


def test_card_created_in_todo_persists_event(client, settings_override, capsys):
    body = json.dumps(_create_card_payload("action-7", settings_override.trello_list_todo)).encode()
    response = _post_signed(client, settings_override, body)
    assert response.status_code == 200
    assert _event_count() == 1

    captured = capsys.readouterr()
    assert "card_id=card-abc" in captured.out
    assert "action_id=action-7" in captured.out


def test_card_created_in_other_list_is_noop(client, settings_override):
    body = json.dumps(
        _create_card_payload("action-8", settings_override.trello_list_in_progress)
    ).encode()
    response = _post_signed(client, settings_override, body)
    assert response.status_code == 200
    assert _event_count() == 0


def test_duplicate_action_id_is_not_reprocessed(client, settings_override):
    body = json.dumps(
        _payload("action-6", "updateCard", settings_override.trello_list_todo)
    ).encode()

    first = _post_signed(client, settings_override, body)
    second = _post_signed(client, settings_override, body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert _event_count() == 1
