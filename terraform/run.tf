resource "google_cloud_run_v2_service" "cascade" {
  name     = local.service_name
  location = var.region

  scaling {
    min_instance_count = 0
  }

  template {
    service_account = google_service_account.cascade_run.email

    containers {
      image = var.image

      env {
        name  = "CASCADE_GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "CASCADE_GCP_REGION"
        value = var.region
      }
      env {
        name  = "CASCADE_GCP_GPU_REGION"
        value = var.gpu_region
      }
      env {
        name  = "CASCADE_GCS_BUCKET"
        value = google_storage_bucket.artifacts.name
      }
      env {
        name  = "CASCADE_PUBSUB_PUSH_SERVICE_ACCOUNT"
        value = google_service_account.cascade_pubsub_push.email
      }
      env {
        name  = "CASCADE_PUBSUB_PUSH_AUDIENCE"
        value = local.cloud_run_url
      }
      env {
        name  = "CASCADE_TRELLO_BOARD_ID"
        value = var.trello_board_id
      }
      env {
        name  = "CASCADE_TRELLO_CALLBACK_URL"
        value = "${local.cloud_run_url}/webhooks/trello"
      }
      env {
        name  = "CASCADE_TRELLO_LIST_TODO"
        value = var.trello_list_todo
      }
      env {
        name  = "CASCADE_TRELLO_LIST_IN_PROGRESS"
        value = var.trello_list_in_progress
      }
      env {
        name  = "CASCADE_TRELLO_LIST_RECOMMENDED"
        value = var.trello_list_recommended
      }
      env {
        name  = "CASCADE_TRELLO_LIST_NEEDS_ATTENTION"
        value = var.trello_list_needs_attention
      }
      env {
        name  = "CASCADE_TRELLO_LIST_DONE"
        value = var.trello_list_done
      }

      env {
        name = "CASCADE_TRELLO_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.cascade["trello-api-key"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "CASCADE_TRELLO_API_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.cascade["trello-api-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "CASCADE_TRELLO_API_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.cascade["trello-api-secret"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "CASCADE_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.cascade["database-url"].secret_id
            version = "latest"
          }
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.cascade.connection_name]
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.cascade_run_access,
    google_project_iam_member.cascade_run_cloudsql_client,
    google_storage_bucket_iam_member.cascade_run_object_admin,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  name     = google_cloud_run_v2_service.cascade.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
