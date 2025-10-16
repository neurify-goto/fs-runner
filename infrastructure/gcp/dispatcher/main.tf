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

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  dispatcher_sa_email   = "${var.service_account_id}@${var.project_id}.iam.gserviceaccount.com"
  tasks_sa_email        = "${var.cloud_tasks_service_account_id}@${var.project_id}.iam.gserviceaccount.com"
  cloud_tasks_service   = "service-${data.google_project.current.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

  secret_env = {
    DISPATCHER_SUPABASE_URL             = var.supabase_url_secret
    DISPATCHER_SUPABASE_SERVICE_ROLE_KEY = var.supabase_service_role_secret
  }

  optional_secret_env = {
    DISPATCHER_SUPABASE_URL_TEST             = var.supabase_url_test_secret
    DISPATCHER_SUPABASE_SERVICE_ROLE_KEY_TEST = var.supabase_service_role_test_secret
  }

  filtered_optional_secret_env = {
    for k, v in local.optional_secret_env : k => v if length(trim(v)) > 0
  }

  all_secret_env = merge(local.secret_env, local.filtered_optional_secret_env)

  base_env = {
    DISPATCHER_PROJECT_ID                       = var.project_id
    DISPATCHER_LOCATION                         = var.region
    FORM_SENDER_CLOUD_RUN_JOB                   = var.cloud_run_job_name
    FORM_SENDER_CLIENT_CONFIG_BUCKET            = var.client_config_bucket
    FORM_SENDER_SIGNED_URL_TTL_HOURS            = tostring(var.signed_url_ttl_hours)
    FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD    = tostring(var.signed_url_refresh_threshold_seconds)
    FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH      = tostring(var.signed_url_ttl_hours_batch)
    FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH = tostring(var.signed_url_refresh_threshold_batch)
    FORM_SENDER_DISPATCHER_BASE_URL             = var.dispatcher_base_url
    FORM_SENDER_DISPATCHER_AUDIENCE             = var.dispatcher_audience
    FORM_SENDER_BATCH_PROJECT_ID                = var.project_id
    FORM_SENDER_BATCH_LOCATION                  = var.region
    FORM_SENDER_BATCH_JOB_TEMPLATE              = var.batch_job_template_name
    FORM_SENDER_BATCH_TASK_GROUP                = var.batch_task_group_name
    FORM_SENDER_BATCH_SERVICE_ACCOUNT           = var.batch_service_account_email
    FORM_SENDER_BATCH_CONTAINER_IMAGE           = var.batch_container_image
    FORM_SENDER_BATCH_ENTRYPOINT                = var.batch_container_entrypoint
    FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT      = var.batch_machine_type_default
    FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT   = tostring(var.batch_vcpu_per_worker_default)
    FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT = tostring(var.batch_memory_per_worker_mb_default)
    FORM_SENDER_BATCH_SUPABASE_URL_SECRET       = var.supabase_url_secret
    FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_SECRET = var.supabase_service_role_secret
    FORM_SENDER_BATCH_SUPABASE_URL_TEST_SECRET  = var.supabase_url_test_secret
    FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_TEST_SECRET = var.supabase_service_role_test_secret
  }

  merged_env = merge(local.base_env, var.extra_env)

  invoker_members = concat([
    "serviceAccount:${local.tasks_sa_email}",
    "serviceAccount:${var.batch_service_account_email}"
  ], var.additional_invokers)
}

resource "google_project_service" "dispatcher_services" {
  for_each = toset([
    "run.googleapis.com",
    "cloudtasks.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com"
  ])

  project = var.project_id
  service = each.value

  disable_on_destroy = false
}

resource "google_service_account" "dispatcher" {
  account_id   = var.service_account_id
  display_name = "Form Sender Dispatcher"
}

resource "google_service_account" "tasks_invoker" {
  account_id   = var.cloud_tasks_service_account_id
  display_name = "Form Sender Dispatcher Cloud Tasks Invoker"
}

resource "google_project_iam_member" "dispatcher_secret_access" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${local.dispatcher_sa_email}"
}

resource "google_service_account_iam_member" "tasks_token_creator" {
  service_account_id = google_service_account.tasks_invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${local.cloud_tasks_service}"
}

resource "google_cloud_tasks_queue" "dispatcher" {
  name     = "projects/${var.project_id}/locations/${var.region}/queues/${var.cloud_tasks_queue_id}"
  rate_limits {
    max_dispatches_per_second = var.cloud_tasks_max_dispatches_per_second
    max_concurrent_dispatches = var.cloud_tasks_max_concurrent_dispatches
  }
  retry_config {
    max_attempts = var.cloud_tasks_max_attempts
  }
  stackdriver_logging_config {
    sampling_ratio = 1.0
  }
  depends_on = [google_project_service.dispatcher_services]
}

resource "google_cloud_run_v2_service" "dispatcher" {
  name     = var.service_name
  location = var.region

  template {
    service_account = google_service_account.dispatcher.email
    timeout         = "${var.timeout_seconds}s"
    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }
    containers {
      image = var.container_image
      resources {
        limits = {
          cpu    = var.cpu_limit
          memory = var.memory_limit
        }
      }

      dynamic "env" {
        for_each = local.merged_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.all_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }

  ingress = var.ingress

  depends_on = [
    google_project_service.dispatcher_services,
    google_service_account.dispatcher
  ]

  lifecycle {
    ignore_changes = [
      template[0].containers[0].image
    ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = toset(local.invoker_members)
  name     = google_cloud_run_v2_service.dispatcher.name
  location = var.region
  role     = "roles/run.invoker"
  member   = each.value
}
