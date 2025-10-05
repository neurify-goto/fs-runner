from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DispatcherSettings:
    project_id: str
    location: str
    job_name: str
    supabase_url: str
    supabase_service_role_key: str
    default_client_config_path: str = "/tmp/client_config.json"
    signed_url_ttl_hours: int = 15
    signed_url_refresh_threshold_seconds: int = 1800
    client_config_bucket: Optional[str] = None
    git_token_secret: Optional[str] = None
    default_cpu_class: str = "standard"

    @classmethod
    def from_env(cls) -> "DispatcherSettings":
        def require(name: str) -> str:
            value = os.getenv(name)
            if not value:
                raise RuntimeError(f"環境変数 {name} が設定されていません")
            return value

        return cls(
            project_id=require("DISPATCHER_PROJECT_ID"),
            location=require("DISPATCHER_LOCATION"),
            job_name=require("FORM_SENDER_CLOUD_RUN_JOB"),
            supabase_url=require("DISPATCHER_SUPABASE_URL"),
            supabase_service_role_key=require("DISPATCHER_SUPABASE_SERVICE_ROLE_KEY"),
            default_client_config_path=os.getenv("FORM_SENDER_CLIENT_CONFIG_PATH", "/tmp/client_config.json"),
            signed_url_ttl_hours=int(os.getenv("FORM_SENDER_SIGNED_URL_TTL_HOURS", "15")),
            signed_url_refresh_threshold_seconds=int(os.getenv("FORM_SENDER_SIGNED_URL_REFRESH_THRESHOLD", "1800")),
            client_config_bucket=os.getenv("FORM_SENDER_CLIENT_CONFIG_BUCKET"),
            git_token_secret=os.getenv("FORM_SENDER_GIT_TOKEN_SECRET"),
            default_cpu_class=os.getenv("FORM_SENDER_CPU_CLASS_DEFAULT", "standard"),
        )


_cached_settings: Optional[DispatcherSettings] = None


def get_settings() -> DispatcherSettings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = DispatcherSettings.from_env()
    return _cached_settings
