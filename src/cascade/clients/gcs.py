import asyncio
import json
import threading
from datetime import timedelta

import google.auth
import google.auth.transport.requests
from google.cloud import storage

GCS_URI_SCHEME = "gs://"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
UNRESOLVED_SERVICE_ACCOUNT_EMAIL = "default"


def run_spec_path(run_id: str, attempt: int = 1) -> str:
    if attempt <= 1:
        return f"runs/{run_id}/spec.json"
    return f"runs/{run_id}/spec_attempt{attempt}.json"


def run_inputs_prefix(run_id: str) -> str:
    return f"runs/{run_id}/inputs"


def run_outputs_prefix(run_id: str, attempt: int = 1) -> str:
    if attempt <= 1:
        return f"runs/{run_id}/outputs"
    return f"runs/{run_id}/outputs_attempt{attempt}"


def run_reports_prefix(run_id: str) -> str:
    return f"runs/{run_id}/reports"


def run_figures_prefix(run_id: str) -> str:
    return f"runs/{run_id}/figures"


def split_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith(GCS_URI_SCHEME):
        raise ValueError(f"not a GCS URI: {uri}")
    bucket_name, _, object_path = uri.removeprefix(GCS_URI_SCHEME).partition("/")
    if not bucket_name or not object_path:
        raise ValueError(f"GCS URI missing bucket or object path: {uri}")
    return bucket_name, object_path


def authenticated_browser_url(uri: str) -> str:
    bucket_name, object_path = split_gcs_uri(uri)
    return f"https://storage.cloud.google.com/{bucket_name}/{object_path}"


def storage_console_url(uri: str) -> str:
    bucket_name, object_path = split_gcs_uri(uri)
    return f"https://console.cloud.google.com/storage/browser/{bucket_name}/{object_path}"


class GCSClient:
    def __init__(self, bucket_name: str):
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._signing_credentials = None
        self._signing_credentials_lock = threading.Lock()

    @property
    def bucket_name(self) -> str:
        return self._bucket.name

    def uri_for_path(self, path: str) -> str:
        return f"{GCS_URI_SCHEME}{self._bucket.name}/{path}"

    async def upload_json(self, path: str, data: dict) -> str:
        def upload_json_blob() -> str:
            blob = self._bucket.blob(path)
            blob.upload_from_string(json.dumps(data), content_type="application/json")
            return self.uri_for_path(path)

        return await asyncio.to_thread(upload_json_blob)

    async def upload_bytes(self, path: str, data: bytes, content_type: str) -> str:
        def upload_bytes_blob() -> str:
            blob = self._bucket.blob(path)
            blob.upload_from_string(data, content_type=content_type)
            return self.uri_for_path(path)

        return await asyncio.to_thread(upload_bytes_blob)

    async def download_json(self, path: str) -> dict:
        def download_json_blob() -> dict:
            blob = self._bucket.blob(path)
            return json.loads(blob.download_as_text())

        return await asyncio.to_thread(download_json_blob)

    async def download_json_from_uri(self, uri: str) -> dict:
        bucket_name, object_path = split_gcs_uri(uri)

        def download_json_blob_from_bucket() -> dict:
            blob = self._client.bucket(bucket_name).blob(object_path)
            return json.loads(blob.download_as_text())

        return await asyncio.to_thread(download_json_blob_from_bucket)

    async def download_text_from_uri(self, uri: str) -> str:
        bucket_name, object_path = split_gcs_uri(uri)

        def download_text_blob_from_bucket() -> str:
            blob = self._client.bucket(bucket_name).blob(object_path)
            return blob.download_as_text()

        return await asyncio.to_thread(download_text_blob_from_bucket)

    def _resolve_iam_signing_identity(self) -> tuple[str, str] | None:
        with self._signing_credentials_lock:
            if self._signing_credentials is None:
                self._signing_credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            credentials = self._signing_credentials
            if not credentials.valid:
                credentials.refresh(google.auth.transport.requests.Request())
            email = getattr(credentials, "service_account_email", None)
            if not email or email == UNRESOLVED_SERVICE_ACCOUNT_EMAIL:
                return None
            return email, credentials.token

    def _generate_signed_get_url_for_blob(
        self, blob, expiration_minutes: int, response_disposition: str | None
    ) -> str:
        signing_identity = self._resolve_iam_signing_identity()
        delegated_signing = (
            {
                "service_account_email": signing_identity[0],
                "access_token": signing_identity[1],
            }
            if signing_identity
            else {}
        )
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=expiration_minutes),
            method="GET",
            response_disposition=response_disposition,
            **delegated_signing,
        )

    async def generate_signed_url_for_uri(
        self, uri: str, expiration_minutes: int = 60, download_filename: str | None = None
    ) -> str:
        bucket_name, object_path = split_gcs_uri(uri)

        def sign_blob_url_in_bucket() -> str:
            blob = self._client.bucket(bucket_name).blob(object_path)
            response_disposition = (
                f'attachment; filename="{download_filename}"' if download_filename else None
            )
            return self._generate_signed_get_url_for_blob(
                blob, expiration_minutes, response_disposition
            )

        return await asyncio.to_thread(sign_blob_url_in_bucket)

    async def generate_signed_url(self, path: str, expiration_minutes: int = 60) -> str:
        def sign_blob_url() -> str:
            return self._generate_signed_get_url_for_blob(
                self._bucket.blob(path), expiration_minutes, None
            )

        return await asyncio.to_thread(sign_blob_url)
