locals {
  database_url = "postgresql+asyncpg://${google_sql_user.cascade.name}:${random_password.db.result}@/${google_sql_database.cascade.name}?host=/cloudsql/${google_sql_database_instance.cascade.connection_name}"

  secrets = {
    trello-api-key    = var.trello_api_key
    trello-api-token  = var.trello_api_token
    trello-api-secret = var.trello_api_secret
    database-url      = local.database_url
  }
}

resource "google_secret_manager_secret" "cascade" {
  for_each  = local.secrets
  secret_id = each.key

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "cascade" {
  for_each    = local.secrets
  secret      = google_secret_manager_secret.cascade[each.key].id
  secret_data = each.value
}

resource "google_secret_manager_secret_iam_member" "cascade_run_access" {
  for_each  = local.secrets
  secret_id = google_secret_manager_secret.cascade[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cascade_run.email}"
}
