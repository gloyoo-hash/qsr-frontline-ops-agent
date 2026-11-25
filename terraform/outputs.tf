output "cloud_run_service_url" {
  description = "HTTPS endpoint of the deployed ShiftPilot QSR Cloud Run service."
  value       = google_cloud_run_v2_service.shiftpilot_agent.uri
}

output "runtime_service_account_email" {
  description = "Email of the least-privilege IAM service account used by Cloud Run."
  value       = google_service_account.agent_runtime.email
}

output "secret_manager_secret_id" {
  description = "Secret Manager resource ID storing GOOGLE_API_KEY."
  value       = google_secret_manager_secret.google_api_key.id
}

output "firestore_database_name" {
  description = "Firestore Native database storing persistent QSR shift state."
  value       = google_firestore_database.shiftpilot_state.name
}
