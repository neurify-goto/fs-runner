output "dispatcher_service_account_email" {
  value       = google_service_account.dispatcher.email
  description = "Service account email used by the dispatcher Cloud Run service"
}

output "tasks_invoker_service_account_email" {
  value       = google_service_account.tasks_invoker.email
  description = "Service account email used by Cloud Tasks to invoke the dispatcher"
}

output "dispatcher_service_url" {
  value       = google_cloud_run_v2_service.dispatcher.uri
  description = "Public URL of the dispatcher Cloud Run service"
}

output "cloud_tasks_queue_name" {
  value       = google_cloud_tasks_queue.dispatcher.name
  description = "Full resource name of the Cloud Tasks queue"
}

output "cloud_tasks_queue_location" {
  value       = var.region
  description = "Region where the Cloud Tasks queue is deployed"
}
