terraform {
  required_version = ">= 1.7"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "project" {
  project_id = var.project_id
}

locals {
  service_name  = "cascade"
  cloud_run_url = "https://${local.service_name}-${data.google_project.project.number}.${var.region}.run.app"
  dock_image    = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.cascade.repository_id}/dock@${var.dock_image_digest}"
  admet_image   = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.cascade.repository_id}/admet@${var.admet_image_digest}"
}
