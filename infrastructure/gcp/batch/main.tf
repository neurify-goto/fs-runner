terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.17.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.17.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  bucket_name       = var.gcs_bucket
  artifact_repo_id  = var.artifact_repo
  batch_sa_account  = var.batch_service_account_id
}

resource "google_service_account" "batch_runner" {
  account_id   = local.batch_sa_account
  display_name = "Form Sender Cloud Batch Runner"
}

resource "google_project_iam_member" "batch_runner_secret_access" {
  for_each = toset(var.supabase_secret_names)
  project  = var.project_id
  role     = "roles/secretmanager.secretAccessor"
  member   = "serviceAccount:${google_service_account.batch_runner.email}"
  condition {
    title       = "form-sender-secret-access-${each.key}"
    description = "Allow Batch runner to read Supabase secrets"
    expression  = "true"
  }
}

resource "google_project_iam_member" "batch_runner_storage" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.batch_runner.email}"
}

resource "google_project_iam_member" "batch_runner_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.batch_runner.email}"
}

resource "google_storage_bucket" "client_config" {
  name                        = local.bucket_name
  project                     = var.project_id
  location                    = var.gcs_bucket_location
  uniform_bucket_level_access = true

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 7
    }
  }

  versioning {
    enabled = false
  }

  labels = {
    workload = "form-sender"
  }
}

resource "google_artifact_registry_repository" "runner" {
  project       = var.project_id
  location      = var.artifact_repo_location
  repository_id = local.artifact_repo_id
  description   = "Container images for form sender Cloud Batch runner"
  format        = "DOCKER"
  mode          = "STANDARD_REPOSITORY"
}

# TODO: Define google-beta_batch_job template once container command and volumes are finalized.
# Placeholder locals document the expected structure for downstream modules.
locals {
  batch_defaults = {
    prefer_spot                      = var.prefer_spot_default
    allow_on_demand_fallback         = var.allow_on_demand_default
    max_parallelism                  = var.max_parallelism_default
    machine_type                     = var.machine_type
    signed_url_ttl_hours             = var.signed_url_ttl_hours
    signed_url_refresh_threshold_sec = var.signed_url_refresh_threshold_seconds
  }
}

