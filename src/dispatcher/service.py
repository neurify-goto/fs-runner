from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException
from pydantic import ValidationError

from .config import DispatcherSettings, get_settings
from .batch_monitor import BatchJobMonitor
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
        self._batch_monitor: Optional[BatchJobMonitor] = None
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
            logger.error(
                "Signed URL validation failed (targeting_id=%s, client_config_object=%s): %s",
                task.targeting_id,
                task.client_config_object,
                exc,
                exc_info=True,
            )
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
                task_count = max(1, task.execution.run_total)
                requested_instance_count = task.batch.instance_count if task.batch and task.batch.instance_count else None

                effective_parallelism = task.effective_parallelism()
                desired_parallelism = effective_parallelism
                if requested_instance_count is not None:
                    desired_parallelism = max(desired_parallelism, min(task_count, requested_instance_count))
                    if task.batch and task.batch.max_parallelism is not None:
                        desired_parallelism = min(desired_parallelism, task.batch.max_parallelism)

                parallelism_value = max(1, min(task_count, desired_parallelism))
                if parallelism_value != task.execution.parallelism:
                    try:
                        self._supabase.update_parallelism(job_execution_id, parallelism_value)
                    except Exception as exc:  # pragma: no cover - best effort
                        logger.warning(
                            "Failed to update Supabase parallelism for execution %s: %s",
                            job_execution_id,
                            exc,
                        )
                job, batch_meta = batch_runner.run_job(
                    task=task,
                    env_vars=env_vars,
                    task_count=task_count,
                    parallelism=parallelism_value,
                )
                batch_metadata = self._build_batch_metadata(
                    task,
                    job,
                    batch_meta,
                    signed_url,
                    task_count,
                    parallelism_value,
                )

                # Schedule monitoring if batch_runner has a proper client
                if batch_runner.client is not None:
                    monitor = self._ensure_batch_monitor(batch_runner)
                    monitor.schedule(job_execution_id, job.name)
                    batch_metadata["monitor"] = {"state": "scheduled"}

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

    def _build_batch_metadata(
        self,
        task: FormSenderTask,
        job,
        batch_meta: Dict[str, Any],
        signed_url: str,
        task_count: int,
        parallelism_value: int,
    ) -> Dict[str, Any]:
        metadata: Dict[str, Any] = dict(batch_meta)
        metadata["job_name"] = job.name
        metadata["task_count"] = task_count
        metadata.setdefault("parallelism", parallelism_value)
        metadata["latest_signed_url"] = signed_url

        if batch_meta.get("instance_count") is None and task.batch and task.batch.instance_count is not None:
            metadata["instance_count"] = task.batch.instance_count

        task_group_name = None
        if getattr(job, "task_groups", None):
            first_group = job.task_groups[0]
            task_group_name = getattr(first_group, "name", None)
        if not task_group_name:
            task_group_name = batch_meta.get("task_group_resource_hint")
        if task_group_name:
            metadata["task_group"] = task_group_name

        configured_task_group_id = batch_meta.get("configured_task_group_id")
        if configured_task_group_id:
            metadata["configured_task_group"] = configured_task_group_id

        metadata.setdefault("array_size", batch_meta.get("array_size", task_count))

        for key in ("attempts", "max_retry_count", "machine_type", "cpu_milli", "memory_mb", "memory_buffer_mb",
                    "prefer_spot", "allow_on_demand", "memory_warning", "computed_memory_mb",
                    "requested_machine_type", "resolved_machine_type", "effective_provisioning_model"):
            if key in batch_meta:
                metadata[key] = batch_meta[key]

        if batch_meta.get("memory_warning") and batch_meta.get("computed_memory_mb") is not None:
            metadata["computed_memory_mb"] = batch_meta["computed_memory_mb"]

        if batch_meta.get("job_template"):
            metadata["job_template"] = batch_meta["job_template"]

        return metadata

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

            # Check if monitor has already detected job completion
            monitor_info = batch_meta.get("monitor", {})
            monitor_state = monitor_info.get("state", "").upper()
            terminal_states = {"SUCCEEDED", "FAILED", "CANCELLED", "CANCELLATION_IN_PROGRESS"}
            if monitor_state in terminal_states:
                logger.info(
                    "Skipping batch job deletion; monitor already detected terminal state",
                    extra={
                        "execution_id": execution_id,
                        "monitor_state": monitor_state,
                    },
                )
            else:
                if monitor_state == "TIMEOUT":
                    logger.info(
                        "Batch monitor timed out; attempting job deletion regardless",
                        extra={
                            "execution_id": execution_id,
                            "monitor_state": monitor_state,
                        },
                    )
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
            logger.error(
                "Signed URL refresh failed (client_config_object=%s): %s",
                request.client_config_object,
                exc,
                exc_info=True,
            )
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

    def _ensure_batch_monitor(self, batch_runner: CloudBatchJobRunner) -> BatchJobMonitor:
        if not hasattr(self, '_batch_monitor') or self._batch_monitor is None:
            self._batch_monitor = BatchJobMonitor(
                batch_client=batch_runner.client,
                supabase=self._supabase,
                settings=self._settings,
                fallback_handler=self._retry_batch_execution,
            )
        return self._batch_monitor

    def _retry_batch_execution(
        self,
        job_execution_id: str,
        job,
        execution: Dict[str, Any],
        events: Sequence[Any],
    ) -> Optional[str]:
        metadata = execution.get("metadata") or {}
        batch_meta = metadata.get("batch") or {}

        if not batch_meta.get("allow_on_demand"):
            return None

        if not batch_meta.get("prefer_spot", True):
            return None

        spot_fallback_meta = batch_meta.get("spot_fallback")
        if isinstance(spot_fallback_meta, dict) and spot_fallback_meta.get("applied"):
            return None

        if not self._should_trigger_spot_fallback(events):
            return None

        task_payload = metadata.get("task_payload")
        if not isinstance(task_payload, dict):
            logger.warning(
                "Missing task payload for batch fallback; skipping",
                extra={"execution_id": job_execution_id},
            )
            return None

        payload = dict(task_payload)
        batch_payload: Dict[str, Any] = dict(payload.get("batch") or {})
        batch_payload["enabled"] = True
        batch_payload["prefer_spot"] = False
        batch_payload["allow_on_demand_fallback"] = False
        payload["batch"] = batch_payload
        payload["mode"] = "batch"

        try:
            task = FormSenderTask.parse_obj(payload)
        except ValidationError as exc:
            logger.error(
                "Failed to rebuild FormSenderTask for fallback",
                extra={"execution_id": job_execution_id, "error": str(exc)},
            )
            return None

        fallback_url = batch_meta.get("latest_signed_url") or metadata.get("client_config_ref")
        try:
            signed_url = self._signed_url_manager.ensure_fresh(task, override_url=fallback_url)
        except ValueError as exc:
            logger.error(
                "Failed to refresh signed URL for fallback",
                extra={"execution_id": job_execution_id, "error": str(exc)},
            )
            return None

        env_vars = self._build_env(task, job_execution_id, signed_url)

        batch_runner = self._ensure_batch_runner()
        task_count = max(1, task.execution.run_total)
        requested_instance_count = (
            task.batch.instance_count if task.batch and task.batch.instance_count else None
        )

        desired_parallelism = task.effective_parallelism()
        if requested_instance_count is not None:
            desired_parallelism = max(desired_parallelism, min(task_count, requested_instance_count))
            if task.batch and task.batch.max_parallelism is not None:
                desired_parallelism = min(desired_parallelism, task.batch.max_parallelism)

        parallelism_value = max(1, min(task_count, desired_parallelism))

        try:
            new_job, batch_meta_new = batch_runner.run_job(
                task=task,
                env_vars=env_vars,
                task_count=task_count,
                parallelism=parallelism_value,
            )
        except Exception as exc:  # pragma: no cover - network path
            logger.error(
                "Failed to launch fallback Batch job",
                extra={"execution_id": job_execution_id, "error": str(exc)},
            )
            return None

        batch_metadata = self._build_batch_metadata(
            task,
            new_job,
            batch_meta_new,
            signed_url,
            task_count,
            parallelism_value,
        )

        fallback_reason = self._summarize_status_events(events)
        batch_metadata["spot_fallback"] = {
            "applied": True,
            "trigger": "monitor",
            "original_job_name": getattr(job, "name", None),
            "reason": fallback_reason,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        batch_metadata.setdefault("monitor", {})
        batch_metadata["monitor"] = {
            "state": "scheduled",
            "recorded_at": datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).isoformat(),
        }

        metadata_patch = {
            "client_config_ref": signed_url,
            "batch": batch_metadata,
        }

        self._supabase.update_metadata(job_execution_id, metadata_patch)
        logger.info(
            "Submitted fallback Batch job with on-demand provisioning",
            extra={
                "execution_id": job_execution_id,
                "new_job": new_job.name,
                "original_job": getattr(job, "name", None),
            },
        )
        return new_job.name

    @staticmethod
    def _should_trigger_spot_fallback(events: Sequence[Any]) -> bool:
        if not events:
            return False

        keywords = (
            "resource exhausted",
            "capacity",
            "quota",
            "preempt",
            "spot",
            "unavailable",
        )

        for event in events:
            description = getattr(event, "description", None) or getattr(event, "message", None)
            if not description:
                continue
            lowered = description.lower()
            if any(keyword in lowered for keyword in keywords):
                return True
        return False

    @staticmethod
    def _summarize_status_events(events: Sequence[Any]) -> Optional[str]:
        if not events:
            return None
        descriptions = [
            (getattr(event, "description", None) or getattr(event, "message", None) or "").strip()
            for event in events
        ]
        descriptions = [text for text in descriptions if text]
        if not descriptions:
            return None
        summary = "; ".join(descriptions)
        return summary[:512]

    def _ensure_batch_runner(self) -> CloudBatchJobRunner:
        if not hasattr(self, '_batch_runner') or self._batch_runner is None:
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
