from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import requests
from google.api_core import operations_v1
from google.api_core.client_options import ClientOptions
from google.api_core.operation import Operation
from google.api_core import exceptions as gcloud_exceptions
from google.cloud import batch_v1, run_v2, secretmanager, storage
try:  # pragma: no cover - optional dependency during local tests
    from google.cloud.run_v2 import CancelExecutionRequest, RunJobMetadata
except ImportError:  # pragma: no cover - fallback when run_v2 extras are not available
    class CancelExecutionRequest:  # type: ignore[override]
        def __init__(self, name: str):
            self.name = name

    class RunJobMetadata:  # type: ignore[override]
        def __init__(self):
            self.name = ""

from .config import DispatcherSettings
from .schemas import FormSenderTask


logger = logging.getLogger(__name__)


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
        ttl_hours, refresh_threshold = self._resolve_signed_url_policy(task)
        should_resign = False

        try:
            head_response = requests.head(signed_url, timeout=10)
            if head_response.status_code >= 400:
                should_resign = True
        except requests.RequestException:  # pragma: no cover - network failure path
            should_resign = True

        if not should_resign and self._should_resign(signed_url, refresh_threshold):
            should_resign = True

        if should_resign:
            try:
                signed_url = self._generate_signed_url(bucket, blob_name, ttl_hours)
            except Exception as exc:
                raise ValueError("client_config_ref の再署名に失敗しました") from exc
        return signed_url

    def refresh_for_object(self, gcs_uri: str, *, ttl_hours: Optional[int] = None) -> str:
        bucket, blob_name = self._parse_gcs_uri(gcs_uri)
        if self._settings.client_config_bucket and bucket != self._settings.client_config_bucket:
            raise ValueError("client_config_object bucket が設定と一致しません")
        ttl = ttl_hours or self._settings.signed_url_ttl_hours_batch or self._settings.signed_url_ttl_hours
        return self._generate_signed_url(bucket, blob_name, ttl)

    def _generate_signed_url(self, bucket: str, blob_name: str, ttl_hours: int) -> str:
        blob = self._storage.bucket(bucket).blob(blob_name)
        return blob.generate_signed_url(
            expiration=timedelta(hours=max(1, ttl_hours)),
            method="GET",
            version="v4",
        )

    def _resolve_signed_url_policy(self, task: FormSenderTask) -> tuple[int, int]:
        if task.batch_enabled() and task.batch:
            ttl_hours = task.batch.signed_url_ttl_hours or self._settings.signed_url_ttl_hours_batch
            refresh_threshold = task.batch.signed_url_refresh_threshold_seconds or self._settings.signed_url_refresh_threshold_seconds_batch
        else:
            ttl_hours = self._settings.signed_url_ttl_hours
            refresh_threshold = self._settings.signed_url_refresh_threshold_seconds
        return max(1, ttl_hours), max(60, refresh_threshold)

    def _should_resign(self, signed_url: str, refresh_threshold_seconds: int) -> bool:
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
        return remaining <= refresh_threshold_seconds

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

    @staticmethod
    def _parse_gcs_uri(gcs_uri: str) -> tuple[str, str]:
        if not gcs_uri.startswith("gs://"):
            raise ValueError("client_config_object must be a gs:// URI")
        parsed = urlparse(gcs_uri)
        bucket = parsed.netloc
        blob_name = parsed.path.lstrip("/")
        if not bucket or not blob_name:
            raise ValueError("client_config_object is invalid")
        return bucket, blob_name


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
        if not hasattr(RunJobMetadata, "DESCRIPTOR"):
            return None

        metadata = RunJobMetadata()
        try:
            unpacked = op.metadata.Unpack(metadata)  # type: ignore[attr-defined]
        except TypeError:  # pragma: no cover - defensive for stub fallback
            return None

        if unpacked:
            return metadata.name
        return None


class CloudBatchJobRunner:
    def __init__(self, settings: DispatcherSettings) -> None:
        self._settings = settings
        self._settings.require_batch_configuration()
        self._client = batch_v1.BatchServiceClient()
        self._parent = f"projects/{self._settings.batch_project_id}/locations/{self._settings.batch_location}"

    def run_job(
        self,
        task: FormSenderTask,
        env_vars: Dict[str, str],
        task_count: int,
        parallelism: int,
    ) -> tuple[batch_v1.Job, Dict[str, Any]]:
        job_id = self._generate_job_id()
        (
            machine_type,
            cpu_milli,
            memory_mb,
            prefer_spot,
            allow_on_demand,
            resource_metadata,
        ) = self._calculate_resources(task)
        configured_task_group_id = self._sanitize_task_group_id(self._settings.batch_task_group)
        attempts, max_retry_count = self._resolve_attempts(task)

        secret_env = {
            name: resource
            for name, resource in self._settings.batch_secret_environment().items()
            if resource
        }
        environment = batch_v1.Environment(variables=env_vars)
        if secret_env:
            for name, resource in secret_env.items():
                environment.secret_variables[name] = resource
        container_spec = batch_v1.Runnable.Container(
            image=self._settings.batch_container_image,
        )
        if self._settings.batch_container_entrypoint:
            container_spec.entrypoint = self._settings.batch_container_entrypoint

        runnable = batch_v1.Runnable(container=container_spec)
        compute_resource = batch_v1.ComputeResource(cpu_milli=cpu_milli, memory_mib=memory_mb)
        task_spec = batch_v1.TaskSpec(
            runnables=[runnable],
            environment=environment,
            compute_resource=compute_resource,
        )
        task_spec.max_retry_count = max_retry_count

        effective_parallelism = max(1, min(parallelism, task_count))
        task_group = batch_v1.TaskGroup(
            task_spec=task_spec,
            task_count=task_count,
            parallelism=effective_parallelism,
        )
        if configured_task_group_id:
            task_group._pb.name = f"{self._parent}/jobs/{job_id}/taskGroups/{configured_task_group_id}"

        allocation_policy = self._build_allocation_policy(machine_type, prefer_spot, allow_on_demand)
        labels = {
            "workload": "form_sender",
            "targeting_id": str(task.targeting_id),
        }
        if self._settings.batch_job_template:
            labels["job_template"] = self._sanitize_job_prefix(self._settings.batch_job_template)
        if configured_task_group_id:
            labels["task_group"] = configured_task_group_id

        job = batch_v1.Job(
            task_groups=[task_group],
            allocation_policy=allocation_policy,
            labels=labels,
        )

        response = self._client.create_job(parent=self._parent, job=job, job_id=job_id)
        logger.info("Submitted Cloud Batch job", extra={"job_name": response.name})
        metadata = {
            "machine_type": machine_type,
            "cpu_milli": cpu_milli,
            "memory_mb": memory_mb,
            "prefer_spot": prefer_spot,
            "allow_on_demand": allow_on_demand,
            "parallelism": effective_parallelism,
            "array_size": task_count,
            "attempts": attempts,
            "max_retry_count": max_retry_count,
        }
        metadata.update(resource_metadata)
        if configured_task_group_id:
            metadata["configured_task_group_id"] = configured_task_group_id
            metadata["task_group_resource_hint"] = f"{self._parent}/jobs/{job_id}/taskGroups/{configured_task_group_id}"
        return response, metadata

    def delete_job(self, job_name: str) -> None:
        try:
            self._client.delete_job(name=job_name)
        except gcloud_exceptions.NotFound:
            logger.warning("Cloud Batch job already deleted", extra={"job_name": job_name})
        except gcloud_exceptions.PermissionDenied:
            logger.warning(
                "Cloud Batch job deletion returned permission error; treating as already finished",
                extra={"job_name": job_name},
            )
        except gcloud_exceptions.GoogleAPICallError as exc:
            status_code = getattr(exc, "code", None)
            if status_code == 403:
                logger.warning(
                    "Cloud Batch job deletion returned 403; treating as already finished",
                    extra={"job_name": job_name},
                )
                return
            logger.warning("Failed to delete Cloud Batch job: %s", exc)
            raise

    def _generate_job_id(self) -> str:
        prefix = self._sanitize_job_prefix(self._settings.batch_job_template)
        return f"{prefix}-{uuid4().hex[:16]}"

    @staticmethod
    def _sanitize_job_prefix(template: Optional[str]) -> str:
        value = template or "form-sender"
        value = value.strip()
        if not value:
            return "form-sender"

        if "/" in value:
            value = value.split("/")[-1]

        value = re.sub(r"[^a-z0-9-]", "-", value.lower())
        value = re.sub(r"-+", "-", value).strip("-")
        if not value or not re.match(r"^[a-z][a-z0-9-]*[a-z0-9]$", value):
            value = "form-sender"
        return value

    @staticmethod
    def _sanitize_task_group_id(task_group: Optional[str]) -> Optional[str]:
        if not task_group:
            return None
        value = task_group.strip().lower()
        if not value:
            return None
        value = re.sub(r"[^a-z0-9-]", "-", value)
        value = re.sub(r"-+", "-", value).strip("-")
        if not value:
            return None
        if not re.match(r"^[a-z][a-z0-9-]*[a-z0-9]$", value):
            value = f"group-{value}" if not value.startswith("group-") else value
            value = re.sub(r"[^a-z0-9-]", "-", value).strip("-")
        return value or None

    def _calculate_resources(self, task: FormSenderTask) -> tuple[str, int, int, bool, bool, Dict[str, Any]]:
        workers = max(1, task.execution.workers_per_workflow)
        batch_opts = task.batch if task.batch else None
        vcpu_per_worker = batch_opts.vcpu_per_worker if batch_opts and batch_opts.vcpu_per_worker else self._settings.batch_vcpu_per_worker_default
        memory_per_worker = batch_opts.memory_per_worker_mb if batch_opts and batch_opts.memory_per_worker_mb else self._settings.batch_memory_per_worker_mb_default
        if batch_opts and batch_opts.memory_buffer_mb is not None:
            buffer_mb = max(0, batch_opts.memory_buffer_mb)
        else:
            buffer_mb = max(0, self._settings.batch_memory_buffer_mb_default)

        vcpu = max(1, vcpu_per_worker) * workers
        total_memory = max(1024, workers * memory_per_worker + buffer_mb)
        memory_mb = int(math.ceil(total_memory / 256.0) * 256)
        required_memory = memory_mb
        requested_machine_type = batch_opts.machine_type if batch_opts and batch_opts.machine_type else None
        machine_type = requested_machine_type or self._settings.batch_machine_type_default
        if not machine_type:
            machine_type = f"n2d-custom-{vcpu}-{memory_mb}"

        prefer_spot = batch_opts.prefer_spot if batch_opts else True
        allow_on_demand = batch_opts.allow_on_demand_fallback if batch_opts else True

        metadata: Dict[str, Any] = {}

        parsed = self._parse_custom_machine_type(machine_type)
        needs_fallback = False
        if parsed:
            machine_vcpu, machine_memory = parsed
            if machine_vcpu < vcpu or machine_memory < memory_mb:
                needs_fallback = True

        if needs_fallback:
            fallback_memory = max(memory_mb, 10240)
            fallback_vcpu = max(vcpu, 4)
            fallback_type = f"n2d-custom-{fallback_vcpu}-{fallback_memory}"
            logger.warning(
                "Requested Batch machine_type '%s' insufficient for workers=%s (required_memory_mb=%s). "
                "Falling back to %s.",
                requested_machine_type or machine_type,
                workers,
                memory_mb,
                fallback_type,
            )
            metadata["memory_warning"] = True
            metadata["computed_memory_mb"] = required_memory
            if requested_machine_type:
                metadata["requested_machine_type"] = requested_machine_type
            machine_type = fallback_type
            memory_mb = fallback_memory
            vcpu = fallback_vcpu
        elif requested_machine_type:
            metadata["requested_machine_type"] = requested_machine_type

        metadata["resolved_machine_type"] = machine_type
        cpu_milli = vcpu * 1000
        metadata["memory_buffer_mb"] = buffer_mb

        recommended_min_memory = 8192
        if workers >= 4 and memory_mb < recommended_min_memory:
            logger.warning(
                "Computed Batch memory %sMB is below recommended minimum %sMB for workers=%s.",
                memory_mb,
                recommended_min_memory,
                workers,
            )
            metadata["memory_warning"] = True
            metadata["computed_memory_mb"] = memory_mb
            metadata["recommended_memory_mb"] = recommended_min_memory

        return machine_type, cpu_milli, memory_mb, prefer_spot, allow_on_demand, metadata

    def _resolve_attempts(self, task: FormSenderTask) -> tuple[int, int]:
        batch_opts = task.batch if task.batch else None
        attempts = self._settings.batch_max_attempts_default
        if batch_opts and batch_opts.max_attempts is not None:
            attempts = batch_opts.max_attempts

        attempts = max(1, attempts)
        max_retry_count = max(0, attempts - 1)
        return attempts, max_retry_count

    @staticmethod
    def _parse_custom_machine_type(machine_type: str) -> Optional[tuple[int, int]]:
        match = re.search(r"custom-(\d+)-(\d+)$", machine_type)
        if not match:
            return None
        try:
            return int(match.group(1)), int(match.group(2))
        except (TypeError, ValueError):
            return None

    def _build_allocation_policy(
        self, machine_type: str, prefer_spot: bool, allow_on_demand: bool
    ) -> batch_v1.AllocationPolicy:
        provisioning_model = (
            batch_v1.AllocationPolicy.ProvisioningModel.SPOT
            if prefer_spot
            else batch_v1.AllocationPolicy.ProvisioningModel.STANDARD
        )
        instances = [
            batch_v1.AllocationPolicy.InstancePolicyOrTemplate(
                policy=batch_v1.AllocationPolicy.InstancePolicy(
                    machine_type=machine_type,
                    provisioning_model=provisioning_model,
                )
            )
        ]

        if prefer_spot and allow_on_demand:
            instances.append(
                batch_v1.AllocationPolicy.InstancePolicyOrTemplate(
                    policy=batch_v1.AllocationPolicy.InstancePolicy(
                        machine_type=machine_type,
                        provisioning_model=batch_v1.AllocationPolicy.ProvisioningModel.STANDARD,
                    )
                )
            )

        service_account = None
        if self._settings.batch_service_account_email:
            service_account = batch_v1.ServiceAccount(
                email=self._settings.batch_service_account_email
            )

        return batch_v1.AllocationPolicy(
            instances=instances,
            service_account=service_account,
        )
