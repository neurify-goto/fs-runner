variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "Region where Cloud Run and Cloud Tasks are deployed"
}

variable "service_name" {
  type        = string
  description = "Cloud Run service name for the dispatcher"
  default     = "form-sender-dispatcher"
}

variable "container_image" {
  type        = string
  description = "Container image URI for the dispatcher service"
}

variable "service_account_id" {
  type        = string
  description = "Dispatcher Cloud Run service account ID (without domain)"
  default     = "form-sender-dispatcher"
}

variable "cloud_tasks_service_account_id" {
  type        = string
  description = "Service account ID used by Cloud Tasks when invoking the dispatcher"
  default     = "form-sender-dispatcher-invoker"
}

variable "client_config_bucket" {
  type        = string
  description = "GCS bucket containing client_config payloads"
}

variable "cloud_run_job_name" {
  type        = string
  description = "Cloud Run job name used for legacy serverless execution"
}

variable "dispatcher_base_url" {
  type        = string
  description = "Base URL of the dispatcher service (used by runners)"
}

variable "dispatcher_audience" {
  type        = string
  description = "OIDC audience used for signed URL refresh calls"
}

variable "supabase_url_secret" {
  type        = string
  description = "Secret Manager secret ID for the Supabase URL"
}

variable "supabase_service_role_secret" {
  type        = string
  description = "Secret Manager secret ID for the Supabase service role key"
}

variable "supabase_url_test_secret" {
  type        = string
  description = "Secret Manager secret ID for the Supabase URL (test environment)"
  default     = ""
}

variable "supabase_service_role_test_secret" {
  type        = string
  description = "Secret Manager secret ID for the Supabase service role key (test environment)"
  default     = ""
}

variable "batch_job_template_name" {
  type        = string
  description = "Cloud Batch job template resource name"
}

variable "batch_task_group_name" {
  type        = string
  description = "Cloud Batch task group name"
}

variable "batch_service_account_email" {
  type        = string
  description = "Service account used by Cloud Batch VMs"
}

variable "batch_network" {
  type        = string
  description = "VPC network self link used by Cloud Batch workers"
}

variable "batch_subnetwork" {
  type        = string
  description = "Subnetwork self link used by Cloud Batch workers"
}

variable "batch_no_external_ip" {
  type        = bool
  description = "Whether Batch workers should omit external IP addresses"
  default     = true
}

variable "batch_container_image" {
  type        = string
  description = "Default Batch runner container image"
}

variable "batch_container_entrypoint" {
  type        = string
  description = "Default Batch runner container entrypoint"
  default     = ""
}

variable "batch_machine_type_default" {
  type        = string
  description = "Default Batch machine type override"
  default     = "e2-standard-2"
}

variable "batch_vcpu_per_worker_default" {
  type        = number
  description = "Default vCPU allocation per worker"
  default     = 1
}

variable "batch_memory_per_worker_mb_default" {
  type        = number
  description = "Default memory allocation (MiB) per worker"
  default     = 2048
}

variable "signed_url_ttl_hours" {
  type        = number
  description = "Default TTL (hours) for Cloud Run signed URLs"
  default     = 15
}

variable "signed_url_refresh_threshold_seconds" {
  type        = number
  description = "Refresh threshold (seconds) for Cloud Run signed URLs"
  default     = 1800
}

variable "signed_url_ttl_hours_batch" {
  type        = number
  description = "Default TTL (hours) for Cloud Batch signed URLs"
  default     = 48
}

variable "signed_url_refresh_threshold_batch" {
  type        = number
  description = "Refresh threshold (seconds) for Cloud Batch signed URLs"
  default     = 21600
}

variable "cloud_tasks_queue_id" {
  type        = string
  description = "Identifier for the Cloud Tasks queue"
  default     = "form-sender-dispatcher"
}

variable "cloud_tasks_max_attempts" {
  type        = number
  description = "Maximum retry attempts for Cloud Tasks"
  default     = 3
}

variable "cloud_tasks_max_dispatches_per_second" {
  type        = number
  description = "Dispatch rate limit for Cloud Tasks"
  default     = 100
}

variable "cloud_tasks_max_concurrent_dispatches" {
  type        = number
  description = "Maximum concurrent task executions"
  default     = 10
}

variable "additional_invokers" {
  type        = list(string)
  description = "Additional principals granted Cloud Run invoker access"
  default     = []
}

variable "extra_env" {
  type        = map(string)
  description = "Additional static environment variables for the dispatcher"
  default     = {}
}

variable "min_instance_count" {
  type        = number
  description = "Minimum Cloud Run instances"
  default     = 0
}

variable "max_instance_count" {
  type        = number
  description = "Maximum Cloud Run instances"
  default     = 3
}

variable "timeout_seconds" {
  type        = number
  description = "Request timeout for the dispatcher service"
  default     = 900
}

variable "cpu_limit" {
  type        = string
  description = "CPU limit for dispatcher container"
  default     = "1"
}

variable "memory_limit" {
  type        = string
  description = "Memory limit for dispatcher container"
  default     = "1Gi"
}

variable "ingress" {
  type        = string
  description = "Cloud Run ingress policy"
  default     = "INGRESS_TRAFFIC_ALL"
}
