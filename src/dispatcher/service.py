from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .config import DispatcherSettings, get_settings
from .gcp import CloudRunJobRunner, SecretManager, SignedUrlManager
from .schemas import FormSenderTask
from .supabase_client import JobExecutionRepository


class DispatcherService:
    def __init__(self, settings: DispatcherSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._supabase = JobExecutionRepository(
            url=self._settings.supabase_url,
            key=self._settings.supabase_service_role_key,
        )
        self._signed_url_manager = SignedUrlManager(storage_client=self._build_storage_client(), settings=self._settings)
        self._job_runner = CloudRunJobRunner(settings=self._settings)
        self._secret_manager = SecretManager() if self._settings.git_token_secret else None

    @staticmethod
    def _build_storage_client():
        from google.cloud import storage

        return storage.Client()

    def handle_form_sender_task(self, task: FormSenderTask) -> Dict[str, str]:
        existing = self._supabase.find_active_execution(task.targeting_id, task.execution.run_index_base)
        if existing:
            return {
                "status": "duplicate",
                "job_execution_id": existing["execution_id"],
            }

        try:
            signed_url = self._signed_url_manager.ensure_fresh(task)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        job_execution_id = task.execution_id or uuid.uuid4().hex
        task_data = task.dict()
        task_data["execution_id"] = job_execution_id
        env_vars = self._build_env(task, job_execution_id, signed_url)

        record = self._supabase.insert_execution(
            job_execution_id=job_execution_id,
            payload=task_data,
            cloud_run_operation=None,
            cloud_run_execution=None,
        )

        try:
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
        metadata = dict(record.get("metadata") or {})
        metadata.update(
            {
                "cloud_run_operation": getattr(operation, "name", None),
                "cloud_run_execution": execution_name,
            }
        )
        record = self._supabase.update_metadata(job_execution_id, metadata)
        return {
            "status": "queued",
            "job_execution_id": record["execution_id"],
            "cloud_run_operation": getattr(operation, "name", None),
        }

    def _build_env(self, task: FormSenderTask, job_execution_id: str, signed_url: str) -> Dict[str, str]:
        cpu_class = (task.cpu_class or self._settings.default_cpu_class or "standard").strip().lower()
        env_vars: Dict[str, str] = {
            "FORM_SENDER_CLIENT_CONFIG_URL": signed_url,
            "FORM_SENDER_CLIENT_CONFIG_OBJECT": task.client_config_object,
            "FORM_SENDER_CLIENT_CONFIG_PATH": self._settings.default_client_config_path,
            "FORM_SENDER_ENV": "cloud_run",
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
        execution_name = metadata.get("cloud_run_execution")
        operation_name = metadata.get("cloud_run_operation")

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
        }
