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

resource "google_service_account" "cascade_admet" {
  account_id   = "cascade-admet"
  display_name = "Cascade ADMET workload identity"

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "cascade_admet_object_admin" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cascade_admet.email}"
}

resource "google_pubsub_topic_iam_member" "cascade_admet_completion_publisher" {
  topic  = google_pubsub_topic.job_completions.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.cascade_admet.email}"
}

resource "google_service_account_iam_member" "cascade_run_can_act_as_admet" {
  service_account_id = google_service_account.cascade_admet.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cascade_run.email}"
}

resource "google_cloud_run_v2_job" "admet" {
  name     = "cascade-admet"
  location = var.region

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.cascade_admet.email
      max_retries     = 0
      timeout         = "1800s"

      containers {
        image = local.admet_image

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
            cpu    = "2"
            memory = "4Gi"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_storage_bucket_iam_member.cascade_admet_object_admin,
    google_pubsub_topic_iam_member.cascade_admet_completion_publisher,
  ]
}

resource "google_service_account" "cascade_md_stability" {
  account_id   = "cascade-md-stability"
  display_name = "Cascade MD stability workload identity"

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "cascade_md_stability_object_admin" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.cascade_md_stability.email}"
}

resource "google_pubsub_topic_iam_member" "cascade_md_stability_completion_publisher" {
  topic  = google_pubsub_topic.job_completions.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.cascade_md_stability.email}"
}

resource "google_service_account_iam_member" "cascade_run_can_act_as_md_stability" {
  service_account_id = google_service_account.cascade_md_stability.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cascade_run.email}"
}

resource "google_cloud_run_v2_job" "md_stability" {
  name                = "cascade-md-stability"
  location            = var.gpu_region
  deletion_protection = false

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.cascade_md_stability.email
      max_retries     = 0
      timeout         = "3600s"

      gpu_zonal_redundancy_disabled = true

      node_selector {
        accelerator = var.gpu_accelerator_type
      }

      containers {
        image = local.md_stability_image

        env {
          name  = "GCP_PROJECT"
          value = var.project_id
        }
        env {
          name  = "PUBSUB_TOPIC"
          value = google_pubsub_topic.job_completions.name
        }
        env {
          name  = "OPENMM_CPU_THREADS"
          value = "8"
        }

        resources {
          limits = {
            cpu              = "8"
            memory           = "32Gi"
            "nvidia.com/gpu" = "1"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_storage_bucket_iam_member.cascade_md_stability_object_admin,
    google_pubsub_topic_iam_member.cascade_md_stability_completion_publisher,
  ]
}
