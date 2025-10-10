from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .config import DispatcherSettings, get_settings
from .gcp import CloudBatchJobRunner, CloudRunJobRunner, SecretManager, SignedUrlManager
from .schemas import FormSenderTask, SignedUrlRefreshRequest
from .supabase_client import JobExecutionRepository


logger = logging.getLogger(__name__)


class DispatcherService:
    def __init__(self, settings: DispatcherSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._supabase = JobExecutionRepository(
            url=self._settings.supabase_url,
            key=self._settings.supabase_service_role_key,
        )
        self._signed_url_manager = SignedUrlManager(storage_client=self._build_storage_client(), settings=self._settings)
        self._job_runner = CloudRunJobRunner(settings=self._settings)
        self._batch_runner: Optional[CloudBatchJobRunner] = None
        self._secret_manager = SecretManager() if self._settings.git_token_secret else None

    @staticmethod
    def _build_storage_client():
        from google.cloud import storage

        return storage.Client()

    def handle_form_sender_task(self, task: FormSenderTask) -> Dict[str, Any]:
        existing = self._supabase.find_active_execution(task.targeting_id, task.execution.run_index_base)
        if existing:
            return {
                "status": "duplicate",
                "job_execution_id": existing["execution_id"],
            }

        fallback_signed_url: Optional[str] = None
        if task.batch_enabled():
            try:
                fallback_signed_url = self._supabase.find_latest_signed_url(
                    targeting_id=task.targeting_id,
                    client_config_object=task.client_config_object,
                )
            except Exception as exc:  # pragma: no cover - best effort fallback
                logger.debug("Failed to lookup latest signed URL from Supabase: %s", exc)

        try:
            signed_url = self._signed_url_manager.ensure_fresh(task, override_url=fallback_signed_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        job_execution_id = task.execution_id or uuid.uuid4().hex
        task_data = task.dict()
        task_data["execution_id"] = job_execution_id
        task_data["client_config_ref"] = signed_url
        env_vars = self._build_env(task, job_execution_id, signed_url)

        execution_mode = "batch" if task.batch_enabled() else "cloud_run"

        record = self._supabase.insert_execution(
            job_execution_id=job_execution_id,
            payload=task_data,
            cloud_run_operation=None,
            cloud_run_execution=None,
            execution_mode=execution_mode,
        )

        try:
            if execution_mode == "batch":
                batch_runner = self._ensure_batch_runner()
                job, batch_meta = batch_runner.run_job(
                    task=task,
                    env_vars=env_vars,
                    task_count=task.execution.run_total,
                    parallelism=task.effective_parallelism(),
                )
                batch_metadata: Dict[str, Any] = {
                    "job_name": job.name,
                    "task_count": task.execution.run_total,
                    "parallelism": batch_meta.get("parallelism"),
                    "machine_type": batch_meta.get("machine_type"),
                    "cpu_milli": batch_meta.get("cpu_milli"),
                    "memory_mb": batch_meta.get("memory_mb"),
                    "memory_buffer_mb": batch_meta.get("memory_buffer_mb"),
                    "prefer_spot": batch_meta.get("prefer_spot"),
                    "allow_on_demand": batch_meta.get("allow_on_demand"),
                    "latest_signed_url": signed_url,
                }
                task_group_name = None
                if job.task_groups:
                    task_group_name = job.task_groups[0].name
                if not task_group_name:
                    task_group_name = batch_meta.get("task_group_resource_hint")
                if task_group_name:
                    batch_metadata["task_group"] = task_group_name
                configured_task_group_id = batch_meta.get("configured_task_group_id")
                if configured_task_group_id:
                    batch_metadata["configured_task_group"] = configured_task_group_id
                batch_metadata["array_size"] = batch_meta.get("array_size", task.execution.run_total)
                if batch_meta.get("attempts") is not None:
                    batch_metadata["attempts"] = batch_meta.get("attempts")
                if batch_meta.get("max_retry_count") is not None:
                    batch_metadata["max_retry_count"] = batch_meta.get("max_retry_count")
                if batch_meta.get("memory_warning"):
                    batch_metadata["memory_warning"] = True
                    if batch_meta.get("computed_memory_mb") is not None:
                        batch_metadata["computed_memory_mb"] = batch_meta.get("computed_memory_mb")
                if batch_meta.get("requested_machine_type"):
                    batch_metadata["requested_machine_type"] = batch_meta.get("requested_machine_type")
                if batch_meta.get("resolved_machine_type"):
                    batch_metadata["resolved_machine_type"] = batch_meta.get("resolved_machine_type")
                if task.batch and getattr(task.batch, "memory_warning", None):
                    batch_metadata["memory_warning"] = True
                    if getattr(task.batch, "computed_memory_mb", None):
                        batch_metadata["computed_memory_mb"] = task.batch.computed_memory_mb

                metadata_patch = {
                    "execution_mode": "batch",
                    "batch": batch_metadata,
                }
                record = self._supabase.update_metadata(job_execution_id, metadata_patch)
                return {
                    "status": "queued",
                    "job_execution_id": record["execution_id"],
                    "batch": batch_metadata,
                    "batch_job_name": job.name,
                }

            operation = self._job_runner.run_job(
                task=task,
                env_vars=env_vars,
                task_count=task.execution.run_total,
                parallelism=task.execution.parallelism,
            )
        except Exception:
            ended_at = datetime.now(timezone.utc).isoformat()
            self._supabase.update_status(job_execution_id, "failed", ended_at=ended_at)
            raise

        execution_name = self._job_runner.extract_execution_name(operation)
        cloud_run_metadata = {
            "operation": getattr(operation, "name", None),
            "execution": execution_name,
        }
        metadata_patch = {
            "execution_mode": "cloud_run",
            "cloud_run": cloud_run_metadata,
        }
        record = self._supabase.update_metadata(job_execution_id, metadata_patch)
        return {
            "status": "queued",
            "job_execution_id": record["execution_id"],
            "cloud_run": cloud_run_metadata,
            "cloud_run_operation": getattr(operation, "name", None),
        }

    def _build_env(self, task: FormSenderTask, job_execution_id: str, signed_url: str) -> Dict[str, str]:
        cpu_class = (task.cpu_class or self._settings.default_cpu_class or "standard").strip().lower()
        env_vars: Dict[str, str] = {
            "FORM_SENDER_CLIENT_CONFIG_URL": signed_url,
            "FORM_SENDER_CLIENT_CONFIG_OBJECT": task.client_config_object,
            "FORM_SENDER_CLIENT_CONFIG_PATH": self._settings.default_client_config_path,
            "FORM_SENDER_ENV": "gcp_batch" if task.batch_enabled() else "cloud_run",
            "FORM_SENDER_LOG_SANITIZE": "1",
            "FORM_SENDER_WORKFLOW_TRIGGER": task.workflow_trigger,
            "FORM_SENDER_TOTAL_SHARDS": str(task.execution.shards),
            "FORM_SENDER_MAX_WORKERS": str(task.execution.workers_per_workflow),
            "FORM_SENDER_TARGETING_ID": str(task.targeting_id),
            "FORM_SENDER_TEST_MODE": "1" if task.test_mode else "0",
            "COMPANY_TABLE": task.tables.company_table,
            "SEND_QUEUE_TABLE": task.tables.send_queue_table,
            "FORM_SENDER_TABLE_MODE": "extra" if task.tables.use_extra_table else ("test" if task.test_mode else "default"),
            "JOB_EXECUTION_ID": job_execution_id,
            "JOB_EXECUTION_META": task.job_execution_meta(),
            "FORM_SENDER_CPU_CLASS": cpu_class,
        }

        if self._settings.dispatcher_base_url:
            env_vars["FORM_SENDER_DISPATCHER_BASE_URL"] = self._settings.dispatcher_base_url
        if self._settings.dispatcher_audience:
            env_vars["FORM_SENDER_DISPATCHER_AUDIENCE"] = self._settings.dispatcher_audience

        if task.tables.submissions_table:
            env_vars["SUBMISSIONS_TABLE"] = task.tables.submissions_table

        if task.branch:
            env_vars["FORM_SENDER_GIT_REF"] = task.branch
            if self._secret_manager and self._settings.git_token_secret:
                token = self._secret_manager.access(self._settings.git_token_secret)
                env_vars["FORM_SENDER_GIT_TOKEN"] = token

        return env_vars

    def list_executions(self, status: str = "running", targeting_id: Optional[int] = None) -> Dict[str, Any]:
        records = self._supabase.list_executions(status=status, targeting_id=targeting_id)
        return {
            "status": "ok",
            "executions": [self._public_execution(record) for record in records],
        }

    def cancel_execution(self, execution_id: str) -> Dict[str, Any]:
        record = self._supabase.get_execution(execution_id)
        if record is None:
            raise HTTPException(status_code=404, detail="execution not found")

        if record.get("status") != "running":
            return {
                "status": "noop",
                "execution": self._public_execution(record),
            }

        metadata = record.get("metadata") or {}
        execution_mode = metadata.get("execution_mode", record.get("execution_mode", "cloud_run"))
        cloud_run_meta = metadata.get("cloud_run") or {}
        batch_meta = metadata.get("batch") or {}

        execution_name = cloud_run_meta.get("execution") or metadata.get("cloud_run_execution")
        operation_name = cloud_run_meta.get("operation") or metadata.get("cloud_run_operation")
        batch_job_name = batch_meta.get("job_name") or metadata.get("batch_job_name")

        if execution_mode == "batch":
            if not batch_job_name:
                raise HTTPException(status_code=400, detail="execution missing Batch identifiers")
            try:
                batch_runner = self._ensure_batch_runner()
                batch_runner.delete_job(batch_job_name)
            except HTTPException:
                raise
            except Exception as exc:  # pylint: disable=broad-except
                raise HTTPException(status_code=502, detail=f"Failed to cancel batch job: {exc}") from exc
        else:
            if not execution_name and not operation_name:
                raise HTTPException(status_code=400, detail="execution missing Cloud Run identifiers")
            try:
                self._job_runner.cancel_execution(
                    execution_name=execution_name,
                    operation_name=operation_name,
                )
            except Exception as exc:  # pylint: disable=broad-except
                raise HTTPException(status_code=502, detail=f"Failed to cancel execution: {exc}") from exc

        ended_at = datetime.now(timezone.utc).isoformat()
        self._supabase.update_status(execution_id, "cancelled", ended_at=ended_at)
        record["status"] = "cancelled"
        record["ended_at"] = ended_at
        return {
            "status": "cancelled",
            "execution": self._public_execution(record),
        }

    def refresh_signed_url(self, request: SignedUrlRefreshRequest) -> Dict[str, str]:
        try:
            signed_url = self._signed_url_manager.refresh_for_object(
                request.client_config_object,
                ttl_hours=request.signed_url_ttl_hours,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if request.execution_id:
            metadata_patch = {
                "client_config_ref": signed_url,
                "batch": {
                    "latest_signed_url": signed_url,
                    "signed_url_refreshed_at": datetime.now(timezone.utc).isoformat(),
                }
            }
            self._supabase.update_metadata(request.execution_id, metadata_patch)

        return {"status": "ok", "signed_url": signed_url}

    def _ensure_batch_runner(self) -> CloudBatchJobRunner:
        if self._batch_runner is None:
            try:
                self._batch_runner = CloudBatchJobRunner(settings=self._settings)
            except RuntimeError as exc:  # pragma: no cover - configuration error
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        return self._batch_runner

    @staticmethod
    def _public_execution(record: Dict[str, Any]) -> Dict[str, Any]:
        metadata = record.get("metadata") or {}
        return {
            "execution_id": record.get("execution_id"),
            "targeting_id": record.get("targeting_id"),
            "run_index_base": record.get("run_index_base"),
            "status": record.get("status"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "task_count": record.get("task_count"),
            "parallelism": record.get("parallelism"),
            "shards": record.get("shards"),
            "workers_per_workflow": record.get("workers_per_workflow"),
            "metadata": metadata,
            "execution_mode": metadata.get("execution_mode", "cloud_run"),
        }
