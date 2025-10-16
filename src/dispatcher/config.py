from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class DispatcherSettings:
    project_id: str
    location: str
    job_name: str
    supabase_url: str
    supabase_service_role_key: str
    default_client_config_path: str = "/tmp/client_config_default.json"
    signed_url_ttl_hours: int = 15
    signed_url_refresh_threshold_seconds: int = 1800
    client_config_bucket: Optional[str] = None
    git_token_secret: Optional[str] = None
    default_cpu_class: str = "standard"
    dispatcher_base_url: str = ""
    dispatcher_audience: str = ""
    dispatcher_service_account_email: Optional[str] = None
    signed_url_ttl_hours_batch: int = 48
    signed_url_refresh_threshold_seconds_batch: int = 21600
    batch_project_id: Optional[str] = None
    batch_location: Optional[str] = None
    batch_job_template: Optional[str] = None
    batch_task_group: Optional[str] = None
    batch_service_account_email: Optional[str] = None
    batch_container_image: Optional[str] = None
    batch_container_entrypoint: Optional[str] = None
    batch_machine_type_default: Optional[str] = None
    batch_vcpu_per_worker_default: int = 1
    batch_memory_per_worker_mb_default: int = 2048
    batch_memory_buffer_mb_default: int = 2048
    batch_max_attempts_default: int = 1
    batch_supabase_url_secret: Optional[str] = None
    batch_supabase_service_role_secret: Optional[str] = None
    batch_supabase_url_test_secret: Optional[str] = None
    batch_supabase_service_role_test_secret: Optional[str] = None
    batch_monitor_interval_seconds: int = 60
    batch_monitor_timeout_seconds: int = 18000

    def require_batch_configuration(self) -> None:
        required_fields = {
            "dispatcher_base_url": "FORM_SENDER_DISPATCHER_BASE_URL (または FORM_SENDER_DISPATCHER_URL から導出)",
            "dispatcher_audience": "FORM_SENDER_DISPATCHER_AUDIENCE",
            "batch_project_id": "FORM_SENDER_BATCH_PROJECT_ID",
            "batch_location": "FORM_SENDER_BATCH_LOCATION",
            "batch_job_template": "FORM_SENDER_BATCH_JOB_TEMPLATE",
            "batch_task_group": "FORM_SENDER_BATCH_TASK_GROUP",
            "batch_service_account_email": "FORM_SENDER_BATCH_SERVICE_ACCOUNT",
            "batch_container_image": "FORM_SENDER_BATCH_CONTAINER_IMAGE",
            "batch_supabase_url_secret": "FORM_SENDER_BATCH_SUPABASE_URL_SECRET",
            "batch_supabase_service_role_secret": "FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_SECRET",
        }
        missing = [env for attr, env in required_fields.items() if not getattr(self, attr)]
        if missing:
            raise RuntimeError(
                "Batch execution requires the following environment variables: " + ", ".join(missing)
            )

    def batch_secret_ids(self) -> Dict[str, str]:
        secrets: Dict[str, str] = {}
        if self.batch_supabase_url_secret:
            secrets["SUPABASE_URL"] = self.batch_supabase_url_secret
        if self.batch_supabase_service_role_secret:
            secrets["SUPABASE_SERVICE_ROLE_KEY"] = self.batch_supabase_service_role_secret
        if self.batch_supabase_url_test_secret:
            secrets["SUPABASE_URL_TEST"] = self.batch_supabase_url_test_secret
        if self.batch_supabase_service_role_test_secret:
            secrets["SUPABASE_SERVICE_ROLE_KEY_TEST"] = self.batch_supabase_service_role_test_secret
        return secrets

    def batch_secret_environment(self) -> Dict[str, str]:
        project_id = self.batch_project_id or self.project_id
        if not project_id:
            return {}
        env_map: Dict[str, str] = {}
        for env_name, secret_id in self.batch_secret_ids().items():
            normalized = self._normalize_secret_resource(secret_id, project_id)
            if normalized:
                env_map[env_name] = normalized
        return env_map

    @staticmethod
    def _normalize_secret_resource(secret_id: str, project_id: str) -> Optional[str]:
        if not secret_id:
            return None

        secret_id = secret_id.strip()
        if not secret_id:
            return None

        if secret_id.startswith("projects/"):
            if "/versions/" in secret_id:
                return secret_id
            return secret_id.rstrip("/") + "/versions/latest"

        return f"projects/{project_id}/secrets/{secret_id}/versions/latest"

    @staticmethod
    def _derive_base_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        if not parsed.scheme or not parsed.netloc:
            return None
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base.rstrip('/')

    @classmethod
    def from_env(cls) -> "DispatcherSettings":
        def require(name: str) -> str:
            value = os.getenv(name)
            if not value:
                raise RuntimeError(f"環境変数 {name} が設定されていません")
            return value

        def get_int(name: str, default: int) -> int:
            value = os.getenv(name)
            if value in (None, ""):
                return default
            try:
                return int(value)
            except ValueError as exc:
                raise RuntimeError(f"環境変数 {name} は整数で指定してください") from exc

        dispatcher_base_url = os.getenv("FORM_SENDER_DISPATCHER_BASE_URL", "") or ""
        dispatcher_audience = os.getenv("FORM_SENDER_DISPATCHER_AUDIENCE", "") or ""
        dispatcher_url = os.getenv("FORM_SENDER_DISPATCHER_URL")

        if not dispatcher_base_url and dispatcher_url:
            derived_base = cls._derive_base_url(dispatcher_url)
            if derived_base:
                dispatcher_base_url = derived_base

        if not dispatcher_audience:
            dispatcher_audience = dispatcher_base_url or ""

        monitor_interval = get_int("FORM_SENDER_BATCH_MONITOR_INTERVAL_SECONDS", 60)
        monitor_timeout = get_int("FORM_SENDER_BATCH_MONITOR_TIMEOUT_SECONDS", 18000)

        return cls(
            project_id=require("DISPATCHER_PROJECT_ID"),
            location=require("DISPATCHER_LOCATION"),
            job_name=require("FORM_SENDER_CLOUD_RUN_JOB"),
            supabase_url=require("DISPATCHER_SUPABASE_URL"),
            supabase_service_role_key=require("DISPATCHER_SUPABASE_SERVICE_ROLE_KEY"),
            default_client_config_path=os.getenv(
                "FORM_SENDER_CLIENT_CONFIG_PATH", "/tmp/client_config_default.json"
            ),
            signed_url_ttl_hours=int(os.getenv("FORM_SENDER_SIGNED_URL_TTL_HOURS", "15")),
            signed_url_refresh_threshold_seconds=int(os.getenv("FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD", "1800")),
            client_config_bucket=os.getenv("FORM_SENDER_CLIENT_CONFIG_BUCKET"),
            git_token_secret=os.getenv("FORM_SENDER_GIT_TOKEN_SECRET"),
            default_cpu_class=os.getenv("FORM_SENDER_CPU_CLASS_DEFAULT", "standard"),
            dispatcher_base_url=dispatcher_base_url,
            dispatcher_audience=dispatcher_audience,
            dispatcher_service_account_email=os.getenv("FORM_SENDER_DISPATCHER_SERVICE_ACCOUNT"),
            signed_url_ttl_hours_batch=int(os.getenv("FORM_SENDER_SIGNED_URL_TTL_HOURS_BATCH", "48")),
            signed_url_refresh_threshold_seconds_batch=int(os.getenv("FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD_BATCH", "21600")),
            batch_project_id=os.getenv("FORM_SENDER_BATCH_PROJECT_ID") or os.getenv("DISPATCHER_PROJECT_ID"),
            batch_location=os.getenv("FORM_SENDER_BATCH_LOCATION") or os.getenv("DISPATCHER_LOCATION"),
            batch_job_template=os.getenv("FORM_SENDER_BATCH_JOB_TEMPLATE"),
            batch_task_group=os.getenv("FORM_SENDER_BATCH_TASK_GROUP"),
            batch_service_account_email=os.getenv("FORM_SENDER_BATCH_SERVICE_ACCOUNT"),
            batch_container_image=os.getenv("FORM_SENDER_BATCH_CONTAINER_IMAGE"),
            batch_container_entrypoint=os.getenv("FORM_SENDER_BATCH_ENTRYPOINT"),
            batch_machine_type_default=os.getenv("FORM_SENDER_BATCH_MACHINE_TYPE_DEFAULT"),
            batch_vcpu_per_worker_default=int(os.getenv("FORM_SENDER_BATCH_VCPU_PER_WORKER_DEFAULT", "1")),
            batch_memory_per_worker_mb_default=int(os.getenv("FORM_SENDER_BATCH_MEMORY_PER_WORKER_MB_DEFAULT", "2048")),
            batch_memory_buffer_mb_default=int(os.getenv("FORM_SENDER_BATCH_MEMORY_BUFFER_MB_DEFAULT", "2048")),
            batch_max_attempts_default=int(os.getenv("FORM_SENDER_BATCH_MAX_ATTEMPTS_DEFAULT", "1")),
            batch_supabase_url_secret=os.getenv("FORM_SENDER_BATCH_SUPABASE_URL_SECRET"),
            batch_supabase_service_role_secret=os.getenv("FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_SECRET"),
            batch_supabase_url_test_secret=os.getenv("FORM_SENDER_BATCH_SUPABASE_URL_TEST_SECRET"),
            batch_supabase_service_role_test_secret=os.getenv("FORM_SENDER_BATCH_SUPABASE_SERVICE_ROLE_TEST_SECRET"),
            batch_monitor_interval_seconds=monitor_interval,
            batch_monitor_timeout_seconds=monitor_timeout,
        )


_cached_settings: Optional[DispatcherSettings] = None


def get_settings() -> DispatcherSettings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = DispatcherSettings.from_env()
    return _cached_settings
