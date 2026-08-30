import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from cascade.clients.gcs import (
    GCSClient,
    run_inputs_prefix,
    run_outputs_prefix,
    run_spec_path,
    split_gcs_uri,
)
from cascade.schemas import BindingSite, JobSpec, TargetStructure

RCSB_TARGET = TargetStructure(
    source="rcsb",
    reference="1HSG",
    pdb_id="1HSG",
    chain="A",
    structure_uri="gs://cascade-test/runs/run-1/inputs/target.pdb",
)


def test_job_spec_round_trips_through_json():
    spec = JobSpec(
        run_id="run-1",
        workload="dock",
        target=RCSB_TARGET,
        ligands_uri="gs://cascade-test/runs/run-1/inputs/ligands.sdf",
        binding_site=BindingSite(center_x=1.0, center_y=2.0, center_z=3.0),
        params={"exhaustiveness": 32},
        output_uri="gs://cascade-test/runs/run-1/outputs",
        control_compound="MK1",
    )

    restored = JobSpec.model_validate(json.loads(spec.model_dump_json()))

    assert restored == spec
    assert restored.target.pdb_id == "1HSG"
    assert restored.binding_site.size_x == 20.0
    assert restored.params["exhaustiveness"] == 32


def test_job_spec_defaults_leave_optional_fields_unset():
    spec = JobSpec(
        run_id="run-2",
        workload="admet",
        target=RCSB_TARGET,
        ligands_uri="gs://cascade-test/runs/run-2/inputs/ligands.sdf",
        output_uri="gs://cascade-test/runs/run-2/outputs",
    )

    assert spec.binding_site is None
    assert spec.control_compound is None
    assert spec.params == {}


def test_job_spec_rejects_unknown_workload():
    with pytest.raises(ValueError):
        JobSpec(
            run_id="run-3",
            workload="quantum_chemistry",
            target=RCSB_TARGET,
            ligands_uri="gs://cascade-test/in.sdf",
            output_uri="gs://cascade-test/out",
        )


def test_target_accepts_a_pdb_uploaded_as_a_card_attachment():
    target = TargetStructure(
        source="card_attachment",
        reference="my_protease_model.pdb",
        structure_uri="gs://cascade-test/runs/run-4/inputs/my_protease_model.pdb",
    )

    assert target.pdb_id is None
    assert target.reference == "my_protease_model.pdb"


def test_target_accepts_a_public_url():
    target = TargetStructure(
        source="url",
        reference="https://example.org/structures/model.pdb",
        structure_uri="gs://cascade-test/runs/run-5/inputs/model.pdb",
    )

    assert target.pdb_id is None
    assert target.source == "url"


def test_rcsb_target_normalizes_its_id_to_uppercase():
    target = TargetStructure(
        source="rcsb",
        reference="1hsg",
        pdb_id="1hsg",
        structure_uri="gs://cascade-test/runs/run-6/inputs/target.pdb",
    )

    assert target.pdb_id == "1HSG"


@pytest.mark.parametrize("bad_id", ["1HSG.pdb", "HSG", "1HSGX", "", "indinavir"])
def test_rcsb_target_rejects_malformed_ids(bad_id):
    with pytest.raises(ValueError):
        TargetStructure(
            source="rcsb",
            reference=bad_id,
            pdb_id=bad_id,
            structure_uri="gs://cascade-test/x.pdb",
        )


def test_rcsb_target_requires_a_pdb_id():
    with pytest.raises(ValueError, match="pdb_id is required"):
        TargetStructure(
            source="rcsb",
            reference="1HSG",
            structure_uri="gs://cascade-test/x.pdb",
        )


def test_non_rcsb_target_rejects_a_stray_pdb_id():
    with pytest.raises(ValueError, match="only meaningful when source is 'rcsb'"):
        TargetStructure(
            source="card_attachment",
            reference="model.pdb",
            pdb_id="1HSG",
            structure_uri="gs://cascade-test/x.pdb",
        )


@pytest.mark.parametrize("bad_reference", ["file:///etc/passwd", "ftp://host/x.pdb", "model.pdb"])
def test_url_target_rejects_non_http_references(bad_reference):
    with pytest.raises(ValueError, match="http"):
        TargetStructure(
            source="url",
            reference=bad_reference,
            structure_uri="gs://cascade-test/x.pdb",
        )


@pytest.mark.parametrize(
    "unresolved_uri",
    ["https://example.org/model.pdb", "/local/model.pdb", "model.pdb"],
)
def test_target_requires_the_structure_to_be_resolved_into_gcs(unresolved_uri):
    with pytest.raises(ValueError, match="resolved gs:// URI"):
        TargetStructure(
            source="card_attachment",
            reference="model.pdb",
            structure_uri=unresolved_uri,
        )


def test_run_paths_match_the_documented_bucket_layout():
    assert run_spec_path("run-1") == "runs/run-1/spec.json"
    assert run_inputs_prefix("run-1") == "runs/run-1/inputs"


def test_run_outputs_prefix_separates_retry_attempts():
    assert run_outputs_prefix("run-1") == "runs/run-1/outputs"
    assert run_outputs_prefix("run-1", attempt=1) == "runs/run-1/outputs"
    assert run_outputs_prefix("run-1", attempt=2) == "runs/run-1/outputs_attempt2"
    assert run_outputs_prefix("run-1", attempt=3) == "runs/run-1/outputs_attempt3"


def test_split_gcs_uri_separates_bucket_from_object_path():
    assert split_gcs_uri("gs://bucket/runs/run-1/spec.json") == ("bucket", "runs/run-1/spec.json")


@pytest.mark.parametrize(
    "uri",
    ["https://bucket/object", "gs://bucket", "gs://bucket/", "gs:///object", "bucket/object"],
)
def test_split_gcs_uri_rejects_malformed_uris(uri):
    with pytest.raises(ValueError):
        split_gcs_uri(uri)


def keyless_metadata_credentials(email="cascade-run@test-project.iam.gserviceaccount.com"):
    credentials = MagicMock(spec=["valid", "refresh", "token", "service_account_email"])
    credentials.valid = True
    credentials.token = "metadata-access-token"
    credentials.service_account_email = email
    return credentials


@pytest.fixture
def gcs_client_with_mock_bucket():
    with (
        patch("cascade.clients.gcs.storage.Client") as storage_client,
        patch("cascade.clients.gcs.google.auth.default") as auth_default,
    ):
        bucket = MagicMock()
        bucket.name = "cascade-test"
        storage_client.return_value.bucket.return_value = bucket
        auth_default.return_value = (keyless_metadata_credentials(), "test-project")
        yield GCSClient("cascade-test"), bucket


def test_upload_json_writes_json_and_returns_gs_uri(gcs_client_with_mock_bucket):
    client, bucket = gcs_client_with_mock_bucket

    uri = asyncio.run(client.upload_json("runs/run-1/spec.json", {"run_id": "run-1"}))

    assert uri == "gs://cascade-test/runs/run-1/spec.json"
    bucket.blob.assert_called_once_with("runs/run-1/spec.json")
    bucket.blob.return_value.upload_from_string.assert_called_once_with(
        '{"run_id": "run-1"}', content_type="application/json"
    )


def test_upload_bytes_passes_through_content_type(gcs_client_with_mock_bucket):
    client, bucket = gcs_client_with_mock_bucket

    uri = asyncio.run(client.upload_bytes("runs/run-1/figures/chart.png", b"\x89PNG", "image/png"))

    assert uri == "gs://cascade-test/runs/run-1/figures/chart.png"
    bucket.blob.return_value.upload_from_string.assert_called_once_with(
        b"\x89PNG", content_type="image/png"
    )


def test_download_json_from_uri_reads_the_bucket_named_in_the_uri(gcs_client_with_mock_bucket):
    client, _ = gcs_client_with_mock_bucket
    other_bucket = MagicMock()
    other_bucket.blob.return_value.download_as_text.return_value = '{"workload": "admet"}'
    client._client.bucket.return_value = other_bucket

    result = asyncio.run(client.download_json_from_uri("gs://other-bucket/runs/run-9/spec.json"))

    assert result == {"workload": "admet"}
    client._client.bucket.assert_called_with("other-bucket")
    other_bucket.blob.assert_called_once_with("runs/run-9/spec.json")


def test_download_text_from_uri_reads_the_bucket_named_in_the_uri(gcs_client_with_mock_bucket):
    client, _ = gcs_client_with_mock_bucket
    other_bucket = MagicMock()
    other_bucket.blob.return_value.download_as_text.return_value = "CC(C)Cc1ccc(cc1)\tibuprofen\n"
    client._client.bucket.return_value = other_bucket

    result = asyncio.run(
        client.download_text_from_uri("gs://other-bucket/runs/run-9/inputs/ligands.smi")
    )

    assert result == "CC(C)Cc1ccc(cc1)\tibuprofen\n"
    client._client.bucket.assert_called_with("other-bucket")
    other_bucket.blob.assert_called_once_with("runs/run-9/inputs/ligands.smi")


def test_signed_url_requests_a_v4_get_url(gcs_client_with_mock_bucket):
    client, bucket = gcs_client_with_mock_bucket
    bucket.blob.return_value.generate_signed_url.return_value = "https://signed.example/x"

    url = asyncio.run(
        client.generate_signed_url_for_uri("gs://cascade-test/runs/run-1/outputs/scores.csv", 15)
    )

    assert url == "https://signed.example/x"
    kwargs = bucket.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["version"] == "v4"
    assert kwargs["method"] == "GET"
    assert kwargs["expiration"].total_seconds() == 900
    assert kwargs["response_disposition"] is None


def test_signed_url_delegates_signing_to_the_iam_api_when_there_is_no_private_key(
    gcs_client_with_mock_bucket,
):
    client, bucket = gcs_client_with_mock_bucket
    bucket.blob.return_value.generate_signed_url.return_value = "https://signed.example/x"

    asyncio.run(
        client.generate_signed_url_for_uri("gs://cascade-test/runs/run-1/outputs/scores.csv", 15)
    )

    kwargs = bucket.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["service_account_email"] == "cascade-run@test-project.iam.gserviceaccount.com"
    assert kwargs["access_token"] == "metadata-access-token"


def test_signed_url_for_uri_forces_a_download_and_delegates_signing(
    gcs_client_with_mock_bucket,
):
    client, _ = gcs_client_with_mock_bucket
    archive_bucket = MagicMock()
    archive_bucket.blob.return_value.generate_signed_url.return_value = "https://signed.example/z"
    client._client.bucket.return_value = archive_bucket

    url = asyncio.run(
        client.generate_signed_url_for_uri(
            "gs://cascade-test/runs/run-1/outputs/results.zip",
            10080,
            download_filename="results.zip",
        )
    )

    assert url == "https://signed.example/z"
    kwargs = archive_bucket.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["response_disposition"] == 'attachment; filename="results.zip"'
    assert kwargs["service_account_email"] == "cascade-run@test-project.iam.gserviceaccount.com"
    assert kwargs["access_token"] == "metadata-access-token"
    assert kwargs["expiration"].total_seconds() == 604800


def test_signed_url_refreshes_an_expired_metadata_token_before_signing(
    gcs_client_with_mock_bucket,
):
    client, bucket = gcs_client_with_mock_bucket
    credentials = client._signing_credentials = keyless_metadata_credentials()
    credentials.valid = False

    asyncio.run(
        client.generate_signed_url_for_uri("gs://cascade-test/runs/run-1/outputs/scores.csv", 15)
    )

    credentials.refresh.assert_called_once()


def test_signed_url_falls_back_to_local_signing_for_a_non_service_account_identity(
    gcs_client_with_mock_bucket,
):
    client, bucket = gcs_client_with_mock_bucket
    user_credentials = MagicMock(spec=["valid", "refresh", "token"])
    user_credentials.valid = True
    client._signing_credentials = user_credentials

    asyncio.run(
        client.generate_signed_url_for_uri("gs://cascade-test/runs/run-1/outputs/scores.csv", 15)
    )

    kwargs = bucket.blob.return_value.generate_signed_url.call_args.kwargs
    assert "service_account_email" not in kwargs
    assert "access_token" not in kwargs
