resource "google_service_account" "cascade_dock" {
  account_id   = "cascade-dock"
  display_name = "Cascade docking workload identity"

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "cascade_dock_object_admin" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cascade_dock.email}"
}

resource "google_pubsub_topic_iam_member" "cascade_dock_completion_publisher" {
  topic  = google_pubsub_topic.job_completions.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.cascade_dock.email}"
}

resource "google_project_iam_member" "cascade_run_job_admin" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.cascade_run.email}"
}

resource "google_service_account_iam_member" "cascade_run_can_act_as_dock" {
  service_account_id = google_service_account.cascade_dock.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cascade_run.email}"
}

resource "google_cloud_run_v2_job" "dock" {
  name     = "cascade-dock"
  location = var.region

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.cascade_dock.email
      max_retries     = 0
      timeout         = "3600s"

      containers {
        image = local.dock_image

        env {
          name  = "GCP_PROJECT"
          value = var.project_id
        }
        env {
          name  = "PUBSUB_TOPIC"
          value = google_pubsub_topic.job_completions.name
        }

        resources {
          limits = {
            cpu    = "8"
            memory = "8Gi"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_storage_bucket_iam_member.cascade_dock_object_admin,
    google_pubsub_topic_iam_member.cascade_dock_completion_publisher,
  ]
}
