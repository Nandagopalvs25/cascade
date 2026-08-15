from fastapi import HTTPException, Request
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token

from cascade.dependencies import SettingsDep

_google_request = google_auth_requests.Request()


def _bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


def verify_pubsub_oidc(request: Request, settings: SettingsDep) -> None:
    token = _bearer_token(request)
    audience = f"{settings.pubsub_push_audience}{request.url.path}"

    try:
        claims = id_token.verify_oauth2_token(token, _google_request, audience=audience)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid identity token") from exc

    expected_account = settings.pubsub_push_service_account
    if not claims.get("email_verified") or claims.get("email") != expected_account:
        raise HTTPException(status_code=401, detail="Unexpected token identity")
