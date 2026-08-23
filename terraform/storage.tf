resource "google_storage_bucket" "artifacts" {
  name     = "cascade-${var.project_id}"
  location = var.region

  uniform_bucket_level_access = true
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "cascade_run_object_admin" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cascade_run.email}"
}

resource "google_service_account_iam_member" "cascade_run_can_sign_as_itself" {
  service_account_id = google_service_account.cascade_run.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.cascade_run.email}"
}
