resource "google_pubsub_topic" "card_events" {
  name = "card-events"

  depends_on = [google_project_service.apis]
}

resource "google_pubsub_topic" "job_completions" {
  name = "job-completions"

  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "card_events_push" {
  name  = "card-events-push"
  topic = google_pubsub_topic.card_events.name

  ack_deadline_seconds = 60

  push_config {
    push_endpoint = "${local.cloud_run_url}/pubsub/card-events"

    oidc_token {
      service_account_email = google_service_account.cascade_pubsub_push.email
      audience              = "${local.cloud_run_url}/pubsub/card-events"
    }
  }

  depends_on = [
    google_cloud_run_v2_service.cascade,
    google_cloud_run_v2_service_iam_member.public_invoker,
  ]
}

resource "google_pubsub_subscription" "job_completions_push" {
  name  = "job-completions-push"
  topic = google_pubsub_topic.job_completions.name

  ack_deadline_seconds = 60

  push_config {
    push_endpoint = "${local.cloud_run_url}/pubsub/job-completions"

    oidc_token {
      service_account_email = google_service_account.cascade_pubsub_push.email
      audience              = "${local.cloud_run_url}/pubsub/job-completions"
    }
  }

  depends_on = [
    google_cloud_run_v2_service.cascade,
    google_cloud_run_v2_service_iam_member.public_invoker,
  ]
}
