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
