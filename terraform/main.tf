# =============================================================================
# ShiftPilot QSR Frontline Operations Agent - GCP Infrastructure as Code
# Provisions:
#   1. Required GCP APIs (Cloud Run, Secret Manager, Firestore, Cloud Trace, DLP)
#   2. Dedicated least-privilege IAM Service Account
#   3. Google Cloud Secret Manager secret for GOOGLE_API_KEY
#   4. Firestore Native database for persistent store & session state
#   5. Cloud Run v2 service with secret injection & OpenTelemetry env config
# =============================================================================

locals {
  required_apis = toset([
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "firestore.googleapis.com",
    "cloudtrace.googleapis.com",
    "dlp.googleapis.com",
    "iam.googleapis.com",
  ])
}

resource "google_project_service" "apis" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# -----------------------------------------------------------------------------
# 1. Dedicated Least-Privilege IAM Service Account
# -----------------------------------------------------------------------------

resource "google_service_account" "agent_runtime" {
  account_id   = "shiftpilot-agent-sa"
  display_name = "ShiftPilot QSR Agent Runtime Service Account"
  description  = "Least-privilege runtime identity for qsr-frontline-ops-agent on Cloud Run."
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.agent_runtime.email}"
}

resource "google_project_iam_member" "trace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.agent_runtime.email}"
}

resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.agent_runtime.email}"
}

resource "google_project_iam_member" "dlp_user" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_service_account.agent_runtime.email}"
}

# -----------------------------------------------------------------------------
# 2. Google Cloud Secret Manager (Zero Hardcoded Secrets)
# -----------------------------------------------------------------------------

resource "google_secret_manager_secret" "google_api_key" {
  secret_id = "shiftpilot-google-api-key"

  replication {
    auto {}
  }

  labels = {
    app = "qsr-frontline-ops-agent"
    env = "production"
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "agent_secret_accessor" {
  secret_id = google_secret_manager_secret.google_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}

# -----------------------------------------------------------------------------
# 3. Persistent Firestore Database (Store Memory & Session State)
# -----------------------------------------------------------------------------

resource "google_firestore_database" "shiftpilot_state" {
  project                           = var.project_id
  name                              = "(default)"
  location_id                       = var.region
  type                              = "FIRESTORE_NATIVE"
  concurrency_mode                  = "OPTIMISTIC"
  app_engine_integration_mode       = "DISABLED"
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"

  depends_on = [google_project_service.apis]
}

# -----------------------------------------------------------------------------
# 4. Cloud Run v2 Service
# -----------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "shiftpilot_agent" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account = google_service_account.agent_runtime.email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "1Gi"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "GCP_REGION"
        value = var.region
      }

      env {
        name  = "MEMORY_BACKEND"
        value = "firestore"
      }

      env {
        name  = "ENABLE_CLOUD_DLP"
        value = "true"
      }

      env {
        name  = "OTEL_SERVICE_NAME"
        value = var.service_name
      }

      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.google_api_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_firestore_database.shiftpilot_state,
    google_secret_manager_secret_iam_member.agent_secret_accessor,
  ]
}
