terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.2.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.2.0"
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
  bucket_name      = var.gcs_bucket
  artifact_repo_id = var.artifact_repo
  batch_sa_account = var.batch_service_account_id
  batch_env_defaults = {
    FORM_SENDER_ENV                 = "gcp_batch"
    FORM_SENDER_LOG_SANITIZE        = "1"
    FORM_SENDER_DISPATCHER_BASE_URL = var.dispatcher_base_url
    FORM_SENDER_DISPATCHER_AUDIENCE = var.dispatcher_audience
  }
  batch_environment_variables = merge(local.batch_env_defaults, var.batch_template_env)
}

resource "google_service_account" "batch_runner" {
  account_id   = local.batch_sa_account
  display_name = "Form Sender Cloud Batch Runner"
}

resource "google_compute_network" "batch" {
  name                    = var.batch_network_name
  project                 = var.project_id
  auto_create_subnetworks = false

  depends_on = [google_project_service.batch_services]
}

resource "google_compute_subnetwork" "batch" {
  name                     = var.batch_subnetwork_name
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.batch.id
  ip_cidr_range            = var.batch_subnetwork_cidr_range
  private_ip_google_access = true

  depends_on = [google_compute_network.batch]
}

resource "google_compute_router" "batch" {
  name    = var.batch_router_name
  project = var.project_id
  region  = var.region
  network = google_compute_network.batch.id

  depends_on = [google_compute_network.batch]
}

resource "google_compute_router_nat" "batch" {
  name                                = var.batch_nat_name
  project                             = var.project_id
  region                              = var.region
  router                              = google_compute_router.batch.name
  nat_ip_allocate_option              = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat  = "LIST_OF_SUBNETWORKS"
  min_ports_per_vm                    = var.batch_nat_min_ports_per_vm
  enable_endpoint_independent_mapping = var.batch_nat_enable_endpoint_independent_mapping

  subnetwork {
    name                    = google_compute_subnetwork.batch.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  dynamic "log_config" {
    for_each = var.batch_nat_log_type == "" ? [] : [var.batch_nat_log_type]
    content {
      enable = true
      filter = log_config.value
    }
  }

  depends_on = [google_compute_router.batch]
}

resource "google_project_service" "batch_services" {
  for_each = toset([
    "batch.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "compute.googleapis.com",
    "logging.googleapis.com",
    "storage.googleapis.com",
  ])

  project = var.project_id
  service = each.value

  disable_on_destroy = false
}

resource "google_project_iam_member" "batch_runner_secret_access" {
  for_each = toset(var.supabase_secret_names)
  project  = var.project_id
  role     = "roles/secretmanager.secretAccessor"
  member   = "serviceAccount:${google_service_account.batch_runner.email}"
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

resource "google_batch_job" "form_sender_template" {
  provider = google-beta

  project  = var.project_id
  location = var.region

  job_id = var.batch_job_template_id

  labels = {
    workload = "form-sender"
    template = var.batch_job_template_id
  }

  priority = 0

  task_groups {
    name        = var.batch_task_group_name
    task_count  = 1
    parallelism = 1
    task_spec {
      runnables {
        # Terraform apply 時に実行されるテンプレート検証ジョブは軽量スクリプトのみを
        # 実行し、本番ランナーのエントリポイントや環境変数には影響しない。
        script {
          text = <<-EOT
            #!/bin/bash
            echo "Form Sender Batch template validated: $(date -Is)"
            exit 0
          EOT
        }
      }

      environment {
        variables        = local.batch_environment_variables
        secret_variables = var.batch_template_secret_env
      }

      compute_resource {
        cpu_milli  = var.batch_template_cpu_milli
        memory_mib = var.batch_template_memory_mb
      }

      max_retry_count  = var.batch_max_retry_count
      max_run_duration = "${var.batch_max_run_duration_seconds}s"
    }
  }

  allocation_policy {
    instances {
      policy {
        machine_type       = var.machine_type
        provisioning_model = var.prefer_spot_default ? "SPOT" : "STANDARD"
      }

      network_interfaces {
        network                = google_compute_network.batch.id
        subnetwork             = google_compute_subnetwork.batch.id
        no_external_ip_address = true
      }
    }

    dynamic "instances" {
      for_each = var.prefer_spot_default && var.allow_on_demand_default ? ["on_demand_fallback"] : []
      content {
        policy {
          machine_type       = var.machine_type
          provisioning_model = "STANDARD"
        }

        network_interfaces {
          network                = google_compute_network.batch.id
          subnetwork             = google_compute_subnetwork.batch.id
          no_external_ip_address = true
        }
      }
    }

    service_account {
      email = google_service_account.batch_runner.email
    }
  }

  logs_policy {
    destination = var.batch_logs_destination
  }

  depends_on = [
    google_project_service.batch_services,
    google_service_account.batch_runner,
    google_compute_network.batch,
    google_compute_subnetwork.batch,
  ]
}

locals {
  batch_defaults = {
    prefer_spot                      = var.prefer_spot_default
    allow_on_demand_fallback         = var.allow_on_demand_default
    max_parallelism                  = var.max_parallelism_default
    machine_type                     = var.machine_type
    network                          = google_compute_network.batch.id
    subnetwork                       = google_compute_subnetwork.batch.id
    no_external_ip                   = true
    signed_url_ttl_hours             = var.signed_url_ttl_hours
    signed_url_refresh_threshold_sec = var.signed_url_refresh_threshold_seconds
    job_template_name                = google_batch_job.form_sender_template.name
    task_group_name                  = google_batch_job.form_sender_template.task_groups[0].name
  }
}
