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
