from google.cloud import run_v2

from cascade.clients.gcs import GCSClient, run_spec_path
from cascade.config import Settings
from cascade.schemas import GPU_ACCELERATED_WORKLOADS, JobSpec

SPEC_URI_ENVIRONMENT_VARIABLE = "SPEC_URI"
RUN_ID_ENVIRONMENT_VARIABLE = "RUN_ID"


def cloud_run_job_name_for_workload(workload: str) -> str:
    return f"cascade-{workload.replace('_', '-')}"


def cloud_run_region_for_workload(workload: str, settings: Settings) -> str:
    if workload in GPU_ACCELERATED_WORKLOADS:
        return settings.gcp_gpu_region
    return settings.gcp_region


def workload_job_resource_name(workload: str, settings: Settings) -> str:
    region = cloud_run_region_for_workload(workload, settings)
    return (
        f"projects/{settings.gcp_project_id}/locations/{region}"
        f"/jobs/{cloud_run_job_name_for_workload(workload)}"
    )


class CloudRunJobClient:
    def __init__(self):
        self._jobs_client: run_v2.JobsAsyncClient | None = None

    def _connected_jobs_client(self) -> run_v2.JobsAsyncClient:
        if self._jobs_client is None:
            self._jobs_client = run_v2.JobsAsyncClient()
        return self._jobs_client

    async def submit_workload_execution(
        self, spec: JobSpec, gcs: GCSClient, settings: Settings, attempt: int = 1
    ) -> str:
        spec_uri = await gcs.upload_json(
            run_spec_path(spec.run_id, attempt), spec.model_dump(exclude_none=True)
        )
        request = run_v2.RunJobRequest(
            name=workload_job_resource_name(spec.workload, settings),
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            run_v2.EnvVar(name=SPEC_URI_ENVIRONMENT_VARIABLE, value=spec_uri),
                            run_v2.EnvVar(name=RUN_ID_ENVIRONMENT_VARIABLE, value=spec.run_id),
                        ]
                    )
                ]
            ),
        )
        operation = await self._connected_jobs_client().run_job(request=request)
        return operation.metadata.name
