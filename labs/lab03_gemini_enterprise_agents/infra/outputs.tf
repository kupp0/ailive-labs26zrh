output "publisher_service_account" {
  description = "Identity the Lab 12 publisher console runs as"
  value       = google_service_account.publisher.email
}

output "discoveryengine_service_agent" {
  description = "Identity Gemini Enterprise uses to call the A2A agents on Cloud Run"
  value       = google_project_service_identity.discoveryengine.email
}
