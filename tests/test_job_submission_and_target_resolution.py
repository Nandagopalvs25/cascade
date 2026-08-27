import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from cascade.clients.jobs import CloudRunJobClient, workload_job_resource_name
from cascade.clients.structures import CampaignInputResolver
from cascade.schemas import JobSpec, TargetRequest, TargetStructure

STRUCTURE_BYTES = b"ATOM      1  N   PRO A   1      29.361  39.686   5.862\nEND\n"


class RecordingGCS:
    def __init__(self, bucket_name: str = "test-bucket"):
        self.bucket_name = bucket_name
        self.uploaded_json: dict[str, dict] = {}
        self.uploaded_bytes: dict[str, tuple[bytes, str]] = {}

    async def upload_json(self, path: str, data: dict) -> str:
        self.uploaded_json[path] = data
        return f"gs://{self.bucket_name}/{path}"

    async def upload_bytes(self, path: str, data: bytes, content_type: str) -> str:
        self.uploaded_bytes[path] = (data, content_type)
        return f"gs://{self.bucket_name}/{path}"


class StubTrello:
    def __init__(self, attachments: list[dict], content: bytes = STRUCTURE_BYTES):
        self._attachments = attachments
        self._content = content
        self.downloaded_urls: list[str] = []

    async def get_attachments(self, card_id: str) -> list[dict]:
        return self._attachments

    async def download_attachment(self, url: str) -> bytes:
        self.downloaded_urls.append(url)
        return self._content


def _job_spec(run_id: str = "run-1", workload: str = "dock") -> JobSpec:
    return JobSpec(
        run_id=run_id,
        workload=workload,
        target=TargetStructure(
            source="rcsb",
            reference="1HSG",
            structure_uri=f"gs://test-bucket/runs/{run_id}/inputs/structure.pdb",
            pdb_id="1HSG",
        ),
        ligands_uri=f"gs://test-bucket/runs/{run_id}/inputs/ligands.smi",
        output_uri=f"gs://test-bucket/runs/{run_id}/outputs",
        control_compound="indinavir",
    )


def _submit(spec: JobSpec, gcs: RecordingGCS, settings, attempt: int = 1):
    operation = MagicMock()
    operation.metadata.name = "projects/p/locations/us-central1/jobs/cascade-dock/executions/x1"
    jobs_client = MagicMock()
    jobs_client.run_job = AsyncMock(return_value=operation)

    with patch("cascade.clients.jobs.run_v2.JobsAsyncClient", return_value=jobs_client):
        execution = asyncio.run(
            CloudRunJobClient().submit_workload_execution(spec, gcs, settings, attempt=attempt)
        )
    return execution, jobs_client.run_job.await_args.kwargs["request"]


def _resolver(gcs: RecordingGCS, trello: StubTrello, handler) -> CampaignInputResolver:
    return CampaignInputResolver(gcs, trello, transport=httpx.MockTransport(handler))


def test_job_name_is_derived_from_the_workload(settings_override):
    assert workload_job_resource_name("dock", settings_override) == (
        "projects/test-project/locations/us-central1/jobs/cascade-dock"
    )
    assert workload_job_resource_name("admet", settings_override).endswith("/jobs/cascade-admet")


def test_submission_overrides_carry_both_spec_uri_and_run_id(settings_override):
    gcs = RecordingGCS()
    execution, request = _submit(_job_spec(), gcs, settings_override)

    overrides = request.overrides.container_overrides[0]
    supplied = {variable.name: variable.value for variable in overrides.env}

    assert supplied == {
        "SPEC_URI": "gs://test-bucket/runs/run-1/spec.json",
        "RUN_ID": "run-1",
    }
    assert request.name.endswith("/jobs/cascade-dock")
    assert execution.endswith("/executions/x1")


def test_submitted_spec_is_archived_before_the_execution_starts(settings_override):
    gcs = RecordingGCS()
    _submit(_job_spec(), gcs, settings_override)

    archived = gcs.uploaded_json["runs/run-1/spec.json"]
    assert archived["workload"] == "dock"
    assert archived["control_compound"] == "indinavir"
    assert archived["target"]["structure_uri"].startswith("gs://")


def test_retry_archives_its_own_spec_instead_of_overwriting_the_first(settings_override):
    gcs = RecordingGCS()
    _submit(_job_spec(), gcs, settings_override, attempt=1)
    _, request = _submit(_job_spec(), gcs, settings_override, attempt=2)

    assert sorted(gcs.uploaded_json) == ["runs/run-1/spec.json", "runs/run-1/spec_attempt2.json"]
    overrides = request.overrides.container_overrides[0]
    supplied = {variable.name: variable.value for variable in overrides.env}
    assert supplied["SPEC_URI"] == "gs://test-bucket/runs/run-1/spec_attempt2.json"


def test_rcsb_target_is_fetched_uppercased_and_archived_to_the_run_inputs(settings_override):
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=STRUCTURE_BYTES)

    gcs = RecordingGCS()
    resolver = _resolver(gcs, StubTrello([]), handler)
    target = asyncio.run(
        resolver.resolve_target_structure(
            TargetRequest(run_id="run-1", card_id="card-1", source="rcsb", reference="1hsg")
        )
    )

    assert requested_urls == ["https://files.rcsb.org/download/1HSG.pdb"]
    assert target.structure_uri == "gs://test-bucket/runs/run-1/inputs/structure.pdb"
    assert target.pdb_id == "1HSG"
    assert gcs.uploaded_bytes["runs/run-1/inputs/structure.pdb"] == (
        STRUCTURE_BYTES,
        "chemical/x-pdb",
    )


def test_card_attachment_target_is_downloaded_through_trello_and_has_no_pdb_id(settings_override):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("attachment downloads must not go through plain http")

    gcs = RecordingGCS()
    trello = StubTrello([{"name": "target.pdb", "url": "https://trello.com/a/target.pdb"}])
    resolver = _resolver(gcs, trello, handler)
    target = asyncio.run(
        resolver.resolve_target_structure(
            TargetRequest(
                run_id="run-2", card_id="card-2", source="card_attachment", reference="target.pdb"
            )
        )
    )

    assert trello.downloaded_urls == ["https://trello.com/a/target.pdb"]
    assert target.pdb_id is None
    assert target.structure_uri == "gs://test-bucket/runs/run-2/inputs/structure.pdb"


def test_missing_named_attachment_is_an_explicit_failure(settings_override):
    resolver = _resolver(
        RecordingGCS(),
        StubTrello([{"name": "notes.txt", "url": "https://trello.com/a/notes.txt"}]),
        lambda request: httpx.Response(200, content=STRUCTURE_BYTES),
    )

    with pytest.raises(ValueError, match="no attachment named"):
        asyncio.run(
            resolver.resolve_target_structure(
                TargetRequest(
                    run_id="run-3",
                    card_id="card-3",
                    source="card_attachment",
                    reference="target.pdb",
                )
            )
        )


def test_non_http_target_url_is_refused_before_any_fetch(settings_override):
    resolver = _resolver(
        RecordingGCS(),
        StubTrello([]),
        lambda request: httpx.Response(200, content=STRUCTURE_BYTES),
    )

    with pytest.raises(ValueError, match="must be http"):
        asyncio.run(resolver.download_from_url("ftp://files.example.org/target.pdb"))


def test_empty_structure_is_rejected_rather_than_archived(settings_override):
    gcs = RecordingGCS()
    resolver = _resolver(gcs, StubTrello([]), lambda request: httpx.Response(200, content=b"\n"))

    with pytest.raises(ValueError, match="was empty"):
        asyncio.run(
            resolver.resolve_target_structure(
                TargetRequest(run_id="run-4", card_id="card-4", source="rcsb", reference="1HSG")
            )
        )
    assert gcs.uploaded_bytes == {}


def test_unreachable_target_url_surfaces_the_http_error(settings_override):
    resolver = _resolver(
        RecordingGCS(), StubTrello([]), lambda request: httpx.Response(404, content=b"")
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            resolver.resolve_target_structure(
                TargetRequest(
                    run_id="run-5",
                    card_id="card-5",
                    source="url",
                    reference="https://example.org/missing.pdb",
                )
            )
        )


@pytest.mark.parametrize(
    "line",
    [
        "Screen these against HIV protease (PDB 1HSG).",
        "Please dock the attached library.",
        "aspirin",
        "1HSG)",
        "- see attached spreadsheet",
    ],
)
def test_prose_lines_are_not_mistaken_for_compounds(line):
    from cascade.agents.compound_library import smiles_library_lines_from_text

    kept, skipped = smiles_library_lines_from_text(line)
    assert kept == []
    assert skipped == 1


@pytest.mark.parametrize(
    "smiles",
    [
        "CC(C)Cc1ccc(cc1)C(C)C(=O)O",
        "CC(=O)Oc1ccccc1C(=O)O",
        "CCO",
        "CC[C@H](C)N",
        "c1ccc2c(c1)[nH]c1ccccc12",
        "CN1CCC[C@H]1c1cccnc1",
        "OC(=O)CBr",
    ],
)
def test_real_smiles_survive_the_filter(smiles):
    from cascade.agents.compound_library import smiles_library_lines_from_text

    kept, skipped = smiles_library_lines_from_text(f"{smiles} compound")
    assert kept == [f"{smiles}\tcompound"]
    assert skipped == 0
