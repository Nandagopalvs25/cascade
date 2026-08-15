variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "Region for Cloud Run, Cloud SQL, Artifact Registry"
  default     = "us-central1"
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
