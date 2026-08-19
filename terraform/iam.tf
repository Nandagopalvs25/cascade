resource "google_project_iam_member" "cascade_run_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.cascade_run.email}"
}

resource "google_project_iam_member" "cascade_run_aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.cascade_run.email}"
}

resource "google_project_iam_member" "cascade_run_pubsub_publisher" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.cascade_run.email}"
}


resource "google_service_account_iam_member" "pubsub_can_mint_push_tokens" {
  service_account_id = google_service_account.cascade_pubsub_push.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-pubsub.iam.gserviceaccount.com"

  depends_on = [google_project_service.apis]
}
