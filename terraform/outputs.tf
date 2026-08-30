output "cloud_run_url" {
  value = local.cloud_run_url
}

output "cloud_sql_connection_name" {
  value = google_sql_database_instance.cascade.connection_name
}

output "artifact_registry_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.cascade.repository_id}"
}

output "cascade_run_service_account" {
  value = google_service_account.cascade_run.email
}

output "cascade_pubsub_push_service_account" {
  value = google_service_account.cascade_pubsub_push.email
}

output "artifacts_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "dock_job_name" {
  value = google_cloud_run_v2_job.dock.name
}

output "cascade_dock_service_account" {
  value = google_service_account.cascade_dock.email
}

output "gpu_artifact_registry_repo" {
  value = "${var.gpu_region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.cascade_gpu.repository_id}"
}

output "md_stability_job_name" {
  value = google_cloud_run_v2_job.md_stability.name
}

output "md_stability_job_region" {
  value = google_cloud_run_v2_job.md_stability.location
}
