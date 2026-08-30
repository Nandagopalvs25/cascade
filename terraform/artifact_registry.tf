resource "google_artifact_registry_repository" "cascade" {
  location      = var.region
  repository_id = "cascade"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "cascade_gpu" {
  location      = var.gpu_region
  repository_id = "cascade"
  format        = "DOCKER"

  depends_on = [google_project_service.apis]
}
