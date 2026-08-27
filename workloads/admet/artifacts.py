import json
import logging
import os
from pathlib import Path

from google.cloud import pubsub_v1, storage

GCS_URI_SCHEME = "gs://"
PUBLISH_TIMEOUT_SECONDS = 60
PROJECT_ENVIRONMENT_VARIABLE = "GCP_PROJECT"
TOPIC_ENVIRONMENT_VARIABLE = "PUBSUB_TOPIC"

LOGGER = logging.getLogger("cascade.artifacts")


class ArtifactStore:
    def __init__(self) -> None:
        self._storage_client: storage.Client | None = None

    def _client(self) -> storage.Client:
        if self._storage_client is None:
            self._storage_client = storage.Client()
        return self._storage_client

    @staticmethod
    def _split_gcs_uri(uri: str) -> tuple[str, str]:
        bucket_name, _, object_path = uri.removeprefix(GCS_URI_SCHEME).partition("/")
        if not bucket_name or not object_path:
            raise ValueError(f"GCS URI missing bucket or object path: {uri}")
        return bucket_name, object_path

    def read_text(self, uri: str) -> str:
        if uri.startswith(GCS_URI_SCHEME):
            bucket_name, object_path = self._split_gcs_uri(uri)
            return self._client().bucket(bucket_name).blob(object_path).download_as_text()
        return Path(uri).read_text()

    def download_to_file(self, uri: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if uri.startswith(GCS_URI_SCHEME):
            bucket_name, object_path = self._split_gcs_uri(uri)
            self._client().bucket(bucket_name).blob(object_path).download_to_filename(
                str(destination)
            )
        else:
            destination.write_bytes(Path(uri).read_bytes())
        return destination

    def upload_file(self, source: Path, uri: str) -> str:
        if uri.startswith(GCS_URI_SCHEME):
            bucket_name, object_path = self._split_gcs_uri(uri)
            self._client().bucket(bucket_name).blob(object_path).upload_from_filename(str(source))
        else:
            destination = Path(uri)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        return uri

    def upload_directory(self, directory: Path, prefix_uri: str) -> list[str]:
        prefix = prefix_uri.rstrip("/")
        uploaded: list[str] = []
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                uploaded.append(self.upload_file(path, f"{prefix}/{relative}"))
        return uploaded


def publish_job_completion(payload: dict) -> None:
    project_id = os.environ.get(PROJECT_ENVIRONMENT_VARIABLE)
    topic_name = os.environ.get(TOPIC_ENVIRONMENT_VARIABLE)
    if not project_id or not topic_name:
        LOGGER.warning(
            "skipping completion publish: %s and %s must both be set",
            PROJECT_ENVIRONMENT_VARIABLE,
            TOPIC_ENVIRONMENT_VARIABLE,
        )
        return
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(project_id, topic_name)
    future = publisher.publish(topic_path, json.dumps(payload).encode())
    future.result(timeout=PUBLISH_TIMEOUT_SECONDS)
    LOGGER.info("published completion for run %s to %s", payload.get("run_id"), topic_path)
