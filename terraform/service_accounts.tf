resource "google_service_account" "cascade_run" {
  account_id   = "cascade-run"
  display_name = "Cascade Cloud Run runtime identity"

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "cascade_pubsub_push" {
  account_id   = "cascade-pubsub-push"
  display_name = "Cascade Pub/Sub push identity (OIDC)"

  depends_on = [google_project_service.apis]
}
