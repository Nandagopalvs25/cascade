import base64
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cascade.config import get_settings
from cascade.main import app


@pytest.fixture
def client(settings_override):
    app.dependency_overrides[get_settings] = lambda: settings_override
    yield TestClient(app)
    app.dependency_overrides.clear()


def _envelope(payload: dict) -> dict:
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return {"message": {"data": data, "messageId": "1"}}


def test_rejects_missing_token(client):
    response = client.post("/pubsub/card-events", json=_envelope({"card_id": "abc"}))
    assert response.status_code == 401


def test_rejects_wrong_service_account(client, settings_override):
    with patch("cascade.security.id_token.verify_oauth2_token") as mock_verify:
        mock_verify.return_value = {
            "email": "someone-else@another-project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        response = client.post(
            "/pubsub/card-events",
            json=_envelope({"card_id": "abc"}),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert response.status_code == 401


def test_accepts_valid_token(client, settings_override):
    with patch("cascade.security.id_token.verify_oauth2_token") as mock_verify:
        mock_verify.return_value = {
            "email": settings_override.pubsub_push_service_account,
            "email_verified": True,
        }
        response = client.post(
            "/pubsub/job-completions",
            json=_envelope({"run_id": "run-abc"}),
            headers={"Authorization": "Bearer fake-token"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "run_id": "run-abc"}
