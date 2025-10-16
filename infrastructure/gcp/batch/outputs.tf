output "batch_runner_service_account_email" {
  value       = google_service_account.batch_runner.email
  description = "Service account used by Cloud Batch VMs"
}

output "client_config_bucket" {
  value       = google_storage_bucket.client_config.name
  description = "GCS bucket storing client_config artifacts"
}

output "artifact_registry_repository" {
  value       = google_artifact_registry_repository.runner.id
  description = "Artifact Registry repository for runner images"
}

output "batch_defaults" {
  value       = local.batch_defaults
  description = "Default Cloud Batch execution settings for Playwright runner"
}

output "batch_job_template_name" {
  value       = var.batch_job_template_id
  description = "Identifier for the Cloud Batch job template (without resource path)"
}

output "batch_task_group_name" {
  value       = google_batch_job.form_sender_template.task_groups[0].name
  description = "Primary task group name defined in the Batch job template"
}
