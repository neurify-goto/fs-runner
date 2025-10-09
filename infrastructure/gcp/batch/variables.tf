variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "Primary region for Cloud Batch resources"
}

variable "supabase_secret_names" {
  type        = list(string)
  description = "List of Secret Manager resource names required by the runner"
  default     = []
}

variable "gcs_bucket" {
  type        = string
  description = "Name of the GCS bucket for client_config objects"
}

variable "gcs_bucket_location" {
  type        = string
  description = "Location for the client_config bucket"
  default     = "asia-northeast1"
}

variable "artifact_repo" {
  type        = string
  description = "Artifact Registry repository ID for runner images"
}

variable "artifact_repo_location" {
  type        = string
  description = "Location for the Artifact Registry repository"
  default     = "asia-northeast1"
}

variable "prefer_spot_default" {
  type        = bool
  description = "Whether Spot instances are preferred by default"
  default     = true
}

variable "allow_on_demand_default" {
  type        = bool
  description = "Allow on-demand fallback when Spot capacity is unavailable"
  default     = true
}

variable "max_parallelism_default" {
  type        = number
  description = "Default Cloud Batch parallelism limit"
  default     = 100
}

variable "machine_type" {
  type        = string
  description = "Default machine type for Cloud Batch jobs"
  default     = "n2d-custom-4-10240"
}

variable "batch_service_account_id" {
  type        = string
  description = "Service account ID (without domain) for Cloud Batch VMs"
  default     = "form-sender-batch"
}

variable "batch_job_template_id" {
  type        = string
  description = "Identifier for the reusable Cloud Batch job template"
  default     = "form-sender-batch-template"
}

variable "batch_task_group_name" {
  type        = string
  description = "Name of the primary task group inside the Cloud Batch job template"
  default     = "form-sender-workers"
}

variable "batch_container_image" {
  type        = string
  description = "Container image used by Cloud Batch jobs (including registry path)"
}

variable "batch_container_entrypoint" {
  type        = string
  description = "Optional override entrypoint for the Cloud Batch container"
  default     = ""
}

variable "dispatcher_base_url" {
  type        = string
  description = "Base URL of the Cloud Run dispatcher for signed URL refresh"
}

variable "dispatcher_audience" {
  type        = string
  description = "OIDC audience value used when invoking the dispatcher from Batch"
}

variable "batch_template_env" {
  type        = map(string)
  description = "Static environment variables injected into the Batch job template"
  default     = {}
}

variable "batch_template_secret_env" {
  type        = map(string)
  description = "Environment variable -> Secret Manager resource mappings for Batch jobs"
  default     = {}
}

variable "batch_template_cpu_milli" {
  type        = number
  description = "Default CPU allocation (in milliCPU) for the Batch job template"
  default     = 4000
}

variable "batch_template_memory_mb" {
  type        = number
  description = "Default memory allocation (MiB) for the Batch job template"
  default     = 10240
}

variable "batch_max_retry_count" {
  type        = number
  description = "Maximum retry attempts for each Cloud Batch task"
  default     = 4
}

variable "batch_max_run_duration_seconds" {
  type        = number
  description = "Maximum run duration for a single Batch task in seconds"
  default     = 14400
}

variable "batch_logs_destination" {
  type        = string
  description = "Destination for Cloud Batch job logs"
  default     = "CLOUD_LOGGING"
}

variable "signed_url_ttl_hours" {
  type        = number
  description = "TTL (hours) for client_config signed URLs in Cloud Batch"
  default     = 48
}

variable "signed_url_refresh_threshold_seconds" {
  type        = number
  description = "Refresh threshold for signed URLs in Cloud Batch"
  default     = 21600
}
