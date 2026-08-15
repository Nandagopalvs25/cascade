resource "random_password" "db" {
  length  = 24
  special = false
}

resource "google_sql_database_instance" "cascade" {
  name                = "cascade-db"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = false

  settings {
    tier    = "db-f1-micro"
    edition = "ENTERPRISE"

    backup_configuration {
      enabled = false
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "cascade" {
  name     = "cascade"
  instance = google_sql_database_instance.cascade.name
}

resource "google_sql_user" "cascade" {
  name     = "cascade"
  instance = google_sql_database_instance.cascade.name
  password = random_password.db.result
}
