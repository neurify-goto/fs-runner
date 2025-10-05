from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

import requests
from google.api_core import operations_v1
from google.api_core.client_options import ClientOptions
from google.api_core.operation import Operation
from google.cloud import run_v2, secretmanager, storage
from google.cloud.run_v2 import CancelExecutionRequest, RunJobMetadata

from .config import DispatcherSettings
from .schemas import FormSenderTask


class SignedUrlManager:
    def __init__(self, storage_client: storage.Client, settings: DispatcherSettings) -> None:
        self._storage = storage_client
        self._settings = settings

    def ensure_fresh(self, task: FormSenderTask) -> str:
        bucket, blob_name = task.gcs_blob_components()
        if self._settings.client_config_bucket and bucket != self._settings.client_config_bucket:
            raise ValueError("client_config_object bucket が設定と一致しません")

        signed_url = str(task.client_config_ref)
        self._validate_signed_url_origin(signed_url, bucket, blob_name)
        should_resign = False

        try:
            head_response = requests.head(signed_url, timeout=10)
            if head_response.status_code >= 400:
                should_resign = True
        except requests.RequestException as exc:  # pragma: no cover - network failure path
            should_resign = True

        if not should_resign and self._should_resign(signed_url):
            should_resign = True

        if should_resign:
            blob = self._storage.bucket(bucket).blob(blob_name)
            try:
                signed_url = blob.generate_signed_url(
                    expiration=timedelta(hours=self._settings.signed_url_ttl_hours),
                    method="GET",
                    version="v4",
                )
            except Exception as exc:
                raise ValueError("client_config_ref の再署名に失敗しました") from exc
        return signed_url

    def _should_resign(self, signed_url: str) -> bool:
        parsed = urlparse(signed_url)
        query = parse_qs(parsed.query)
        expires = int(query.get("X-Goog-Expires", ["0"])[0])
        if expires == 0:
            return False

        date_str = query.get("X-Goog-Date", [None])[0]
        if date_str:
            issued = datetime.strptime(date_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        else:
            issued = datetime.now(timezone.utc) - timedelta(seconds=expires)
        expiry = issued + timedelta(seconds=expires)
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        return remaining <= self._settings.signed_url_refresh_threshold_seconds

    def _validate_signed_url_origin(self, signed_url: str, bucket: str, blob_name: str) -> None:
        parsed = urlparse(signed_url)

        if parsed.scheme != "https":
            raise ValueError("client_config_ref must use https scheme")

        host = parsed.netloc.lower()
        if not host.endswith("storage.googleapis.com"):
            raise ValueError("client_config_ref must point to storage.googleapis.com")

        path = parsed.path.lstrip("/")
        if not path:
            raise ValueError("client_config_ref is missing object path")

        parts = path.split("/", 1)
        if len(parts) != 2:
            raise ValueError("client_config_ref path is invalid")

        bucket_from_url, object_from_url = parts
        if bucket_from_url != bucket or object_from_url != blob_name:
            raise ValueError("client_config_ref does not match client_config_object")

        query = parse_qs(parsed.query)
        algorithm = query.get("X-Goog-Algorithm", [None])[0]
        if not algorithm or algorithm.upper() != "GOOG4-RSA-SHA256":
            raise ValueError("client_config_ref must be a V4 signed URL")


class SecretManager:
    def __init__(self) -> None:
        self._client = secretmanager.SecretManagerServiceClient()

    def access(self, resource_name: str) -> str:
        response = self._client.access_secret_version(name=resource_name)
        return response.payload.data.decode("utf-8")


class CloudRunJobRunner:
    def __init__(self, settings: DispatcherSettings) -> None:
        client_options = ClientOptions(api_endpoint=f"{settings.location}-run.googleapis.com")
        self._client = run_v2.JobsClient(client_options=client_options)
        self._executions_client = run_v2.ExecutionsClient(client_options=client_options)
        self._operations_client = operations_v1.OperationsClient(client_options=client_options)
        self._job_path = f"projects/{settings.project_id}/locations/{settings.location}/jobs/{settings.job_name}"

    def run_job(
        self,
        task: FormSenderTask,
        env_vars: Dict[str, str],
        task_count: int,
        parallelism: int,
    ) -> Operation:
        overrides = run_v2.ExecutionTemplateOverrides(
            task_count=task_count,
            parallelism=parallelism,
            container_overrides=[
                run_v2.ContainerOverride(
                    env=[run_v2.EnvVar(name=k, value=v) for k, v in env_vars.items()]
                )
            ],
        )
        request = run_v2.RunJobRequest(name=self._job_path, overrides=overrides)
        operation = self._client.run_job(request=request)
        return operation

    def extract_execution_name(self, operation: Operation) -> Optional[str]:
        metadata = getattr(operation, "metadata", None)
        if metadata is not None and getattr(metadata, "name", None):
            return metadata.name  # type: ignore[return-value]
        try:
            return self._resolve_execution_name(operation.name)
        except Exception:  # pragma: no cover - best-effort fallback
            return None

    def cancel_execution(
        self,
        *,
        execution_name: Optional[str] = None,
        operation_name: Optional[str] = None,
    ) -> None:
        target = execution_name or (self._resolve_execution_name(operation_name) if operation_name else None)
        if target:
            request = CancelExecutionRequest(name=target)
            self._executions_client.cancel_execution(request=request)
            return
        if operation_name:
            self._operations_client.cancel_operation(name=operation_name)
            return
        raise ValueError("execution_name または operation_name が必要です")

    def _resolve_execution_name(self, operation_name: Optional[str]) -> Optional[str]:
        if not operation_name:
            return None
        op = self._operations_client.get_operation(name=operation_name)
        if not op.metadata:
            return None
        metadata = RunJobMetadata()
        unpacked = op.metadata.Unpack(metadata)  # type: ignore[attr-defined]
        if unpacked:
            return metadata.name
        return None
