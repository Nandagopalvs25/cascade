variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "Region for Cloud Run, Cloud SQL, Artifact Registry"
  default     = "us-central1"
}

variable "gpu_region" {
  type        = string
  description = "Region holding the approved Cloud Run NVIDIA L4 quota. GPU workloads deploy here."
  default     = "europe-west1"
}

variable "gpu_accelerator_type" {
  type        = string
  description = "Cloud Run accelerator attached to GPU workloads."
  default     = "nvidia-l4"
}

variable "image" {
  type        = string
  description = "Container image URI for Cloud Run."
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "trello_api_key" {
  type      = string
  sensitive = true
}

variable "trello_api_token" {
  type      = string
  sensitive = true
}

variable "trello_api_secret" {
  type      = string
  sensitive = true
}

variable "trello_board_id" {
  type = string
}

variable "trello_list_todo" {
  type = string
}

variable "trello_list_in_progress" {
  type = string
}

variable "trello_list_recommended" {
  type = string
}

variable "trello_list_needs_attention" {
  type = string
}

variable "trello_list_done" {
  type = string
}

variable "dock_image_digest" {
  type        = string
  description = "Digest of the docking workload image in the cascade Artifact Registry repo."
  default     = "sha256:bc104b2e53959fdf79e5496a90528834ce335e76bc6331a8135d18a6b216c754"
}

variable "admet_image_digest" {
  type        = string
  description = "Digest of the ADMET workload image in the cascade Artifact Registry repo."
}

variable "md_stability_image_digest" {
  type        = string
  description = "Digest of the MD stability workload image in the cascade Artifact Registry repo."
}
