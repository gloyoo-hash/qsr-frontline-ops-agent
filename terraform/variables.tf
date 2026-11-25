variable "project_id" {
  description = "Google Cloud Project ID hosting ShiftPilot QSR Frontline Operations Agent."
  type        = string
}

variable "region" {
  description = "Primary GCP region for Cloud Run and Firestore."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "qsr-frontline-ops-agent"
}

variable "container_image" {
  description = "Artifact Registry container image URI for the ShiftPilot QSR agent."
  type        = string
  default     = "us-central1-docker.pkg.dev/PROJECT_ID/shiftpilot/qsr-frontline-ops-agent:latest"
}

variable "min_instances" {
  description = "Minimum warm Cloud Run instances to avoid cold starts during peak QSR rush."
  type        = number
  default     = 1
}

variable "max_instances" {
  description = "Maximum autoscaled Cloud Run instances."
  type        = number
  default     = 20
}
